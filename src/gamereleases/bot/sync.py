"""The periodic sync: panels, release-day alerts and personal reminders."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import discord

from gamereleases.bot.reconciler import PanelReconciler, ReconcileResult
from gamereleases.bot.views import (
    PanelPayload,
    build_game_card_payload,
    build_overview_payload,
    build_release_alert_embed,
)
from gamereleases.domain.formatting import escape_markdown
from gamereleases.domain.models import (
    MAX_WINDOW_DAYS,
    TOP_LIST_SIZE,
    Game,
    GuildConfig,
    RankedGame,
    Reminder,
)
from gamereleases.domain.ranking import (
    rank_upcoming,
    resolve_release_date,
    resolve_timezone,
    today_in,
)
from gamereleases.igdb.auth import TwitchAuthError
from gamereleases.igdb.client import IgdbError
from gamereleases.storage.repository import Repository

logger = logging.getLogger(__name__)

# The furthest a calendar day can run ahead of UTC (UTC+14).
_MAX_DAYS_AHEAD_OF_UTC = 1


class GameSource(Protocol):
    """The IGDB operations the sync needs."""

    async def upcoming_games(
        self,
        *,
        start: date,
        days: int,
        include_dlc: bool = False,
        bypass_cache: bool = False,
    ) -> list[Game]: ...

    async def get_games_by_ids(
        self, game_ids: Sequence[int], *, bypass_cache: bool = False
    ) -> list[Game]: ...


class Notifier(Protocol):
    """Delivers alerts and reminders.

    Implementations let Discord's own errors through: ``discord.Forbidden``
    when a user or channel cannot be written to, ``discord.NotFound`` when it
    no longer exists.
    """

    async def send_alert(
        self, channel_id: int, embed: discord.Embed, *, role_id: int | None
    ) -> None: ...

    async def send_direct(self, user_id: int, content: str) -> None: ...

    async def send_channel_mention(
        self, channel_id: int, user_id: int, content: str
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class SyncReport:
    """The outcome of a sync pass, by number of guilds and panel messages."""

    finished_at: datetime
    guilds_synced: int = 0
    guilds_failed: int = 0
    # The upcoming catalog could not be fetched for at least one DLC setting.
    catalog_failed: bool = False
    # Panel messages posted, edited, recreated or deleted.
    messages_changed: int = 0

    @property
    def ok(self) -> bool:
        return not self.catalog_failed and self.guilds_failed == 0


class SyncCycle:
    """One pass over every configured guild, then over pending reminders.

    A failure in one guild or phase is logged and does not stop the others, so
    the periodic task keeps running.
    """

    def __init__(
        self,
        *,
        repository: Repository,
        games: GameSource,
        reconciler: PanelReconciler[PanelPayload],
        notifier: Notifier,
        guild_ids: Callable[[], Iterable[int]],
        default_timezone: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._games = games
        self._reconciler = reconciler
        self._notifier = notifier
        self._guild_ids = guild_ids
        self._default_timezone = default_timezone
        self._clock = clock
        self._lock = asyncio.Lock()
        self.last_report: SyncReport | None = None

    async def run(self, *, bypass_cache: bool = False) -> SyncReport:
        """Run a full pass; concurrent calls queue up instead of overlapping."""
        async with self._lock:
            now = self._clock()
            configs = await self._load_configs()
            report = await self._sync_configs(configs, now, bypass_cache)
            try:
                await self._deliver_reminders(now, bypass_cache)
            except Exception:
                logger.exception("Reminder delivery failed")
            self.last_report = report
            return report

    async def sync_guild(
        self, guild_id: int, *, bypass_cache: bool = False
    ) -> SyncReport:
        """Refresh the panel and alerts of one guild.

        Does nothing for a guild without a panel channel. Reminders are left to
        the periodic pass, and the result is not recorded as the last full run.
        """
        async with self._lock:
            now = self._clock()
            config = await self._repository.get_guild(guild_id)
            configs = (
                [] if config is None or config.panel_channel_id is None else [config]
            )
            return await self._sync_configs(configs, now, bypass_cache)

    async def _sync_configs(
        self, configs: Sequence[GuildConfig], now: datetime, bypass_cache: bool
    ) -> SyncReport:
        catalogs = await self._fetch_catalogs(configs, now, bypass_cache)
        needed = {config.include_dlc for config in configs}
        synced = failed = changed = 0
        for config in configs:
            catalog = catalogs.get(config.include_dlc)
            if catalog is None:
                failed += 1
                continue
            try:
                result = await self._sync_guild(config, catalog, now)
            except Exception:
                logger.exception("Sync failed for guild %d", config.guild_id)
                failed += 1
                continue
            if result is None:
                failed += 1
                continue
            synced += 1
            changed += (
                result.created + result.edited + result.recreated + result.deleted
            )
        return SyncReport(
            finished_at=self._clock(),
            guilds_synced=synced,
            guilds_failed=failed,
            catalog_failed=not needed <= catalogs.keys(),
            messages_changed=changed,
        )

    async def _load_configs(self) -> list[GuildConfig]:
        configs: list[GuildConfig] = []
        for guild_id in sorted(set(self._guild_ids())):
            config = await self._repository.get_guild(guild_id)
            if config is not None and config.panel_channel_id is not None:
                configs.append(config)
        return configs

    async def _fetch_catalogs(
        self, configs: Sequence[GuildConfig], now: datetime, bypass_cache: bool
    ) -> dict[bool, list[Game]]:
        """Fetch the upcoming games once per DLC setting in use."""
        catalogs: dict[bool, list[Game]] = {}
        start = today_in(now, UTC)
        for include_dlc in sorted({config.include_dlc for config in configs}):
            try:
                catalogs[include_dlc] = await self._games.upcoming_games(
                    start=start,
                    days=MAX_WINDOW_DAYS,
                    include_dlc=include_dlc,
                    bypass_cache=bypass_cache,
                )
            except (IgdbError, TwitchAuthError):
                logger.exception(
                    "Could not fetch upcoming games (include_dlc=%s)", include_dlc
                )
        return catalogs

    # -- panels and release-day alerts ---------------------------------------

    async def _sync_guild(
        self, config: GuildConfig, catalog: Sequence[Game], now: datetime
    ) -> ReconcileResult | None:
        """Sync one guild; return ``None`` if its panel could not be reconciled."""
        channel_id = config.panel_channel_id
        if channel_id is None:
            return None
        today = today_in(now, resolve_timezone(config.timezone))

        def rank(limit: int) -> list[RankedGame]:
            return rank_upcoming(
                catalog,
                today=today,
                preset=config.platform_preset,
                min_hypes=config.min_hypes,
                include_dlc=config.include_dlc,
                window_days=MAX_WINDOW_DAYS,
                limit=limit,
            )

        top = rank(TOP_LIST_SIZE)
        cards = rank(config.panel_count)
        try:
            result = await self._reconciler.reconcile(
                config.guild_id,
                channel_id,
                build_overview_payload(
                    top, synced_at=now, preset=config.platform_preset
                ),
                [build_game_card_payload(item, today=today) for item in cards],
                config.panel_count,
            )
        except Exception:
            logger.exception("Panel sync failed for guild %d", config.guild_id)
            result = None
        else:
            logger.info("Synced panel for guild %d: %s", config.guild_id, result)

        await self._send_release_alerts(config, channel_id, top, today)
        return result

    async def _send_release_alerts(
        self,
        config: GuildConfig,
        panel_channel_id: int,
        top: Sequence[RankedGame],
        today: date,
    ) -> None:
        target = config.alert_channel_id or panel_channel_id
        for item in top:
            if item.release_date != today:
                continue
            # Recorded first, so overlapping passes can never alert twice.
            if not await self._repository.mark_notified(config.guild_id, item.game.id):
                continue
            try:
                await self._notifier.send_alert(
                    target,
                    build_release_alert_embed(item.game),
                    role_id=config.alert_role_id,
                )
            except Exception:
                logger.exception(
                    "Could not send the release alert for game %d in guild %d",
                    item.game.id,
                    config.guild_id,
                )

    # -- personal reminders --------------------------------------------------

    async def _deliver_reminders(self, now: datetime, bypass_cache: bool) -> None:
        pending_ids = await self._repository.get_pending_reminder_game_ids()
        if not pending_ids:
            return
        try:
            games = await self._games.get_games_by_ids(
                pending_ids, bypass_cache=bypass_cache
            )
        except (IgdbError, TwitchAuthError):
            logger.exception("Could not resolve the games with pending reminders")
            return

        release_dates: dict[int, date] = {}
        for game in games:
            release = resolve_release_date(game)
            if release is not None:
                release_dates[game.id] = release
        names = {game.id: game.name for game in games}

        # Guilds may be in different time zones, so only drop the games that are
        # not out anywhere yet; each reminder is checked against its own guild.
        horizon = today_in(now, UTC) + timedelta(days=_MAX_DAYS_AHEAD_OF_UTC)
        candidate_ids = [
            game_id for game_id, day in release_dates.items() if day <= horizon
        ]
        reminders = await self._repository.get_due_reminders(candidate_ids)

        zones: dict[int | None, ZoneInfo] = {}
        for reminder in reminders:
            zone = zones.get(reminder.guild_id)
            if zone is None:
                zone = await self._reminder_zone(reminder)
                zones[reminder.guild_id] = zone
            if release_dates[reminder.game_id] > today_in(now, zone):
                continue
            try:
                delivered = await self._deliver(reminder, names[reminder.game_id])
            except Exception:
                logger.exception(
                    "Reminder for game %d to user %d failed",
                    reminder.game_id,
                    reminder.user_id,
                )
                continue
            if delivered:
                await self._repository.mark_reminder_sent(
                    reminder.user_id, reminder.game_id
                )

    async def _reminder_zone(self, reminder: Reminder) -> ZoneInfo:
        if reminder.guild_id is not None:
            config = await self._repository.get_guild(reminder.guild_id)
            if config is not None:
                return resolve_timezone(config.timezone)
        return resolve_timezone(self._default_timezone)

    async def _deliver(self, reminder: Reminder, name: str) -> bool:
        """Send one reminder; return ``False`` if it should be retried later."""
        text = f"🔔 **{escape_markdown(name)}** è finalmente uscito!"
        try:
            await self._notifier.send_direct(reminder.user_id, text)
        except discord.Forbidden:
            logger.info("User %d does not accept DMs", reminder.user_id)
        except discord.NotFound:
            logger.info("User %d no longer exists", reminder.user_id)
            return True
        except discord.HTTPException:
            logger.warning("Could not DM user %d; will retry", reminder.user_id)
            return False
        else:
            return True

        if reminder.channel_id is None:
            logger.info("No fallback channel for user %d", reminder.user_id)
            return True
        try:
            await self._notifier.send_channel_mention(
                reminder.channel_id,
                reminder.user_id,
                f"{text}\nNon riesco a scriverti in privato, quindi ti avviso qui.",
            )
        except (discord.Forbidden, discord.NotFound):
            logger.info("Channel %d is not writable", reminder.channel_id)
            return True
        except discord.HTTPException:
            logger.warning(
                "Could not mention user %d in channel %d; will retry",
                reminder.user_id,
                reminder.channel_id,
            )
            return False
        return True
