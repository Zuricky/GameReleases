"""Panel payloads, persistent buttons and the Discord message transport.

Payloads are plain frozen dataclasses, so they can be hashed for change
detection and built without a Discord connection. They are turned into embeds
and views only when a message is actually sent or edited.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

import discord

from gamereleases.bot.reconciler import MessageForbidden, MessageNotFound
from gamereleases.domain.formatting import (
    EmbedData,
    build_overview,
    discord_timestamp,
    escape_markdown,
    format_countdown,
    release_timestamp,
)
from gamereleases.domain.models import (
    Game,
    PlatformPreset,
    RankedGame,
    StoreKind,
    StoreLink,
)
from gamereleases.domain.ranking import resolve_release_date
from gamereleases.igdb.auth import TwitchAuthError
from gamereleases.igdb.client import IgdbClient, IgdbError
from gamereleases.storage.repository import Repository

logger = logging.getLogger(__name__)

EMBED_COLOR = 0x5865F2
ALERT_COLOR = 0x57F287

MAX_STORE_BUTTONS = 3
SUMMARY_MAX_LENGTH = 300
MAX_FIELD_ITEMS = 8

# Idle time after which the paging controls of a list are disabled.
PAGINATION_TIMEOUT_SECONDS = 180.0
# Idle time after which a one-off card stops tracking its buttons in memory.
CARD_TIMEOUT_SECONDS = 600.0

NAV_ROW = 0
STORE_ROW = 1
ACTION_ROW = 2

UNKNOWN_RELEASE_TEXT = "Data da definire"

REMIND_PATTERN = re.compile(r"remind:(?P<game_id>\d+)")
TRAILER_PATTERN = re.compile(r"trailer:(?P<game_id>\d+)")

UNKNOWN_GAME_NAME = "questo gioco"

STORE_LABELS: dict[StoreKind, str] = {
    StoreKind.STEAM: "Steam",
    StoreKind.EPIC: "Epic Games",
    StoreKind.GOG: "GOG",
    StoreKind.PLAYSTATION: "PlayStation Store",
    StoreKind.XBOX: "Xbox Store",
    StoreKind.NINTENDO: "Nintendo eShop",
    StoreKind.OFFICIAL: "Sito ufficiale",
    StoreKind.IGDB: "IGDB",
}


@dataclass(frozen=True, slots=True)
class FieldData:
    name: str
    value: str
    inline: bool = True


@dataclass(frozen=True, slots=True)
class StoreButtonData:
    label: str
    url: str


@dataclass(frozen=True, slots=True)
class PanelPayload:
    """Everything needed to render one panel message.

    ``game_id`` is set on game cards, which carry the reminder button (and the
    trailer button when ``has_trailer`` is set) after the store links.
    """

    title: str
    description: str
    url: str | None = None
    image_url: str | None = None
    fields: tuple[FieldData, ...] = ()
    store_buttons: tuple[StoreButtonData, ...] = ()
    game_id: int | None = None
    has_trailer: bool = False

    def to_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.title,
            description=self.description,
            url=self.url,
            colour=EMBED_COLOR,
        )
        if self.image_url is not None:
            embed.set_image(url=self.image_url)
        for field in self.fields:
            embed.add_field(name=field.name, value=field.value, inline=field.inline)
        return embed

    def to_view(self) -> discord.ui.View | None:
        """Build the buttons, or ``None`` for a message without components."""
        if self.game_id is None and not self.store_buttons:
            return None
        return GameCardView(self)


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _join_limited(items: Sequence[str]) -> str:
    shown = [escape_markdown(item) for item in items[:MAX_FIELD_ITEMS]]
    extra = len(items) - len(shown)
    if extra > 0:
        shown.append(f"+{extra}")
    return ", ".join(shown)


def _platform_names(game: Game) -> list[str]:
    names = [
        platform.abbreviation or platform.name
        for platform in game.platforms
        if platform.abbreviation or platform.name
    ]
    return list(dict.fromkeys(names))


def _store_buttons(stores: Sequence[StoreLink]) -> tuple[StoreButtonData, ...]:
    return tuple(
        StoreButtonData(label=STORE_LABELS[store.kind], url=store.url)
        for store in stores[:MAX_STORE_BUTTONS]
    )


def _embed_payload(data: EmbedData) -> PanelPayload:
    return PanelPayload(title=data.title, description=data.description, url=data.url)


def build_overview_payload(
    ranked: Sequence[RankedGame],
    *,
    synced_at: datetime,
    preset: PlatformPreset,
) -> PanelPayload:
    """Build the compact top list shown above the game cards."""
    return _embed_payload(build_overview(ranked, synced_at=synced_at, preset=preset))


def _card_payload(game: Game, release: str) -> PanelPayload:
    fields = [FieldData("📅 Uscita", release)]
    platforms = _platform_names(game)
    if platforms:
        fields.append(FieldData("🎮 Piattaforme", _join_limited(platforms)))
    if game.genres:
        fields.append(FieldData("🏷️ Generi", _join_limited(game.genres)))
    if game.total_rating is not None and game.total_rating_count > 0:
        fields.append(FieldData("⭐ Valutazione", f"{game.total_rating:.0f}/100"))

    return PanelPayload(
        title=escape_markdown(game.name),
        description=_truncate(game.summary, SUMMARY_MAX_LENGTH) if game.summary else "",
        url=game.stores[0].url if game.stores else None,
        image_url=game.cover_url,
        fields=tuple(fields),
        store_buttons=_store_buttons(game.stores),
        game_id=game.id,
        has_trailer=game.trailer_url is not None,
    )


def _release_text(release_date: date, today: date) -> str:
    return (
        f"{discord_timestamp(release_timestamp(release_date), 'D')}\n"
        f"{format_countdown(release_date, today)}"
    )


def build_game_card_payload(ranked: RankedGame, *, today: date) -> PanelPayload:
    """Build the rich card of one upcoming game.

    ``today`` is the current date in the guild's time zone, so the countdown
    (and with it the payload hash) only changes when the day does.
    """
    return _card_payload(ranked.game, _release_text(ranked.release_date, today))


def build_game_detail_payload(game: Game, *, today: date) -> PanelPayload:
    """Build the card of any game, including released ones and undated ones.

    A game without an exact release day shows IGDB's own wording (for example a
    month or a quarter) instead of a countdown.
    """
    release_date = resolve_release_date(game)
    if release_date is not None:
        return _card_payload(game, _release_text(release_date, today))
    human = next((entry.human for entry in game.release_dates if entry.human), None)
    return _card_payload(
        game, escape_markdown(human) if human else UNKNOWN_RELEASE_TEXT
    )


def build_release_alert_embed(game: Game) -> discord.Embed:
    """Build the "out today" announcement for a game."""
    embed = discord.Embed(
        title="🎮 Esce oggi!",
        description=f"**{escape_markdown(game.name)}** è disponibile da oggi.",
        url=game.stores[0].url if game.stores else None,
        colour=ALERT_COLOR,
    )
    if game.cover_url is not None:
        embed.set_thumbnail(url=game.cover_url)
    platforms = _platform_names(game)
    if platforms:
        embed.add_field(name="🎮 Piattaforme", value=_join_limited(platforms))
    return embed


@runtime_checkable
class BotServices(Protocol):
    """The shared services the buttons read from ``interaction.client``."""

    repository: Repository
    igdb: IgdbClient


def _services(interaction: discord.Interaction[Any]) -> BotServices | None:
    client = interaction.client
    return client if isinstance(client, BotServices) else None


async def _find_game(igdb: IgdbClient, game_id: int) -> Game | None:
    games = await igdb.get_games_by_ids([game_id])
    return games[0] if games else None


async def _reply(interaction: discord.Interaction[Any], content: str) -> None:
    """Answer an interaction that was deferred, visible only to the user."""
    await interaction.followup.send(content, ephemeral=True)


class RemindButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]], template=REMIND_PATTERN
):
    """Toggles the clicking user's release reminder for one game."""

    def __init__(self, game_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Avvisami",
                emoji="🔔",
                style=discord.ButtonStyle.primary,
                custom_id=f"remind:{game_id}",
            )
        )
        self.game_id = game_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[Any],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> RemindButton:
        return cls(int(match["game_id"]))

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        await interaction.response.defer(ephemeral=True)
        services = _services(interaction)
        if services is None:
            await _reply(interaction, "⚠️ Il bot non è ancora pronto, riprova tra poco.")
            return

        subscribed = await services.repository.toggle_reminder(
            interaction.user.id,
            self.game_id,
            interaction.guild_id,
            interaction.channel_id,
        )
        # The title is only decoration: a failed lookup must not undo the toggle.
        name = UNKNOWN_GAME_NAME
        try:
            game = await _find_game(services.igdb, self.game_id)
        except (IgdbError, TwitchAuthError):
            logger.warning("Could not look up game %d for a reminder", self.game_id)
        else:
            if game is not None:
                name = game.name
        title = f"**{escape_markdown(name)}**"
        if subscribed:
            await _reply(interaction, f"🔔 Ti avviserò all'uscita di {title}!")
        else:
            await _reply(interaction, f"🔕 Promemoria rimosso per {title}.")


class TrailerButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]], template=TRAILER_PATTERN
):
    """Replies with the game's trailer link, visible only to the clicking user."""

    def __init__(self, game_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Trailer",
                emoji="🎬",
                style=discord.ButtonStyle.secondary,
                custom_id=f"trailer:{game_id}",
            )
        )
        self.game_id = game_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[Any],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> TrailerButton:
        return cls(int(match["game_id"]))

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        await interaction.response.defer(ephemeral=True)
        services = _services(interaction)
        if services is None:
            await _reply(interaction, "⚠️ Il bot non è ancora pronto, riprova tra poco.")
            return
        try:
            game = await _find_game(services.igdb, self.game_id)
        except (IgdbError, TwitchAuthError):
            logger.warning("Could not look up game %d for a trailer", self.game_id)
            await _reply(
                interaction,
                "⚠️ Non riesco a recuperare il trailer in questo momento. "
                "Riprova tra poco.",
            )
            return
        if game is None:
            await _reply(interaction, "❓ Non trovo questo gioco.")
        elif game.trailer_url is None:
            title = escape_markdown(game.name)
            await _reply(interaction, f"🎬 Nessun trailer disponibile per **{title}**.")
        else:
            title = escape_markdown(game.name)
            await _reply(interaction, f"🎬 Trailer di **{title}**:\n{game.trailer_url}")


def _add_game_buttons(
    view: discord.ui.View, payload: PanelPayload, *, row: int | None
) -> None:
    """Add the reminder and trailer buttons of a game card to ``view``."""
    if payload.game_id is None:
        return
    remind = RemindButton(payload.game_id)
    remind.row = row
    view.add_item(remind)
    if payload.has_trailer:
        trailer = TrailerButton(payload.game_id)
        trailer.row = row
        view.add_item(trailer)


class GameCardView(discord.ui.View):
    """Store links, then the reminder and trailer buttons, in one row.

    Panel messages keep the default ``timeout=None``, which keeps the view
    alive for the process lifetime. One-off replies should pass a timeout so
    the view is dropped; the buttons keep working either way, as they are
    resolved from their ``custom_id``.
    """

    def __init__(self, payload: PanelPayload, *, timeout: float | None = None) -> None:
        super().__init__(timeout=timeout)
        for store in payload.store_buttons[:MAX_STORE_BUTTONS]:
            self.add_item(discord.ui.Button(label=store.label, url=store.url))
        _add_game_buttons(self, payload, row=None)


class _StepButton(discord.ui.Button["PaginatedReleasesView"]):
    def __init__(self, label: str, step: int) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=NAV_ROW)
        self._step = step

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        view = self.view
        if view is not None:
            await view.turn(interaction, self._step)


class PaginatedReleasesView(discord.ui.View):
    """Shows one game at a time, with paging, store links and card buttons.

    Rows: paging controls, then store links, then the reminder and trailer
    buttons of the current game. Paging wraps around at both ends. Only the
    user who ran the command can page, as the message is shared. When the view
    times out the paging controls are disabled, while the store links and the
    reminder and trailer buttons stay usable.
    """

    def __init__(
        self,
        items: Sequence[RankedGame],
        *,
        today: date,
        owner_id: int,
        timeout: float = PAGINATION_TIMEOUT_SECONDS,
    ) -> None:
        if not items:
            raise ValueError("a paginated view needs at least one game")
        super().__init__(timeout=timeout)
        self.items = tuple(items)
        self.today = today
        self.owner_id = owner_id
        self.page = 0
        # Set once the reply has been sent; used to disable the controls.
        self.interaction: discord.Interaction[Any] | None = None
        self._previous = _StepButton("◀️ Indietro", -1)
        self._indicator: discord.ui.Button[Any] = discord.ui.Button(
            label="", style=discord.ButtonStyle.secondary, disabled=True, row=NAV_ROW
        )
        self._next = _StepButton("Avanti ▶️", 1)
        self._render()

    @property
    def payload(self) -> PanelPayload:
        return build_game_card_payload(self.items[self.page], today=self.today)

    def _render(self) -> None:
        """Rebuild the buttons for the current page."""
        payload = self.payload
        multiple = len(self.items) > 1
        self._previous.disabled = not multiple
        self._next.disabled = not multiple
        self._indicator.label = f"{self.page + 1} / {len(self.items)}"

        self.clear_items()
        self.add_item(self._previous)
        self.add_item(self._indicator)
        self.add_item(self._next)
        for store in payload.store_buttons[:MAX_STORE_BUTTONS]:
            self.add_item(
                discord.ui.Button(label=store.label, url=store.url, row=STORE_ROW)
            )
        _add_game_buttons(self, payload, row=ACTION_ROW)

    async def turn(self, interaction: discord.Interaction[Any], step: int) -> None:
        """Move ``step`` pages, wrapping around, and show the new page."""
        self.page = (self.page + step) % len(self.items)
        self._render()
        await interaction.response.edit_message(
            embed=self.payload.to_embed(), view=self
        )

    async def interaction_check(self, interaction: discord.Interaction[Any]) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "⚠️ Solo chi ha usato il comando può sfogliare l'elenco. "
            "Usa /nextreleases per averne uno tuo.",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        self._previous.disabled = True
        self._next.disabled = True
        if self.interaction is None:
            return
        try:
            await self.interaction.edit_original_response(view=self)
        except discord.HTTPException:
            logger.debug("Could not disable the paging controls", exc_info=True)


class DiscordMessenger:
    """Posts, edits and deletes panel messages through the Discord API.

    The transport is stateless: change detection by payload hash is done by the
    reconciler, which only calls ``edit`` for a payload that actually changed.
    """

    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def send(self, channel_id: int, payload: PanelPayload) -> int:
        channel = self._client.get_partial_messageable(channel_id)
        view = payload.to_view()
        try:
            if view is None:
                message = await channel.send(embed=payload.to_embed())
            else:
                message = await channel.send(embed=payload.to_embed(), view=view)
        except discord.NotFound as exc:
            raise MessageNotFound from exc
        except discord.Forbidden as exc:
            raise MessageForbidden from exc
        return message.id

    async def edit(
        self, channel_id: int, message_id: int, payload: PanelPayload
    ) -> None:
        message = self._client.get_partial_messageable(channel_id).get_partial_message(
            message_id
        )
        try:
            await message.edit(embed=payload.to_embed(), view=payload.to_view())
        except discord.NotFound as exc:
            raise MessageNotFound from exc
        except discord.Forbidden as exc:
            raise MessageForbidden from exc

    async def delete(self, channel_id: int, message_id: int) -> None:
        message = self._client.get_partial_messageable(channel_id).get_partial_message(
            message_id
        )
        try:
            await message.delete()
        except discord.NotFound as exc:
            raise MessageNotFound from exc
        except discord.Forbidden as exc:
            raise MessageForbidden from exc
