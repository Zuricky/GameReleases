"""Tests for the periodic sync: panels, release-day alerts and reminders."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import discord
import pytest

from gamereleases.bot.reconciler import PanelReconciler
from gamereleases.bot.sync import SyncCycle
from gamereleases.bot.views import PanelPayload
from gamereleases.domain.models import TOP_LIST_SIZE, Game, GuildConfig
from gamereleases.igdb.client import IgdbError
from gamereleases.storage.db import Database
from gamereleases.storage.repository import Repository

GUILD = 100
PANEL_CHANNEL = 10
ALERT_CHANNEL = 11
FALLBACK_CHANNEL = 12
ROLE = 555
USER = 7
NOW = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
TODAY = NOW.date()


def make_game(game_id: int, name: str, release: date | None, hypes: int = 10) -> Game:
    moment = (
        None
        if release is None
        else datetime(release.year, release.month, release.day, tzinfo=UTC)
    )
    return Game(
        id=game_id,
        name=name,
        slug=f"game-{game_id}",
        first_release_date=moment,
        hypes=hypes,
    )


def http_error[E: discord.HTTPException](kind: type[E]) -> E:
    return kind(MagicMock(status=400, reason="error"), "error")


class FakeGames:
    """Serves a fixed catalog and, separately, a lookup table by ID."""

    def __init__(self) -> None:
        self.catalog: list[Game] = []
        self.known: dict[int, Game] = {}
        self.upcoming_error: Exception | None = None
        self.lookup_calls: list[list[int]] = []

    async def upcoming_games(
        self,
        *,
        start: date,
        days: int,
        include_dlc: bool = False,
        bypass_cache: bool = False,
    ) -> list[Game]:
        if self.upcoming_error is not None:
            raise self.upcoming_error
        return list(self.catalog)

    async def get_games_by_ids(
        self, game_ids: Sequence[int], *, bypass_cache: bool = False
    ) -> list[Game]:
        self.lookup_calls.append(list(game_ids))
        return [self.known[i] for i in game_ids if i in self.known]


class FakeMessenger:
    def __init__(self) -> None:
        self.sent: list[tuple[int, PanelPayload]] = []
        self._next_id = 1000

    async def send(self, channel_id: int, payload: PanelPayload) -> int:
        self._next_id += 1
        self.sent.append((channel_id, payload))
        return self._next_id

    async def edit(
        self, channel_id: int, message_id: int, payload: PanelPayload
    ) -> None:
        return None

    async def delete(self, channel_id: int, message_id: int) -> None:
        return None


@dataclass
class FakeNotifier:
    alerts: list[tuple[int, discord.Embed, int | None]] = field(default_factory=list)
    direct: list[tuple[int, str]] = field(default_factory=list)
    mentions: list[tuple[int, int, str]] = field(default_factory=list)
    direct_error: Exception | None = None
    channel_error: Exception | None = None

    async def send_alert(
        self, channel_id: int, embed: discord.Embed, *, role_id: int | None
    ) -> None:
        self.alerts.append((channel_id, embed, role_id))

    async def send_direct(self, user_id: int, content: str) -> None:
        if self.direct_error is not None:
            raise self.direct_error
        self.direct.append((user_id, content))

    async def send_channel_mention(
        self, channel_id: int, user_id: int, content: str
    ) -> None:
        if self.channel_error is not None:
            raise self.channel_error
        self.mentions.append((channel_id, user_id, content))


@dataclass
class Harness:
    repo: Repository
    games: FakeGames
    messenger: FakeMessenger
    notifier: FakeNotifier
    cycle: SyncCycle
    now: datetime = NOW

    async def run(self, *, bypass_cache: bool = False) -> None:
        await self.cycle.run(bypass_cache=bypass_cache)


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    db = await Database.open(tmp_path / "sync.db")
    repo = Repository(db)
    games = FakeGames()
    messenger = FakeMessenger()
    notifier = FakeNotifier()
    state: dict[str, Any] = {"guilds": [GUILD]}
    h = Harness(
        repo=repo,
        games=games,
        messenger=messenger,
        notifier=notifier,
        cycle=SyncCycle(
            repository=repo,
            games=games,
            reconciler=PanelReconciler(repo, messenger),
            notifier=notifier,
            guild_ids=lambda: state["guilds"],
            default_timezone="Europe/Rome",
            clock=lambda: h.now,
        ),
    )
    await repo.upsert_guild(GuildConfig(guild_id=GUILD, panel_channel_id=PANEL_CHANNEL))
    yield h
    await db.close()


# -- release-day alerts --------------------------------------------------------


async def test_alert_is_sent_once_per_game(harness: Harness) -> None:
    harness.games.catalog = [make_game(1, "Out Today", TODAY)]

    await harness.run()
    await harness.run()

    [(channel, embed, role)] = harness.notifier.alerts
    assert channel == PANEL_CHANNEL
    assert role is None
    assert embed.title == "🎮 Esce oggi!"
    assert embed.description is not None
    assert "Out Today" in embed.description


async def test_alert_skipped_when_already_notified(harness: Harness) -> None:
    harness.games.catalog = [make_game(1, "Out Today", TODAY)]
    assert await harness.repo.mark_notified(GUILD, 1)

    await harness.run()

    assert harness.notifier.alerts == []


async def test_alert_goes_to_alert_channel_with_role(harness: Harness) -> None:
    await harness.repo.update_alert_channel(GUILD, ALERT_CHANNEL)
    await harness.repo.update_alert_role(GUILD, ROLE)
    harness.games.catalog = [make_game(1, "Out Today", TODAY)]

    await harness.run()

    [(channel, _, role)] = harness.notifier.alerts
    assert (channel, role) == (ALERT_CHANNEL, ROLE)


async def test_only_games_releasing_today_are_announced(harness: Harness) -> None:
    harness.games.catalog = [
        make_game(1, "Today", TODAY),
        make_game(2, "Tomorrow", TODAY + timedelta(days=1)),
        make_game(3, "Yesterday", TODAY - timedelta(days=1)),
    ]

    await harness.run()

    assert len(harness.notifier.alerts) == 1
    assert await harness.repo.is_notified(GUILD, 1)
    assert not await harness.repo.is_notified(GUILD, 2)
    assert not await harness.repo.is_notified(GUILD, 3)


async def test_alert_day_follows_the_guild_time_zone(harness: Harness) -> None:
    await harness.repo.update_timezone(GUILD, "Pacific/Auckland")
    harness.now = datetime(2026, 11, 21, 20, 0, tzinfo=UTC)  # already the 22nd there
    harness.games.catalog = [make_game(1, "Auckland Day", date(2026, 11, 22))]

    await harness.run()

    assert len(harness.notifier.alerts) == 1


async def test_failed_alert_does_not_stop_the_others(harness: Harness) -> None:
    harness.games.catalog = [
        make_game(1, "First", TODAY, hypes=20),
        make_game(2, "Second", TODAY, hypes=10),
    ]
    original = harness.notifier.send_alert
    calls = 0

    async def flaky(channel_id: int, embed: discord.Embed, *, role_id: int | None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise http_error(discord.Forbidden)
        await original(channel_id, embed, role_id=role_id)

    harness.notifier.send_alert = flaky  # type: ignore[method-assign]

    await harness.run()

    assert calls == 2
    assert len(harness.notifier.alerts) == 1


# -- panels --------------------------------------------------------------------


async def test_panel_is_reconciled_with_overview_and_cards(harness: Harness) -> None:
    harness.games.catalog = [
        make_game(i, f"Game {i}", TODAY + timedelta(days=i), hypes=i)
        for i in range(1, 9)
    ]

    await harness.run()

    channels = {channel for channel, _ in harness.messenger.sent}
    assert channels == {PANEL_CHANNEL}
    overview, *cards = (payload for _, payload in harness.messenger.sent)
    assert overview.title == "🎮 Prossime uscite"
    assert len(cards) == 5  # default panel_count
    assert all(card.game_id is not None for card in cards)
    # The five most hyped games, in release order.
    assert [card.title for card in cards] == [f"Game {i}" for i in (4, 5, 6, 7, 8)]


async def test_unchanged_panel_is_not_reposted(harness: Harness) -> None:
    harness.games.catalog = [make_game(1, "Game", TODAY + timedelta(days=3))]

    await harness.run()
    posted = len(harness.messenger.sent)
    await harness.run()

    assert len(harness.messenger.sent) == posted


async def test_guild_without_panel_channel_is_ignored(harness: Harness) -> None:
    await harness.repo.upsert_guild(GuildConfig(guild_id=GUILD + 1))
    harness.cycle._guild_ids = lambda: [GUILD + 1]  # pyright: ignore[reportPrivateUsage]
    harness.games.catalog = [make_game(1, "Out Today", TODAY)]

    await harness.run()

    assert harness.messenger.sent == []
    assert harness.notifier.alerts == []


async def test_igdb_failure_skips_panels_but_not_reminders(harness: Harness) -> None:
    harness.games.upcoming_error = IgdbError("down")
    harness.games.known[9] = make_game(9, "Released", TODAY - timedelta(days=1))
    await harness.repo.toggle_reminder(USER, 9, GUILD, FALLBACK_CHANNEL)

    await harness.run()

    assert harness.messenger.sent == []
    assert [user for user, _ in harness.notifier.direct] == [USER]


# -- personal reminders --------------------------------------------------------


async def test_reminder_is_sent_by_dm_and_marked(harness: Harness) -> None:
    # Not in the upcoming catalog at all: resolved purely by ID.
    harness.games.known[9] = make_game(9, "Old *Favorite*", TODAY - timedelta(days=3))
    await harness.repo.toggle_reminder(USER, 9, GUILD, FALLBACK_CHANNEL)

    await harness.run()

    [(user, text)] = harness.notifier.direct
    assert user == USER
    assert text == "🔔 **Old \\*Favorite\\*** è finalmente uscito!"
    assert harness.notifier.mentions == []
    assert await harness.repo.get_pending_reminder_game_ids() == []


async def test_reminder_for_game_outside_the_top_list(harness: Harness) -> None:
    hits = [
        make_game(i, f"Hit {i}", TODAY + timedelta(days=i), hypes=1000 + i)
        for i in range(1, TOP_LIST_SIZE + 1)
    ]
    obscure = make_game(99, "Obscure", TODAY, hypes=0)
    harness.games.catalog = [*hits, obscure]
    harness.games.known[99] = obscure
    await harness.repo.toggle_reminder(USER, 99, GUILD, FALLBACK_CHANNEL)

    await harness.run()

    assert [user for user, _ in harness.notifier.direct] == [USER]
    assert harness.notifier.alerts == []  # not in the guild's top list


async def test_reminder_falls_back_to_channel_when_dms_are_closed(
    harness: Harness,
) -> None:
    harness.games.known[9] = make_game(9, "Released", TODAY)
    harness.notifier.direct_error = http_error(discord.Forbidden)
    await harness.repo.toggle_reminder(USER, 9, GUILD, FALLBACK_CHANNEL)

    await harness.run()

    [(channel, user, text)] = harness.notifier.mentions
    assert (channel, user) == (FALLBACK_CHANNEL, USER)
    assert "**Released** è finalmente uscito!" in text
    assert await harness.repo.get_pending_reminder_game_ids() == []


async def test_closed_dms_without_channel_still_settle_the_reminder(
    harness: Harness,
) -> None:
    harness.games.known[9] = make_game(9, "Released", TODAY)
    harness.notifier.direct_error = http_error(discord.Forbidden)
    await harness.repo.toggle_reminder(USER, 9, None, None)

    await harness.run()

    assert harness.notifier.mentions == []
    assert await harness.repo.get_pending_reminder_game_ids() == []


async def test_unwritable_fallback_channel_settles_the_reminder(
    harness: Harness,
) -> None:
    harness.games.known[9] = make_game(9, "Released", TODAY)
    harness.notifier.direct_error = http_error(discord.Forbidden)
    harness.notifier.channel_error = http_error(discord.Forbidden)
    await harness.repo.toggle_reminder(USER, 9, GUILD, FALLBACK_CHANNEL)

    await harness.run()

    assert await harness.repo.get_pending_reminder_game_ids() == []


async def test_transient_dm_failure_is_retried_next_cycle(harness: Harness) -> None:
    harness.games.known[9] = make_game(9, "Released", TODAY)
    harness.notifier.direct_error = http_error(discord.HTTPException)
    await harness.repo.toggle_reminder(USER, 9, GUILD, FALLBACK_CHANNEL)

    await harness.run()
    assert await harness.repo.get_pending_reminder_game_ids() == [9]

    harness.notifier.direct_error = None
    await harness.run()
    assert [user for user, _ in harness.notifier.direct] == [USER]
    assert await harness.repo.get_pending_reminder_game_ids() == []


async def test_reminder_not_sent_before_release(harness: Harness) -> None:
    harness.games.known[9] = make_game(9, "Soon", TODAY + timedelta(days=2))
    harness.games.known[10] = make_game(10, "No Date", None)
    await harness.repo.toggle_reminder(USER, 9, GUILD, FALLBACK_CHANNEL)
    await harness.repo.toggle_reminder(USER, 10, GUILD, FALLBACK_CHANNEL)

    await harness.run()

    assert harness.notifier.direct == []
    assert await harness.repo.get_pending_reminder_game_ids() == [9, 10]


async def test_reminder_is_sent_only_once(harness: Harness) -> None:
    harness.games.known[9] = make_game(9, "Released", TODAY)
    await harness.repo.toggle_reminder(USER, 9, GUILD, FALLBACK_CHANNEL)

    await harness.run()
    await harness.run()

    assert len(harness.notifier.direct) == 1


async def test_reminders_use_each_guild_time_zone(harness: Harness) -> None:
    other_guild = GUILD + 1
    await harness.repo.upsert_guild(
        GuildConfig(guild_id=GUILD, timezone="Pacific/Auckland")
    )
    await harness.repo.upsert_guild(
        GuildConfig(guild_id=other_guild, timezone="America/Los_Angeles")
    )
    # 20:00 UTC on the 21st: already the 22nd in Auckland, still the 21st in LA.
    harness.now = datetime(2026, 11, 21, 20, 0, tzinfo=UTC)
    harness.games.known[9] = make_game(9, "Tomorrow In UTC", date(2026, 11, 22))
    await harness.repo.toggle_reminder(1, 9, GUILD, FALLBACK_CHANNEL)
    await harness.repo.toggle_reminder(2, 9, other_guild, FALLBACK_CHANNEL)

    await harness.run()

    assert [user for user, _ in harness.notifier.direct] == [1]
    assert await harness.repo.get_pending_reminder_game_ids() == [9]


async def test_no_lookup_without_pending_reminders(harness: Harness) -> None:
    await harness.run()

    assert harness.games.lookup_calls == []


async def test_pending_games_are_looked_up_in_one_batch(harness: Harness) -> None:
    for game_id in (3, 1, 2):
        harness.games.known[game_id] = make_game(game_id, f"G{game_id}", TODAY)
        await harness.repo.toggle_reminder(USER, game_id, GUILD, FALLBACK_CHANNEL)
        await harness.repo.toggle_reminder(USER + 1, game_id, GUILD, FALLBACK_CHANNEL)

    await harness.run()

    assert harness.games.lookup_calls == [[1, 2, 3]]
    assert len(harness.notifier.direct) == 6


# -- reports and single-guild sync ---------------------------------------------


async def test_run_reports_what_it_did_and_remembers_it(harness: Harness) -> None:
    harness.games.catalog = [make_game(1, "Game", TODAY + timedelta(days=3))]
    assert harness.cycle.last_report is None

    report = await harness.cycle.run()

    assert report.ok
    assert report.guilds_synced == 1
    assert report.guilds_failed == 0
    assert report.messages_changed == 2  # overview and one card
    assert report.finished_at == NOW
    assert harness.cycle.last_report == report


async def test_unchanged_panel_reports_no_changes(harness: Harness) -> None:
    harness.games.catalog = [make_game(1, "Game", TODAY + timedelta(days=3))]
    await harness.run()

    report = await harness.cycle.run()

    assert report.guilds_synced == 1
    assert report.messages_changed == 0


async def test_igdb_failure_is_reported(harness: Harness) -> None:
    harness.games.upcoming_error = IgdbError("down")

    report = await harness.cycle.run()

    assert not report.ok
    assert report.catalog_failed
    assert report.guilds_synced == 0
    assert report.guilds_failed == 1


async def test_panel_failure_is_reported_as_a_failed_guild(harness: Harness) -> None:
    async def failing_send(channel_id: int, payload: PanelPayload) -> int:
        raise RuntimeError("boom")

    harness.messenger.send = failing_send  # pyright: ignore[reportAttributeAccessIssue]
    harness.games.catalog = [make_game(1, "Game", TODAY + timedelta(days=3))]

    report = await harness.cycle.run()

    assert not report.ok
    assert (report.guilds_synced, report.guilds_failed) == (0, 1)
    assert not report.catalog_failed


async def test_sync_guild_touches_only_that_guild(harness: Harness) -> None:
    await harness.repo.upsert_guild(
        GuildConfig(guild_id=GUILD + 1, panel_channel_id=PANEL_CHANNEL + 1)
    )
    harness.cycle._guild_ids = lambda: [GUILD, GUILD + 1]  # pyright: ignore[reportPrivateUsage]
    harness.games.catalog = [make_game(1, "Game", TODAY + timedelta(days=3))]

    report = await harness.cycle.sync_guild(GUILD + 1)

    assert report.guilds_synced == 1
    assert {channel for channel, _ in harness.messenger.sent} == {PANEL_CHANNEL + 1}
    assert await harness.repo.get_panel_messages(GUILD) == []
    # A single-guild pass is not the periodic run.
    assert harness.cycle.last_report is None


async def test_sync_guild_ignores_a_guild_without_panel_channel(
    harness: Harness,
) -> None:
    await harness.repo.upsert_guild(GuildConfig(guild_id=GUILD + 1))
    await harness.cycle.sync_guild(GUILD + 1)
    report = await harness.cycle.sync_guild(GUILD + 2)  # unknown guild

    assert harness.messenger.sent == []
    assert (report.guilds_synced, report.guilds_failed) == (0, 0)
    assert report.ok
