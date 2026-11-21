"""Tests for panel payloads, persistent buttons and the Discord messenger."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from gamereleases.bot.reconciler import MessageForbidden, MessageNotFound
from gamereleases.bot.views import (
    MAX_STORE_BUTTONS,
    DiscordMessenger,
    GameCardView,
    PanelPayload,
    RemindButton,
    TrailerButton,
    build_game_card_payload,
    build_overview_payload,
    build_release_alert_embed,
)
from gamereleases.domain.formatting import hash_payload
from gamereleases.domain.models import (
    Game,
    Platform,
    PlatformPreset,
    RankedGame,
    StoreKind,
    StoreLink,
)
from gamereleases.igdb.client import IgdbError
from gamereleases.storage.db import Database
from gamereleases.storage.repository import Repository

TODAY = date(2026, 11, 21)
RELEASE = date(2026, 12, 1)


def make_game(game_id: int = 1, name: str = "Hollow Knight", **fields: Any) -> Game:
    return Game(id=game_id, name=name, slug=f"game-{game_id}", **fields)


def ranked(game: Game, release: date = RELEASE) -> RankedGame:
    return RankedGame(game=game, release_date=release)


class FakeIgdb:
    def __init__(self, *games: Game) -> None:
        self.games = {game.id: game for game in games}
        self.error: Exception | None = None

    async def get_games_by_ids(
        self, game_ids: Sequence[int], *, bypass_cache: bool = False
    ) -> list[Game]:
        if self.error is not None:
            raise self.error
        return [self.games[i] for i in game_ids if i in self.games]


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repository]:
    db = await Database.open(tmp_path / "views.db")
    yield Repository(db)
    await db.close()


def make_interaction(
    *, igdb: FakeIgdb, repo: Repository | None, user_id: int = 7
) -> MagicMock:
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.user.id = user_id
    interaction.guild_id = 100
    interaction.channel_id = 10
    interaction.client = (
        SimpleNamespace(repository=repo, igdb=igdb) if repo is not None else object()
    )
    return interaction


def reply_text(interaction: MagicMock) -> str:
    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
    return interaction.followup.send.await_args.args[0]


def http_error[E: discord.HTTPException](kind: type[E]) -> E:
    return kind(MagicMock(status=400, reason="error"), "error")


# -- custom_id parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    ("item", "custom_id", "game_id"),
    [(RemindButton, "remind:123", 123), (TrailerButton, "trailer:456", 456)],
)
async def test_dynamic_item_parses_custom_id(
    item: type[RemindButton | TrailerButton], custom_id: str, game_id: int
) -> None:
    match = item.__discord_ui_compiled_template__.fullmatch(custom_id)
    assert match is not None

    parsed = await item.from_custom_id(MagicMock(), MagicMock(), match)

    assert isinstance(parsed, item)
    assert parsed.game_id == game_id
    assert parsed.custom_id == custom_id


@pytest.mark.parametrize(
    ("item", "custom_id"),
    [
        (RemindButton, "remind:abc"),
        (RemindButton, "remind:"),
        (RemindButton, "trailer:1"),
        (TrailerButton, "trailer:x1"),
        (TrailerButton, "remind:1"),
    ],
)
def test_dynamic_item_rejects_foreign_custom_id(
    item: type[RemindButton | TrailerButton], custom_id: str
) -> None:
    assert item.__discord_ui_compiled_template__.fullmatch(custom_id) is None


# -- reminder button -----------------------------------------------------------


async def test_remind_button_toggles_and_replies_in_italian(repo: Repository) -> None:
    igdb = FakeIgdb(make_game(5, "Hades *II*"))
    button = RemindButton(5)

    first = make_interaction(igdb=igdb, repo=repo)
    await button.callback(first)
    assert reply_text(first) == "🔔 Ti avviserò all'uscita di **Hades \\*II\\***!"
    first.response.defer.assert_awaited_once_with(ephemeral=True)
    assert await repo.get_pending_reminder_game_ids() == [5]

    second = make_interaction(igdb=igdb, repo=repo)
    await button.callback(second)
    assert reply_text(second) == "🔕 Promemoria rimosso per **Hades \\*II\\***."
    assert await repo.get_pending_reminder_game_ids() == []


async def test_remind_button_stores_origin_for_fallback(repo: Repository) -> None:
    await RemindButton(5).callback(make_interaction(igdb=FakeIgdb(), repo=repo))

    [reminder] = await repo.get_due_reminders([5])
    assert (reminder.user_id, reminder.guild_id, reminder.channel_id) == (7, 100, 10)


async def test_remind_button_survives_igdb_failure(repo: Repository) -> None:
    igdb = FakeIgdb(make_game(5))
    igdb.error = IgdbError("down")
    interaction = make_interaction(igdb=igdb, repo=repo)

    await RemindButton(5).callback(interaction)

    assert reply_text(interaction) == "🔔 Ti avviserò all'uscita di **questo gioco**!"
    assert await repo.get_pending_reminder_game_ids() == [5]


async def test_buttons_report_missing_services() -> None:
    for button in (RemindButton(5), TrailerButton(5)):
        interaction = make_interaction(igdb=FakeIgdb(), repo=None)
        await button.callback(interaction)
        assert "non è ancora pronto" in reply_text(interaction)


# -- trailer button ------------------------------------------------------------


async def test_trailer_button_replies_with_link(repo: Repository) -> None:
    game = make_game(9, "Silksong", trailer_url="https://www.youtube.com/watch?v=abc")
    interaction = make_interaction(igdb=FakeIgdb(game), repo=repo)

    await TrailerButton(9).callback(interaction)

    assert reply_text(interaction) == (
        "🎬 Trailer di **Silksong**:\nhttps://www.youtube.com/watch?v=abc"
    )


async def test_trailer_button_without_trailer(repo: Repository) -> None:
    interaction = make_interaction(igdb=FakeIgdb(make_game(9, "Silksong")), repo=repo)

    await TrailerButton(9).callback(interaction)

    assert reply_text(interaction) == "🎬 Nessun trailer disponibile per **Silksong**."


async def test_trailer_button_unknown_game(repo: Repository) -> None:
    interaction = make_interaction(igdb=FakeIgdb(), repo=repo)

    await TrailerButton(9).callback(interaction)

    assert reply_text(interaction) == "❓ Non trovo questo gioco."


async def test_trailer_button_igdb_failure(repo: Repository) -> None:
    igdb = FakeIgdb(make_game(9))
    igdb.error = IgdbError("down")
    interaction = make_interaction(igdb=igdb, repo=repo)

    await TrailerButton(9).callback(interaction)

    assert "Riprova tra poco" in reply_text(interaction)


# -- payload builders and view -------------------------------------------------


def full_game() -> Game:
    return make_game(
        3,
        "Big Game",
        summary="A " + "very " * 100 + "long summary.",
        cover_url="https://img.example/cover.jpg",
        trailer_url="https://www.youtube.com/watch?v=abc",
        genres=("RPG", "Adventure"),
        platforms=(Platform(6, "PC (Microsoft Windows)", "PC"), Platform(167, "PS5")),
        total_rating=87.4,
        total_rating_count=12,
        stores=(
            StoreLink(StoreKind.STEAM, "https://store.steampowered.com/app/1"),
            StoreLink(StoreKind.EPIC, "https://store.epicgames.com/p/x"),
            StoreLink(StoreKind.GOG, "https://www.gog.com/game/x"),
            StoreLink(StoreKind.OFFICIAL, "https://example.com"),
            StoreLink(StoreKind.IGDB, "https://www.igdb.com/games/x"),
        ),
    )


def test_game_card_payload_content() -> None:
    payload = build_game_card_payload(ranked(full_game()), today=TODAY)

    assert payload.title == "Big Game"
    assert payload.url == "https://store.steampowered.com/app/1"
    assert payload.image_url == "https://img.example/cover.jpg"
    assert len(payload.description) <= 300
    assert payload.description.endswith("…")
    assert payload.game_id == 3
    assert payload.has_trailer
    assert [b.label for b in payload.store_buttons] == ["Steam", "Epic Games", "GOG"]
    values = {field.name: field.value for field in payload.fields}
    assert "Tra 10 giorni" in values["📅 Uscita"]
    assert values["🎮 Piattaforme"] == "PC, PS5"
    assert values["🏷️ Generi"] == "RPG, Adventure"
    assert values["⭐ Valutazione"] == "87/100"


def test_game_card_payload_minimal_game() -> None:
    payload = build_game_card_payload(ranked(make_game()), today=TODAY)

    assert payload.url is None
    assert payload.description == ""
    assert not payload.has_trailer
    assert payload.store_buttons == ()
    assert [f.name for f in payload.fields] == ["📅 Uscita"]
    embed = payload.to_embed()
    assert embed.title == "Hollow Knight"
    assert embed.image.url is None


def test_card_hash_changes_with_the_day_only() -> None:
    card = ranked(full_game())
    same = hash_payload(build_game_card_payload(card, today=TODAY))
    assert same == hash_payload(build_game_card_payload(card, today=TODAY))
    next_day = date(2026, 11, 22)
    assert same != hash_payload(build_game_card_payload(card, today=next_day))


def test_overview_payload_has_no_components() -> None:
    payload = build_overview_payload(
        [ranked(make_game())],
        synced_at=datetime(2026, 11, 21, 10, tzinfo=UTC),
        preset=PlatformPreset.PC_ONLY,
    )

    assert payload.title == "🎮 Prossime uscite"
    assert "Hollow Knight" in payload.description
    assert "Solo PC" in payload.description
    assert payload.to_view() is None


async def test_card_view_layout() -> None:
    payload = build_game_card_payload(ranked(full_game()), today=TODAY)
    view = payload.to_view()

    assert isinstance(view, GameCardView)
    assert view.timeout is None
    children = view.children
    assert len(children) == MAX_STORE_BUTTONS + 2
    links = [c for c in children if isinstance(c, discord.ui.Button)]
    assert [b.url for b in links] == [
        "https://store.steampowered.com/app/1",
        "https://store.epicgames.com/p/x",
        "https://www.gog.com/game/x",
    ]
    assert isinstance(children[-2], RemindButton)
    assert children[-2].custom_id == "remind:3"
    assert isinstance(children[-1], TrailerButton)
    assert children[-1].custom_id == "trailer:3"


async def test_card_view_omits_trailer_button_without_trailer() -> None:
    payload = build_game_card_payload(ranked(make_game(4)), today=TODAY)
    view = payload.to_view()

    assert view is not None
    assert [type(c) for c in view.children] == [RemindButton]


def test_release_alert_embed() -> None:
    embed = build_release_alert_embed(full_game())

    assert embed.title == "🎮 Esce oggi!"
    assert embed.description == "**Big Game** è disponibile da oggi."
    assert embed.url == "https://store.steampowered.com/app/1"
    assert embed.thumbnail.url == "https://img.example/cover.jpg"


# -- messenger -----------------------------------------------------------------


def make_messenger() -> tuple[DiscordMessenger, MagicMock, MagicMock]:
    """Return the messenger, its fake channel and the fake partial message."""
    message = MagicMock()
    message.edit = AsyncMock()
    message.delete = AsyncMock()
    channel = MagicMock()
    channel.send = AsyncMock(return_value=SimpleNamespace(id=4242))
    channel.get_partial_message.return_value = message
    client = MagicMock()
    client.get_partial_messageable.return_value = channel
    return DiscordMessenger(client), channel, message


CARD = PanelPayload(title="T", description="D", game_id=1)
OVERVIEW = PanelPayload(title="O", description="D")


async def test_messenger_send_returns_message_id() -> None:
    messenger, channel, _ = make_messenger()

    assert await messenger.send(10, CARD) == 4242

    kwargs = channel.send.await_args.kwargs
    assert kwargs["embed"].title == "T"
    assert isinstance(kwargs["view"], GameCardView)


async def test_messenger_send_without_components_passes_no_view() -> None:
    messenger, channel, _ = make_messenger()

    await messenger.send(10, OVERVIEW)

    assert "view" not in channel.send.await_args.kwargs


async def test_messenger_edit_replaces_embed_and_view() -> None:
    messenger, channel, message = make_messenger()

    await messenger.edit(10, 77, CARD)
    await messenger.edit(10, 78, OVERVIEW)

    channel.get_partial_message.assert_any_call(77)
    first, second = message.edit.await_args_list
    assert isinstance(first.kwargs["view"], GameCardView)
    assert second.kwargs["view"] is None


async def test_messenger_delete() -> None:
    messenger, channel, message = make_messenger()

    await messenger.delete(10, 77)

    channel.get_partial_message.assert_called_once_with(77)
    message.delete.assert_awaited_once()


@pytest.mark.parametrize(
    ("raised", "expected"),
    [(discord.NotFound, MessageNotFound), (discord.Forbidden, MessageForbidden)],
)
async def test_messenger_translates_discord_errors(
    raised: type[discord.HTTPException], expected: type[Exception]
) -> None:
    messenger, channel, message = make_messenger()
    channel.send.side_effect = http_error(raised)
    message.edit.side_effect = http_error(raised)
    message.delete.side_effect = http_error(raised)

    with pytest.raises(expected):
        await messenger.send(10, CARD)
    with pytest.raises(expected):
        await messenger.edit(10, 77, CARD)
    with pytest.raises(expected):
        await messenger.delete(10, 77)


async def test_messenger_lets_other_errors_through() -> None:
    messenger, channel, _ = make_messenger()
    channel.send.side_effect = http_error(discord.HTTPException)

    with pytest.raises(discord.HTTPException):
        await messenger.send(10, CARD)
