"""Tests for the release browsing commands and the paginated list view."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from gamereleases.bot.app import GameReleasesBot
from gamereleases.bot.cogs import releases as releases_module
from gamereleases.bot.cogs.releases import (
    AUTOCOMPLETE_MAX_CHOICES,
    IGDB_UNAVAILABLE,
    ReleasesCog,
    clamp_days,
    pick_best_match,
    setup,
)
from gamereleases.bot.views import (
    GameCardView,
    PaginatedReleasesView,
    RemindButton,
    TrailerButton,
    build_game_detail_payload,
)
from gamereleases.config import Settings
from gamereleases.domain.models import (
    Game,
    GuildConfig,
    Platform,
    PlatformPreset,
    PlatformRelease,
    RankedGame,
    StoreKind,
    StoreLink,
)
from gamereleases.igdb.auth import TwitchAuthError
from gamereleases.igdb.client import IgdbError
from gamereleases.storage.db import Database
from gamereleases.storage.repository import Repository

GUILD = 100
USER = 7
NOW = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)
TODAY = NOW.date()

PC = Platform(id=6, name="PC (Microsoft Windows)", abbreviation="PC")
PS5 = Platform(id=167, name="PlayStation 5", abbreviation="PS5")


async def invoke(command: object, *args: Any, **kwargs: Any) -> None:
    """Run a slash command's callback directly, as the library would."""
    await cast(Any, command).callback(*args, **kwargs)


def midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def make_game(
    game_id: int,
    name: str = "Game",
    days_ahead: int | None = 5,
    *,
    hypes: int = 10,
    platforms: tuple[Platform, ...] = (PC,),
    **fields: Any,
) -> Game:
    release = None if days_ahead is None else midnight(TODAY + timedelta(days_ahead))
    return Game(
        id=game_id,
        name=name,
        slug=f"game-{game_id}",
        first_release_date=release,
        hypes=hypes,
        platforms=platforms,
        **fields,
    )


def ranked(game: Game) -> RankedGame:
    assert game.first_release_date is not None
    return RankedGame(game=game, release_date=game.first_release_date.date())


class FakeIgdb:
    def __init__(self) -> None:
        self.catalog: list[Game] = []
        self.search_results: list[Game] = []
        self.by_id: dict[int, Game] = {}
        self.error: Exception | None = None
        self.search_delay = 0.0
        self.upcoming_calls: list[dict[str, Any]] = []
        self.search_calls: list[tuple[str, int]] = []
        self.id_calls: list[list[int]] = []

    async def upcoming_games(
        self, *, start: date, days: int, include_dlc: bool = False, **_: Any
    ) -> list[Game]:
        self.upcoming_calls.append(
            {"start": start, "days": days, "include_dlc": include_dlc}
        )
        if self.error is not None:
            raise self.error
        return list(self.catalog)

    async def search_games(self, text: str, *, limit: int = 10, **_: Any) -> list[Game]:
        self.search_calls.append((text, limit))
        if self.search_delay:
            await asyncio.sleep(self.search_delay)
        if self.error is not None:
            raise self.error
        return self.search_results[:limit]

    async def get_games_by_ids(self, game_ids: Sequence[int], **_: Any) -> list[Game]:
        self.id_calls.append(list(game_ids))
        if self.error is not None:
            raise self.error
        return [self.by_id[i] for i in game_ids if i in self.by_id]


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repository]:
    db = await Database.open(tmp_path / "releases.db")
    yield Repository(db)
    await db.close()


@pytest.fixture
def igdb() -> FakeIgdb:
    return FakeIgdb()


@pytest.fixture
def cog(repo: Repository, igdb: FakeIgdb) -> ReleasesCog:
    bot = SimpleNamespace(
        repository=repo, igdb=igdb, settings=SimpleNamespace(timezone="Europe/Rome")
    )
    return ReleasesCog(cast(Any, bot), clock=lambda: NOW)


def make_interaction(*, guild_id: int | None = GUILD, user_id: int = USER) -> MagicMock:
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.user.id = user_id
    interaction.response.defer = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def sent(interaction: MagicMock) -> dict[str, Any]:
    """The keyword arguments of the single follow-up message."""
    interaction.followup.send.assert_awaited_once()
    return dict(interaction.followup.send.await_args.kwargs)


async def run_nextreleases(
    cog: ReleasesCog, interaction: MagicMock, days: int = 30
) -> None:
    await invoke(cog.nextreleases, cog, interaction, days)


# -- helpers -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(-10, 1), (0, 1), (1, 1), (30, 30), (90, 90), (91, 90), (10_000, 90)],
)
def test_clamp_days(requested: int, expected: int) -> None:
    assert clamp_days(requested) == expected


def test_pick_best_match_prefers_an_exact_title() -> None:
    games = [make_game(1, "Hollow Knight: Silksong"), make_game(2, "Hollow Knight")]

    assert pick_best_match(games, "hollow  knight!") == games[1]


def test_pick_best_match_falls_back_to_the_first_result() -> None:
    games = [make_game(1, "Hollow Knight: Silksong"), make_game(2, "Hollow Night")]

    assert pick_best_match(games, "hollow") == games[0]
    assert pick_best_match([], "hollow") is None


# -- /nextreleases -------------------------------------------------------------


@pytest.mark.parametrize(("requested", "fetched"), [(0, 1), (-3, 1), (7, 7), (500, 90)])
async def test_nextreleases_clamps_the_window(
    cog: ReleasesCog, igdb: FakeIgdb, requested: int, fetched: int
) -> None:
    igdb.catalog = [make_game(1)]

    await run_nextreleases(cog, make_interaction(), requested)

    assert igdb.upcoming_calls[0]["days"] == fetched
    assert igdb.upcoming_calls[0]["start"] == TODAY


async def test_nextreleases_defaults_to_thirty_days(
    cog: ReleasesCog, igdb: FakeIgdb
) -> None:
    igdb.catalog = [make_game(1)]
    interaction = make_interaction()

    await invoke(cog.nextreleases, cog, interaction)

    assert igdb.upcoming_calls[0]["days"] == 30
    interaction.response.defer.assert_awaited_once()


async def test_nextreleases_shows_the_top_ten_in_release_order(
    cog: ReleasesCog, igdb: FakeIgdb
) -> None:
    # Twelve games; the two least hyped ones must be dropped.
    igdb.catalog = [
        make_game(i, f"Game {i}", days_ahead=13 - i, hypes=i * 10) for i in range(1, 13)
    ]
    interaction = make_interaction()

    await run_nextreleases(cog, interaction)

    view = sent(interaction)["view"]
    assert isinstance(view, PaginatedReleasesView)
    assert len(view.items) == 10
    assert {item.game.id for item in view.items} == set(range(3, 13))
    dates = [item.release_date for item in view.items]
    assert dates == sorted(dates)
    assert view.owner_id == USER
    assert view.interaction is interaction
    embed = sent(interaction)["embed"]
    assert embed.title == view.items[0].game.name


async def test_nextreleases_excludes_games_outside_the_window(
    cog: ReleasesCog, igdb: FakeIgdb
) -> None:
    igdb.catalog = [make_game(1, "Near", 5), make_game(2, "Far", 45)]
    interaction = make_interaction()

    await run_nextreleases(cog, interaction, 30)

    assert [item.game.name for item in sent(interaction)["view"].items] == ["Near"]


async def test_nextreleases_uses_the_guild_platform_preset(
    cog: ReleasesCog, igdb: FakeIgdb, repo: Repository
) -> None:
    await repo.upsert_guild(
        GuildConfig(guild_id=GUILD, platform_preset=PlatformPreset.PC_ONLY)
    )
    igdb.catalog = [
        make_game(1, "On PC", platforms=(PC,)),
        make_game(2, "Console", platforms=(PS5,)),
    ]
    interaction = make_interaction()

    await run_nextreleases(cog, interaction)

    assert [item.game.name for item in sent(interaction)["view"].items] == ["On PC"]


@pytest.mark.parametrize("guild_id", [None, GUILD + 1])
async def test_nextreleases_uses_the_default_preset_without_configuration(
    cog: ReleasesCog, igdb: FakeIgdb, guild_id: int | None
) -> None:
    igdb.catalog = [
        make_game(1, "On PC", platforms=(PC,)),
        make_game(2, "Console", platforms=(PS5,)),
    ]
    interaction = make_interaction(guild_id=guild_id)

    await run_nextreleases(cog, interaction)

    assert len(sent(interaction)["view"].items) == 2


async def test_nextreleases_with_nothing_upcoming(
    cog: ReleasesCog, igdb: FakeIgdb
) -> None:
    interaction = make_interaction()

    await run_nextreleases(cog, interaction, 1)

    kwargs = sent(interaction)
    assert "view" not in kwargs
    assert "il prossimo giorno" in interaction.followup.send.await_args.args[0]


@pytest.mark.parametrize("error", [IgdbError("down"), TwitchAuthError("no token")])
async def test_nextreleases_reports_an_igdb_failure(
    cog: ReleasesCog, igdb: FakeIgdb, error: Exception
) -> None:
    igdb.error = error
    interaction = make_interaction()

    await run_nextreleases(cog, interaction)

    interaction.followup.send.assert_awaited_once_with(IGDB_UNAVAILABLE)


# -- paginated view ------------------------------------------------------------


def make_view(
    count: int = 3, *, owner: int = USER, **fields: Any
) -> PaginatedReleasesView:
    games = [
        make_game(i, f"Game {i}", days_ahead=i, **fields) for i in range(1, count + 1)
    ]
    return PaginatedReleasesView(
        [ranked(game) for game in games], today=TODAY, owner_id=owner
    )


def row(view: PaginatedReleasesView, index: int) -> list[Any]:
    return [child for child in view.children if child.row == index]


def three_stores() -> tuple[StoreLink, ...]:
    return (
        StoreLink(StoreKind.STEAM, "https://store.steampowered.com/app/1"),
        StoreLink(StoreKind.EPIC, "https://store.epicgames.com/p/g"),
        StoreLink(StoreKind.GOG, "https://www.gog.com/game/g"),
        StoreLink(StoreKind.OFFICIAL, "https://example.com"),
    )


def test_view_layout() -> None:
    view = make_view(
        stores=three_stores(), trailer_url="https://www.youtube.com/watch?v=x"
    )

    previous, indicator, following = row(view, 0)
    assert previous.label == "◀️ Indietro"
    assert indicator.label == "1 / 3"
    assert indicator.disabled
    assert following.label == "Avanti ▶️"
    assert not previous.disabled
    assert not following.disabled

    stores = row(view, 1)
    assert [button.label for button in stores] == [
        "Steam",
        "Epic Games",
        "GOG",
    ]  # at most three, in priority order
    assert all(button.url for button in stores)

    remind, trailer = row(view, 2)
    assert isinstance(remind, RemindButton)
    assert remind.game_id == 1
    assert isinstance(trailer, TrailerButton)
    assert trailer.game_id == 1


def test_view_without_trailer_or_stores() -> None:
    view = make_view()

    assert row(view, 1) == []
    [remind] = row(view, 2)
    assert isinstance(remind, RemindButton)


async def test_view_pages_forward_and_backward() -> None:
    view = make_view()
    interaction = make_interaction()

    await view.turn(interaction, 1)

    assert view.page == 1
    assert row(view, 0)[1].label == "2 / 3"
    kwargs = interaction.response.edit_message.await_args.kwargs
    assert kwargs["embed"].title == "Game 2"
    assert kwargs["view"] is view
    assert row(view, 2)[0].game_id == 2

    await view.turn(interaction, -1)

    assert view.page == 0
    assert row(view, 0)[1].label == "1 / 3"


async def test_view_wraps_around_at_both_ends() -> None:
    view = make_view()
    interaction = make_interaction()

    await view.turn(interaction, -1)
    assert view.page == 2
    assert row(view, 0)[1].label == "3 / 3"

    await view.turn(interaction, 1)
    assert view.page == 0


async def test_view_buttons_drive_the_paging() -> None:
    view = make_view()
    interaction = make_interaction()
    _, _, following = row(view, 0)

    await following.callback(interaction)
    await following.callback(interaction)

    assert view.page == 2
    previous = row(view, 0)[0]
    await previous.callback(interaction)
    assert view.page == 1


async def test_view_replaces_store_buttons_when_the_page_changes() -> None:
    games = [
        make_game(1, "With stores", 1, stores=three_stores()),
        make_game(2, "Without", 2),
    ]
    view = PaginatedReleasesView(
        [ranked(game) for game in games], today=TODAY, owner_id=USER
    )
    assert len(row(view, 1)) == 3

    await view.turn(make_interaction(), 1)

    assert row(view, 1) == []


def test_single_page_view_disables_paging() -> None:
    view = make_view(1)

    previous, indicator, following = row(view, 0)
    assert indicator.label == "1 / 1"
    assert previous.disabled
    assert following.disabled


def test_view_requires_games() -> None:
    with pytest.raises(ValueError, match="at least one"):
        PaginatedReleasesView([], today=TODAY, owner_id=USER)


async def test_only_the_owner_can_page() -> None:
    view = make_view(owner=USER)
    stranger = make_interaction(user_id=USER + 1)

    assert not await view.interaction_check(stranger)
    stranger.response.send_message.assert_awaited_once()
    assert stranger.response.send_message.await_args.kwargs["ephemeral"] is True

    assert await view.interaction_check(make_interaction(user_id=USER))


async def test_timeout_disables_paging_but_keeps_the_other_buttons() -> None:
    view = make_view(
        stores=three_stores(), trailer_url="https://www.youtube.com/watch?v=x"
    )
    view.interaction = make_interaction()

    await view.on_timeout()

    previous, indicator, following = row(view, 0)
    assert previous.disabled
    assert indicator.disabled
    assert following.disabled
    stores = row(view, 1)
    assert len(stores) == 3
    assert all(not button.disabled and button.url for button in stores)
    remind, trailer = row(view, 2)
    assert isinstance(remind, RemindButton)
    assert isinstance(trailer, TrailerButton)
    assert not remind.item.disabled
    assert not trailer.item.disabled
    view.interaction.edit_original_response.assert_awaited_once_with(view=view)


async def test_timeout_survives_a_deleted_message() -> None:
    view = make_view()
    view.interaction = make_interaction()
    view.interaction.edit_original_response.side_effect = discord.NotFound(
        MagicMock(status=404, reason="Not Found"), "gone"
    )

    await view.on_timeout()

    assert row(view, 0)[0].disabled


async def test_timeout_before_the_reply_is_sent_only_disables() -> None:
    view = make_view()

    await view.on_timeout()

    assert row(view, 0)[2].disabled


# -- /calendar -----------------------------------------------------------------


async def test_calendar_groups_games_by_day(cog: ReleasesCog, igdb: FakeIgdb) -> None:
    igdb.catalog = [
        make_game(1, "Later", 10),
        make_game(2, "Sooner", 3),
        make_game(3, "Same day", 3, hypes=1),
    ]
    interaction = make_interaction()

    await invoke(cog.calendar, cog, interaction, 30)

    embed = sent(interaction)["embed"]
    assert embed.title == "📅 Calendario uscite (prossimi 30 giorni)"
    description = embed.description
    assert description.index("Sooner") < description.index("Same day")
    assert description.index("Same day") < description.index("Later")
    assert "Tra 3 giorni" in description
    assert "Tra 10 giorni" in description
    # One header per release day, so two for these three games.
    assert description.count("—") == 2


async def test_calendar_clamps_and_names_the_window(
    cog: ReleasesCog, igdb: FakeIgdb
) -> None:
    interaction = make_interaction()

    await invoke(cog.calendar, cog, interaction, 500)

    assert igdb.upcoming_calls[0]["days"] == 90
    assert "prossimi 90 giorni" in sent(interaction)["embed"].title


async def test_calendar_without_results(cog: ReleasesCog, igdb: FakeIgdb) -> None:
    interaction = make_interaction()

    await invoke(cog.calendar, cog, interaction, 1)

    embed = sent(interaction)["embed"]
    assert embed.title == "📅 Calendario uscite (prossimo giorno)"
    assert embed.description == "Nessuna uscita in programma."


async def test_calendar_reports_an_igdb_failure(
    cog: ReleasesCog, igdb: FakeIgdb
) -> None:
    igdb.error = IgdbError("down")
    interaction = make_interaction()

    await invoke(cog.calendar, cog, interaction, 30)

    interaction.followup.send.assert_awaited_once_with(IGDB_UNAVAILABLE)


# -- /game autocomplete --------------------------------------------------------


@pytest.mark.parametrize("current", ["", "a", "  a  ", " "])
async def test_autocomplete_needs_two_characters(
    cog: ReleasesCog, igdb: FakeIgdb, current: str
) -> None:
    igdb.search_results = [make_game(1)]

    assert await cast(Any, cog).game_autocomplete(make_interaction(), current) == []
    assert igdb.search_calls == []


async def test_autocomplete_returns_ids_as_values(
    cog: ReleasesCog, igdb: FakeIgdb
) -> None:
    igdb.search_results = [
        make_game(11, "Hollow Knight"),
        make_game(22, "Hollow Night"),
    ]

    choices = await cast(Any, cog).game_autocomplete(make_interaction(), " hollow ")

    assert [(c.name, c.value) for c in choices] == [
        ("Hollow Knight", "11"),
        ("Hollow Night", "22"),
    ]
    assert igdb.search_calls == [("hollow", AUTOCOMPLETE_MAX_CHOICES)]


async def test_autocomplete_offers_at_most_25_choices_of_100_characters(
    cog: ReleasesCog, igdb: FakeIgdb
) -> None:
    igdb.search_results = [make_game(i, "N" * 150) for i in range(1, 40)]

    choices = await cast(Any, cog).game_autocomplete(make_interaction(), "nn")

    assert len(choices) == 25
    assert all(len(choice.name) <= 100 for choice in choices)
    assert choices[0].name.endswith("…")


@pytest.mark.parametrize("error", [IgdbError("down"), TwitchAuthError("no token")])
async def test_autocomplete_survives_igdb_errors(
    cog: ReleasesCog, igdb: FakeIgdb, error: Exception
) -> None:
    igdb.error = error

    assert await cast(Any, cog).game_autocomplete(make_interaction(), "zelda") == []


async def test_autocomplete_gives_up_before_discord_does(
    cog: ReleasesCog, igdb: FakeIgdb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(releases_module, "AUTOCOMPLETE_TIMEOUT_SECONDS", 0.01)
    igdb.search_delay = 1.0
    igdb.search_results = [make_game(1)]

    assert await cast(Any, cog).game_autocomplete(make_interaction(), "zelda") == []


# -- /game ---------------------------------------------------------------------


def rich_game(game_id: int = 42, name: str = "Hollow Knight") -> Game:
    return make_game(
        game_id,
        name,
        10,
        summary="A bug adventure.",
        total_rating=91.4,
        total_rating_count=120,
        genres=("Metroidvania",),
        platforms=(PC, PS5),
        stores=three_stores(),
        trailer_url="https://www.youtube.com/watch?v=abc",
    )


def field_values(embed: discord.Embed) -> dict[str, str]:
    return {field.name or "": field.value or "" for field in embed.fields}


async def test_game_by_selected_id(cog: ReleasesCog, igdb: FakeIgdb) -> None:
    igdb.by_id[42] = rich_game()
    interaction = make_interaction()

    await invoke(cog.game, cog, interaction, "42")

    assert igdb.id_calls == [[42]]
    assert igdb.search_calls == []
    kwargs = sent(interaction)
    embed = kwargs["embed"]
    assert embed.title == "Hollow Knight"
    fields = field_values(embed)
    assert "Tra 10 giorni" in fields["📅 Uscita"]
    assert fields["⭐ Valutazione"] == "91/100"
    assert fields["🎮 Piattaforme"] == "PC, PS5"
    assert fields["🏷️ Generi"] == "Metroidvania"

    view = kwargs["view"]
    assert isinstance(view, GameCardView)
    assert view.timeout is not None  # one-off cards must not live forever
    kinds = [type(child) for child in cast(Any, view).children]
    assert kinds.count(RemindButton) == 1
    assert kinds.count(TrailerButton) == 1
    urls = [c.url for c in view.children if isinstance(c, discord.ui.Button)]
    assert len(urls) == 3


async def test_game_by_typed_text_picks_the_exact_match(
    cog: ReleasesCog, igdb: FakeIgdb
) -> None:
    igdb.search_results = [
        make_game(1, "Hollow Knight: Silksong"),
        rich_game(2, "Hollow Knight"),
    ]
    interaction = make_interaction()

    await invoke(cog.game, cog, interaction, "hollow knight")

    assert igdb.id_calls == []
    assert igdb.search_calls[0][0] == "hollow knight"
    assert sent(interaction)["embed"].title == "Hollow Knight"


async def test_game_by_typed_text_falls_back_to_the_first_result(
    cog: ReleasesCog, igdb: FakeIgdb
) -> None:
    igdb.search_results = [make_game(1, "Zelda: Echoes"), make_game(2, "Zelda")]
    interaction = make_interaction()

    await invoke(cog.game, cog, interaction, "zel")

    assert sent(interaction)["embed"].title == "Zelda: Echoes"


async def test_game_with_an_unknown_numeric_id_is_searched_by_name(
    cog: ReleasesCog, igdb: FakeIgdb
) -> None:
    igdb.search_results = [make_game(5, "1942")]
    interaction = make_interaction()

    await invoke(cog.game, cog, interaction, "1942")

    assert igdb.id_calls == [[1942]]
    assert igdb.search_calls[0][0] == "1942"
    assert sent(interaction)["embed"].title == "1942"


async def test_game_not_found(cog: ReleasesCog, igdb: FakeIgdb) -> None:
    interaction = make_interaction()

    await invoke(cog.game, cog, interaction, "*nothing*")

    interaction.followup.send.assert_awaited_once_with(
        "❓ Non ho trovato nessun gioco per «\\*nothing\\*»."
    )


@pytest.mark.parametrize("text", ["42", "zelda"])
async def test_game_reports_an_igdb_failure(
    cog: ReleasesCog, igdb: FakeIgdb, text: str
) -> None:
    igdb.error = IgdbError("down")
    interaction = make_interaction()

    await invoke(cog.game, cog, interaction, text)

    interaction.followup.send.assert_awaited_once_with(IGDB_UNAVAILABLE)


async def test_game_card_for_a_released_game(cog: ReleasesCog, igdb: FakeIgdb) -> None:
    igdb.by_id[1] = make_game(1, "Old Game", days_ahead=-40)
    interaction = make_interaction()

    await invoke(cog.game, cog, interaction, "1")

    assert "Uscito" in field_values(sent(interaction)["embed"])["📅 Uscita"]


def test_game_card_without_an_exact_date() -> None:
    quarter = make_game(
        1,
        "Someday",
        None,
        release_dates=(PlatformRelease(6, None, None, human="Q3 2027"),),
    )
    unknown = make_game(2, "Never", None)

    assert build_game_detail_payload(quarter, today=TODAY).fields[0].value == "Q3 2027"
    assert (
        build_game_detail_payload(unknown, today=TODAY).fields[0].value
        == "Data da definire"
    )


# -- registration --------------------------------------------------------------


async def test_cog_registers_the_italian_commands() -> None:
    settings = Settings(
        discord_token="token",  # pyright: ignore[reportArgumentType]
        twitch_client_id="id",
        twitch_client_secret="secret",  # pyright: ignore[reportArgumentType]
        _env_file=None,  # pyright: ignore[reportCallIssue]
    )
    bot = GameReleasesBot(settings)

    await setup(bot)

    names = {command.name for command in bot.tree.get_commands()}
    assert names == {"nextreleases", "calendar", "game"}
    game = bot.tree.get_command("game")
    assert isinstance(game, discord.app_commands.Command)
    assert game._params["nome"].autocomplete  # pyright: ignore[reportPrivateUsage]
    for command in bot.tree.get_commands():
        assert isinstance(command, discord.app_commands.Command)
        assert command.description
        assert command.default_permissions is None  # public
