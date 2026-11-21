"""Public release browsing commands."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime

import discord
from discord import app_commands
from discord.ext import commands

from gamereleases.bot.app import GameReleasesBot
from gamereleases.bot.views import (
    CARD_TIMEOUT_SECONDS,
    GameCardView,
    PaginatedReleasesView,
    PanelPayload,
    build_game_detail_payload,
)
from gamereleases.domain.formatting import build_calendar, escape_markdown
from gamereleases.domain.models import (
    MAX_WINDOW_DAYS,
    TOP_LIST_SIZE,
    Game,
    GuildConfig,
    RankedGame,
)
from gamereleases.domain.ranking import (
    normalize_title,
    rank_upcoming,
    resolve_timezone,
    today_in,
)
from gamereleases.igdb.auth import TwitchAuthError
from gamereleases.igdb.client import IgdbError

logger = logging.getLogger(__name__)

MIN_WINDOW_DAYS = 1
DEFAULT_DAYS = 30
# Enough to fill a compact calendar; longer ones are cut short with a note.
CALENDAR_MAX_GAMES = 40

# Discord allows at most 25 autocomplete choices, each at most 100 characters.
AUTOCOMPLETE_MAX_CHOICES = 25
AUTOCOMPLETE_MIN_CHARS = 2
CHOICE_NAME_MAX_LENGTH = 100
# Discord drops an autocomplete answer that takes longer than 3 seconds.
AUTOCOMPLETE_TIMEOUT_SECONDS = 2.5

SEARCH_RESULT_LIMIT = 10
QUERY_ECHO_MAX_LENGTH = 100

IGDB_UNAVAILABLE = "⚠️ Non riesco a contattare IGDB in questo momento. Riprova tra poco."


def clamp_days(days: int) -> int:
    """Limit a requested window to the supported range."""
    return max(MIN_WINDOW_DAYS, min(days, MAX_WINDOW_DAYS))


def window_label(days: int) -> str:
    return "il prossimo giorno" if days == 1 else f"i prossimi {days} giorni"


def _choice_name(name: str) -> str:
    if len(name) <= CHOICE_NAME_MAX_LENGTH:
        return name
    return name[: CHOICE_NAME_MAX_LENGTH - 1].rstrip() + "…"


def pick_best_match(games: list[Game], query: str) -> Game | None:
    """Choose the game a typed name most likely refers to.

    An exact title match (ignoring case and punctuation) wins; otherwise the
    first result is kept, as IGDB lists the most relevant ones first.
    """
    if not games:
        return None
    wanted = normalize_title(query)
    for game in games:
        if wanted and normalize_title(game.name) == wanted:
            return game
    return games[0]


class ReleasesCog(commands.Cog):
    """Commands anyone can use to look up upcoming and released games."""

    def __init__(
        self,
        bot: GameReleasesBot,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.bot = bot
        self._clock = clock

    async def _guild_config(self, interaction: discord.Interaction) -> GuildConfig:
        """Return the guild's settings, or the defaults outside a configured guild."""
        guild_id = interaction.guild_id
        if guild_id is not None:
            config = await self.bot.repository.get_guild(guild_id)
            if config is not None:
                return config
        return GuildConfig(guild_id=guild_id or 0, timezone=self.bot.settings.timezone)

    async def _ranked_upcoming(
        self, config: GuildConfig, *, days: int, limit: int
    ) -> tuple[list[RankedGame], date]:
        """Rank the games out within ``days`` days; also return the local today."""
        now = self._clock()
        today = today_in(now, resolve_timezone(config.timezone))
        games = await self.bot.igdb.upcoming_games(
            start=today_in(now, UTC), days=days, include_dlc=config.include_dlc
        )
        ranked = rank_upcoming(
            games,
            today=today,
            preset=config.platform_preset,
            min_hypes=config.min_hypes,
            include_dlc=config.include_dlc,
            window_days=days,
            limit=limit,
        )
        return ranked, today

    @app_commands.command(
        name="nextreleases", description="Mostra le prossime uscite più attese"
    )
    @app_commands.describe(giorni="Quanti giorni guardare avanti (da 1 a 90)")
    async def nextreleases(
        self, interaction: discord.Interaction, giorni: int = DEFAULT_DAYS
    ) -> None:
        days = clamp_days(giorni)
        await interaction.response.defer()
        try:
            config = await self._guild_config(interaction)
            ranked, today = await self._ranked_upcoming(
                config, days=days, limit=TOP_LIST_SIZE
            )
        except (IgdbError, TwitchAuthError):
            logger.warning("Could not fetch upcoming games", exc_info=True)
            await interaction.followup.send(IGDB_UNAVAILABLE)
            return
        if not ranked:
            await interaction.followup.send(
                f"📭 Nessuna uscita in programma per {window_label(days)}."
            )
            return

        view = PaginatedReleasesView(ranked, today=today, owner_id=interaction.user.id)
        await interaction.followup.send(embed=view.payload.to_embed(), view=view)
        view.interaction = interaction

    @app_commands.command(
        name="calendar", description="Mostra il calendario delle prossime uscite"
    )
    @app_commands.describe(giorni="Quanti giorni guardare avanti (da 1 a 90)")
    async def calendar(
        self, interaction: discord.Interaction, giorni: int = DEFAULT_DAYS
    ) -> None:
        days = clamp_days(giorni)
        await interaction.response.defer()
        try:
            config = await self._guild_config(interaction)
            ranked, today = await self._ranked_upcoming(
                config, days=days, limit=CALENDAR_MAX_GAMES
            )
        except (IgdbError, TwitchAuthError):
            logger.warning("Could not fetch upcoming games", exc_info=True)
            await interaction.followup.send(IGDB_UNAVAILABLE)
            return

        data = build_calendar(ranked, today=today, days=days)
        embed = PanelPayload(title=data.title, description=data.description).to_embed()
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="game", description="Mostra la scheda di un gioco")
    @app_commands.describe(nome="Nome del gioco da cercare")
    async def game(self, interaction: discord.Interaction, nome: str) -> None:
        await interaction.response.defer()
        try:
            game = await self._resolve_game(nome)
            config = await self._guild_config(interaction)
        except (IgdbError, TwitchAuthError):
            logger.warning("Could not look up a game", exc_info=True)
            await interaction.followup.send(IGDB_UNAVAILABLE)
            return
        if game is None:
            shown = escape_markdown(nome.strip()[:QUERY_ECHO_MAX_LENGTH])
            await interaction.followup.send(
                f"❓ Non ho trovato nessun gioco per «{shown}»."
            )
            return

        today = today_in(self._clock(), resolve_timezone(config.timezone))
        payload = build_game_detail_payload(game, today=today)
        view = GameCardView(payload, timeout=CARD_TIMEOUT_SECONDS)
        await interaction.followup.send(embed=payload.to_embed(), view=view)

    @game.autocomplete("nome")
    async def game_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest games by name; the chosen value is the IGDB game ID."""
        query = current.strip()
        if len(query) < AUTOCOMPLETE_MIN_CHARS:
            return []
        try:
            async with asyncio.timeout(AUTOCOMPLETE_TIMEOUT_SECONDS):
                games = await self.bot.igdb.search_games(
                    query, limit=AUTOCOMPLETE_MAX_CHOICES
                )
        except (IgdbError, TwitchAuthError, TimeoutError):
            logger.info("Autocomplete lookup failed", exc_info=True)
            return []
        return [
            app_commands.Choice(name=_choice_name(game.name), value=str(game.id))
            for game in games[:AUTOCOMPLETE_MAX_CHOICES]
        ]

    async def _resolve_game(self, text: str) -> Game | None:
        """Find the game for an autocomplete selection or for typed text.

        A selection arrives as the numeric IGDB ID. Anything else, including a
        number that is not a known ID, is searched by name.
        """
        query = text.strip()
        if query.isascii() and query.isdecimal():
            found = await self.bot.igdb.get_games_by_ids([int(query)])
            if found:
                return found[0]
        results = await self.bot.igdb.search_games(query, limit=SEARCH_RESULT_LIMIT)
        return pick_best_match(results, query)


async def setup(bot: GameReleasesBot) -> None:
    await bot.add_cog(ReleasesCog(bot))
