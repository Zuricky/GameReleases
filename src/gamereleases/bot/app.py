"""Discord client setup and lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import discord
import httpx
from discord.ext import commands, tasks

from gamereleases.bot.reconciler import PanelReconciler
from gamereleases.bot.sync import SyncCycle, SyncReport
from gamereleases.bot.views import (
    DiscordMessenger,
    PanelPayload,
    RemindButton,
    TrailerButton,
)
from gamereleases.config import Settings
from gamereleases.igdb.auth import TwitchAuth
from gamereleases.igdb.client import IgdbClient
from gamereleases.storage.db import Database
from gamereleases.storage.repository import Repository

logger = logging.getLogger(__name__)

SYNC_INTERVAL_HOURS = 2

EXTENSIONS = (
    "gamereleases.bot.cogs.releases",
    "gamereleases.bot.cogs.admin",
)

COMMANDS_MARKER_NAME = "commands.sha256"


class DiscordNotifier:
    """Delivers alerts and reminders through a Discord client."""

    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def send_alert(
        self, channel_id: int, embed: discord.Embed, *, role_id: int | None
    ) -> None:
        channel = self._client.get_partial_messageable(channel_id)
        if role_id is None:
            await channel.send(embed=embed)
        else:
            await channel.send(
                content=f"<@&{role_id}>",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )

    async def send_direct(self, user_id: int, content: str) -> None:
        user = await self._client.fetch_user(user_id)
        await user.send(content)

    async def send_channel_mention(
        self, channel_id: int, user_id: int, content: str
    ) -> None:
        channel = self._client.get_partial_messageable(channel_id)
        await channel.send(
            f"<@{user_id}> {content}",
            allowed_mentions=discord.AllowedMentions(
                users=[discord.Object(id=user_id)]
            ),
        )


class GameReleasesBot(commands.Bot):
    """The bot: owns the long-lived services and the periodic sync."""

    # Assigned in ``setup_hook``; the persistent buttons read them from here.
    repository: Repository
    igdb: IgdbClient
    reconciler: PanelReconciler[PanelPayload]

    def __init__(self, settings: Settings) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions.none(),
            help_command=None,
        )
        self.settings = settings
        self.started_at = discord.utils.utcnow()
        self._db: Database | None = None
        self._http: httpx.AsyncClient | None = None
        self._sync: SyncCycle | None = None

    async def setup_hook(self) -> None:
        settings = self.settings
        self._db = await Database.open(settings.db_path)
        self.repository = Repository(self._db)

        self._http = httpx.AsyncClient()
        auth = TwitchAuth(
            settings.twitch_client_id,
            settings.twitch_client_secret.get_secret_value(),
            self._http,
        )
        self.igdb = IgdbClient(settings.twitch_client_id, auth, self._http)
        self.reconciler = PanelReconciler(self.repository, DiscordMessenger(self))
        self._sync = SyncCycle(
            repository=self.repository,
            games=self.igdb,
            reconciler=self.reconciler,
            notifier=DiscordNotifier(self),
            guild_ids=lambda: [guild.id for guild in self.guilds],
            default_timezone=settings.timezone,
        )

        self.add_dynamic_items(RemindButton, TrailerButton)
        await self._load_extensions()
        await self._sync_commands_if_changed()
        self.sync_loop.start()

    async def _load_extensions(self) -> None:
        """Load the command cogs, skipping the ones that are not implemented yet."""
        for name in EXTENSIONS:
            try:
                await self.load_extension(name)
            except (commands.ExtensionNotFound, commands.NoEntryPointError):
                logger.info("Skipping %s: no cog available", name)
            else:
                logger.info("Loaded %s", name)

    async def _sync_commands_if_changed(self) -> None:
        """Push slash commands to Discord only when their definition changed.

        Syncing is rate limited, so it must not happen on every start or
        reconnect. A digest of the command definitions is kept next to the
        database; an empty tree is never synced, as that would delete the
        commands registered earlier.
        """
        definitions = [
            command.to_dict(self.tree) for command in self.tree.get_commands()
        ]
        if not definitions:
            logger.info("No slash commands defined; skipping command sync")
            return
        digest = hashlib.sha256(
            json.dumps(definitions, sort_keys=True).encode("utf-8")
        ).hexdigest()
        marker = self.settings.db_path.parent / COMMANDS_MARKER_NAME

        if await asyncio.to_thread(_read_text, marker) == digest:
            logger.info("Slash commands are unchanged; skipping command sync")
            return
        synced = await self.tree.sync()
        await asyncio.to_thread(marker.write_text, digest, "utf-8")
        logger.info("Synced %d slash commands", len(synced))

    def _require_sync(self) -> SyncCycle:
        if self._sync is None:
            raise RuntimeError("the bot has not finished starting up")
        return self._sync

    async def run_sync_cycle(self, bypass_cache: bool = False) -> SyncReport:
        """Refresh panels, send release-day alerts and deliver due reminders."""
        return await self._require_sync().run(bypass_cache=bypass_cache)

    async def sync_guild(self, guild_id: int, bypass_cache: bool = False) -> SyncReport:
        """Refresh the panel and release-day alerts of a single guild."""
        return await self._require_sync().sync_guild(
            guild_id, bypass_cache=bypass_cache
        )

    @property
    def uptime(self) -> timedelta:
        """Time elapsed since the bot object was created."""
        return discord.utils.utcnow() - self.started_at

    @property
    def last_sync(self) -> SyncReport | None:
        """The outcome of the latest full sync, or ``None`` before the first."""
        return None if self._sync is None else self._sync.last_report

    @property
    def next_sync_at(self) -> datetime | None:
        """When the periodic sync runs next, if it is scheduled."""
        return self.sync_loop.next_iteration

    @tasks.loop(hours=SYNC_INTERVAL_HOURS)
    async def sync_loop(self) -> None:
        await self.run_sync_cycle()

    @sync_loop.before_loop
    async def _before_sync_loop(self) -> None:
        # Guilds are only known once the gateway has delivered them.
        await self.wait_until_ready()

    async def on_ready(self) -> None:
        logger.info("Connected as %s in %d guilds", self.user, len(self.guilds))

    async def close(self) -> None:
        """Stop the periodic task, then release the HTTP client and database."""
        self.sync_loop.cancel()
        await super().close()
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._db is not None:
            await self._db.close()
            self._db = None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text("utf-8").strip()
    except FileNotFoundError:
        return None
