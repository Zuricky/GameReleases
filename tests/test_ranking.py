"""Tests for release ranking and filtering."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from gamereleases.domain.models import (
    DateFormat,
    Game,
    GameType,
    Platform,
    PlatformGroup,
    PlatformPreset,
    PlatformRelease,
)
from gamereleases.domain.ranking import (
    days_until,
    game_platform_ids,
    matches_preset,
    normalize_title,
    platform_group,
    rank_upcoming,
    resolve_release_date,
    resolve_timezone,
    sort_chronologically,
    today_in,
)

ROME = ZoneInfo("Europe/Rome")
NEW_YORK = ZoneInfo("America/New_York")
TODAY = date(2026, 11, 21)

PC = 6
PS4 = 48
PS5 = 167
XBOX_ONE = 49
XBOX_SERIES = 169
SWITCH = 130
SWITCH_2 = 508
ANDROID = 34
IOS = 39


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def make_game(
    game_id: int,
    name: str | None = None,
    *,
    release: datetime | None = None,
    hypes: int = 0,
    follows: int = 0,
    platform_ids: tuple[int, ...] = (PC,),
    release_dates: tuple[PlatformRelease, ...] = (),
    game_type: GameType | None = GameType.MAIN_GAME,
    version_parent: int | None = None,
) -> Game:
    return Game(
        id=game_id,
        name=name or f"Game {game_id}",
        slug=f"game-{game_id}",
        first_release_date=release,
        hypes=hypes,
        follows=follows,
        game_type=game_type,
        version_parent=version_parent,
        platforms=tuple(Platform(id=pid, name=f"P{pid}") for pid in platform_ids),
        release_dates=release_dates,
    )


def entry(
    platform_id: int | None,
    when: datetime | None,
    date_format: DateFormat | None = DateFormat.EXACT_DAY,
) -> PlatformRelease:
    return PlatformRelease(platform_id=platform_id, date=when, date_format=date_format)


# --- platform groups and presets -------------------------------------------


@pytest.mark.parametrize(
    ("platform_id", "group"),
    [
        (6, PlatformGroup.PC),
        (14, PlatformGroup.PC),
        (3, PlatformGroup.PC),
        (PS5, PlatformGroup.PS5),
        (XBOX_SERIES, PlatformGroup.XBOX_SERIES),
        (SWITCH, PlatformGroup.SWITCH),
        (SWITCH_2, PlatformGroup.SWITCH),
        (PS4, PlatformGroup.LAST_GEN),
        (XBOX_ONE, PlatformGroup.LAST_GEN),
        (ANDROID, PlatformGroup.MOBILE),
        (IOS, PlatformGroup.MOBILE),
        (999_999, PlatformGroup.OTHER),
    ],
)
def test_platform_group_normalization(platform_id: int, group: PlatformGroup) -> None:
    assert platform_group(platform_id) is group


@pytest.mark.parametrize(
    ("preset", "platform_id", "expected"),
    [
        (PlatformPreset.ALL, ANDROID, True),
        (PlatformPreset.ALL, 999_999, True),
        (PlatformPreset.PC_CONSOLE, PC, True),
        (PlatformPreset.PC_CONSOLE, PS4, True),
        (PlatformPreset.PC_CONSOLE, XBOX_SERIES, True),
        (PlatformPreset.PC_CONSOLE, SWITCH_2, True),
        (PlatformPreset.PC_CONSOLE, IOS, False),
        (PlatformPreset.PC_ONLY, PC, True),
        (PlatformPreset.PC_ONLY, PS5, False),
        (PlatformPreset.PLAYSTATION, PS5, True),
        (PlatformPreset.PLAYSTATION, PS4, True),
        (PlatformPreset.PLAYSTATION, XBOX_ONE, False),
        (PlatformPreset.XBOX, XBOX_SERIES, True),
        (PlatformPreset.XBOX, XBOX_ONE, True),
        (PlatformPreset.XBOX, PS5, False),
        (PlatformPreset.NINTENDO, SWITCH, True),
        (PlatformPreset.NINTENDO, SWITCH_2, True),
        (PlatformPreset.NINTENDO, PC, False),
    ],
)
def test_preset_matching_by_platform(
    preset: PlatformPreset, platform_id: int, expected: bool
) -> None:
    game = make_game(1, platform_ids=(platform_id,))
    assert matches_preset(game, preset) is expected


@pytest.mark.parametrize(
    "preset",
    [
        PlatformPreset.PC_CONSOLE,
        PlatformPreset.PC_ONLY,
        PlatformPreset.PLAYSTATION,
        PlatformPreset.XBOX,
        PlatformPreset.NINTENDO,
    ],
)
def test_mobile_only_games_are_excluded_from_curated_presets(
    preset: PlatformPreset,
) -> None:
    game = make_game(1, platform_ids=(ANDROID, IOS))
    assert not matches_preset(game, preset)


def test_mobile_only_games_are_kept_by_the_unfiltered_preset() -> None:
    game = make_game(1, platform_ids=(ANDROID, IOS))
    assert matches_preset(game, PlatformPreset.ALL)


def test_cross_platform_game_with_mobile_version_still_matches() -> None:
    game = make_game(1, platform_ids=(PC, ANDROID))
    assert matches_preset(game, PlatformPreset.PC_ONLY)


def test_game_without_platform_data_only_passes_the_unfiltered_preset() -> None:
    game = make_game(1, platform_ids=())
    assert matches_preset(game, PlatformPreset.ALL)
    assert not matches_preset(game, PlatformPreset.PC_CONSOLE)


def test_platform_ids_are_collected_from_release_dates_too() -> None:
    game = make_game(
        1,
        platform_ids=(PC,),
        release_dates=(entry(PS5, utc(2026, 12, 1)), entry(None, utc(2026, 12, 2))),
    )
    assert game_platform_ids(game) == {PC, PS5}


# --- date resolution --------------------------------------------------------


def test_first_release_date_is_used_without_platform_entries() -> None:
    game = make_game(1, release=utc(2026, 12, 5))
    assert resolve_release_date(game) == date(2026, 12, 5)


def test_missing_dates_resolve_to_none_instead_of_today() -> None:
    assert resolve_release_date(make_game(1)) is None


def test_naive_datetimes_are_treated_as_utc() -> None:
    game = make_game(1, release=datetime(2026, 12, 5, 23, 30))
    assert resolve_release_date(game) == date(2026, 12, 5)


def test_release_day_is_the_utc_calendar_day() -> None:
    # Late-evening UTC stays on the same day: the guild zone is not applied.
    game = make_game(1, release=utc(2026, 12, 5, 23, 30))
    assert resolve_release_date(game) == date(2026, 12, 5)


def test_non_utc_aware_datetimes_are_read_in_utc() -> None:
    moment = datetime(2026, 12, 5, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert resolve_release_date(make_game(1, release=moment)) == date(2026, 12, 6)


def test_midnight_utc_keeps_its_day_for_a_guild_west_of_utc() -> None:
    midnight = utc(2026, 12, 5, 0, 0)
    assert resolve_release_date(make_game(1, release=midnight)) == date(2026, 12, 5)
    exact = make_game(2, release_dates=(entry(PC, midnight),))
    assert resolve_release_date(exact) == date(2026, 12, 5)


def test_only_exact_day_entries_are_resolved() -> None:
    game = make_game(
        1,
        release=utc(2026, 12, 1),
        release_dates=(
            entry(PC, utc(2026, 12, 1), DateFormat.MONTH),
            entry(PS5, utc(2026, 12, 31), DateFormat.YEAR),
            entry(XBOX_SERIES, None, DateFormat.TBD),
        ),
    )
    assert resolve_release_date(game) is None


def test_entry_without_a_date_format_is_not_trusted() -> None:
    game = make_game(1, release_dates=(entry(PC, utc(2026, 12, 1), None),))
    assert resolve_release_date(game) is None


def test_exact_entry_wins_over_imprecise_ones() -> None:
    game = make_game(
        1,
        release=utc(2026, 12, 1),
        release_dates=(
            entry(PC, utc(2026, 12, 1), DateFormat.MONTH),
            entry(PS5, utc(2026, 12, 12), DateFormat.EXACT_DAY),
        ),
    )
    assert resolve_release_date(game) == date(2026, 12, 12)


def test_earliest_exact_entry_is_chosen() -> None:
    game = make_game(
        1,
        release_dates=(
            entry(PS5, utc(2026, 12, 20)),
            entry(PC, utc(2026, 12, 10)),
            entry(SWITCH, utc(2027, 2, 1)),
        ),
    )
    assert resolve_release_date(game) == date(2026, 12, 10)


def test_platform_restriction_ignores_other_platform_dates() -> None:
    game = make_game(
        1,
        release_dates=(entry(PC, utc(2026, 12, 10)), entry(PS5, utc(2026, 12, 20))),
    )
    assert resolve_release_date(game, frozenset({PS5})) == date(2026, 12, 20)
    assert resolve_release_date(game, frozenset({SWITCH})) is None


def test_platform_agnostic_entry_applies_to_every_restriction() -> None:
    game = make_game(1, release_dates=(entry(None, utc(2026, 12, 10)),))
    assert resolve_release_date(game, frozenset({PS5})) == date(2026, 12, 10)


# --- today and countdown ----------------------------------------------------


def test_today_is_computed_in_the_guild_zone() -> None:
    now = utc(2026, 11, 21, 23, 30)
    assert today_in(now, ROME) == date(2026, 11, 22)
    assert today_in(now, UTC) == date(2026, 11, 21)
    assert today_in(now, NEW_YORK) == date(2026, 11, 21)


def test_today_follows_the_spring_dst_change() -> None:
    # Rome switches to UTC+2 at 01:00 UTC on 2026-03-29.
    assert today_in(utc(2026, 3, 28, 22, 30), ROME) == date(2026, 3, 28)
    assert today_in(utc(2026, 3, 28, 23, 0), ROME) == date(2026, 3, 29)
    assert today_in(utc(2026, 3, 29, 21, 59), ROME) == date(2026, 3, 29)
    assert today_in(utc(2026, 3, 29, 22, 0), ROME) == date(2026, 3, 30)


@pytest.mark.parametrize(
    ("release", "expected"),
    [
        (date(2026, 11, 21), 0),
        (date(2026, 11, 22), 1),
        (date(2026, 12, 21), 30),
        (date(2026, 11, 20), -1),
    ],
)
def test_days_until(release: date, expected: int) -> None:
    assert days_until(release, TODAY) == expected


def test_unknown_timezone_falls_back_to_the_default() -> None:
    assert resolve_timezone("Mars/Olympus_Mons").key == "Europe/Rome"
    assert resolve_timezone("America/New_York").key == "America/New_York"


# --- title normalization ----------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Hades II", "HADES II™"),
        ("Pokémon Legends", "Pokemon  Legends"),
        ("Marvel's Spider-Man 2", "marvels spiderman 2"),
        ("Half-Life: Alyx", "half life alyx"),
    ],
)
def test_equivalent_titles_normalize_identically(left: str, right: str) -> None:
    assert normalize_title(left) == normalize_title(right)


def test_distinct_non_latin_titles_stay_distinct() -> None:
    assert normalize_title("が") != normalize_title("か")
    assert normalize_title("ゼルダ") == normalize_title("ゼルダ")


# --- ranking ----------------------------------------------------------------


def rank(
    games: list[Game],
    *,
    preset: PlatformPreset = PlatformPreset.ALL,
    min_hypes: int = 0,
    include_dlc: bool = False,
    window_days: int | None = None,
    limit: int = 10,
) -> list[int]:
    ranked = rank_upcoming(
        games,
        today=TODAY,
        preset=preset,
        min_hypes=min_hypes,
        include_dlc=include_dlc,
        window_days=window_days,
        limit=limit,
    )
    return [item.game.id for item in ranked]


def test_past_games_are_excluded_and_today_is_kept() -> None:
    games = [
        make_game(1, release=utc(2026, 11, 20, 12)),
        make_game(2, release=utc(2026, 11, 21, 12)),
        make_game(3, release=utc(2026, 11, 22, 12)),
    ]
    assert rank(games) == [2, 3]


def test_new_york_guild_ranks_a_midnight_utc_release_on_its_intended_day() -> None:
    # 2026-12-05 00:00 UTC is still the 4th in New York, but the game releases
    # on the 5th for everybody.
    game = make_game(1, release=utc(2026, 12, 5, 0, 0))
    today = today_in(utc(2026, 12, 4, 18, 0), NEW_YORK)
    assert today == date(2026, 12, 4)

    (ranked,) = rank_upcoming([game], today=today)
    assert ranked.release_date == date(2026, 12, 5)
    assert days_until(ranked.release_date, today) == 1


def test_release_is_not_dropped_when_new_york_is_still_the_previous_day() -> None:
    game = make_game(1, release=utc(2026, 12, 5, 0, 0))
    today = today_in(utc(2026, 12, 5, 3, 0), NEW_YORK)
    assert today == date(2026, 12, 4)
    assert [r.game.id for r in rank_upcoming([game], today=today)] == [1]


def test_release_is_past_once_the_guild_calendar_moves_on() -> None:
    game = make_game(1, release=utc(2026, 12, 5, 0, 0))
    today = today_in(utc(2026, 12, 6, 6, 0), NEW_YORK)
    assert today == date(2026, 12, 6)
    assert rank_upcoming([game], today=today) == []


def test_game_with_only_an_imprecise_date_is_excluded() -> None:
    game = make_game(
        1,
        release=utc(2026, 12, 1),
        release_dates=(entry(PC, utc(2026, 12, 1), DateFormat.QUARTER_4),),
    )
    assert rank([game]) == []


def test_results_are_chronological_with_hype_and_name_tiebreaks() -> None:
    same_day = utc(2026, 12, 1, 12)
    games = [
        make_game(1, "Zeta", release=same_day, hypes=5),
        make_game(2, "Alpha", release=same_day, hypes=5),
        make_game(3, "Beta", release=same_day, hypes=50),
        make_game(4, "Early", release=utc(2026, 11, 25, 12), hypes=1),
    ]
    assert rank(games) == [4, 3, 2, 1]


def test_top_n_keeps_the_most_hyped_then_sorts_by_date() -> None:
    games = [
        make_game(1, release=utc(2026, 12, 20, 12), hypes=100),
        make_game(2, release=utc(2026, 11, 25, 12), hypes=10),
        make_game(3, release=utc(2026, 12, 1, 12), hypes=50),
        make_game(4, release=utc(2026, 11, 22, 12), hypes=1),
    ]
    assert rank(games, limit=2) == [3, 1]


def test_default_limit_is_the_top_ten() -> None:
    games = [
        make_game(i, release=utc(2026, 12, 1 + i % 20, 12), hypes=i)
        for i in range(1, 16)
    ]
    assert len(rank(games)) == 10


@pytest.mark.parametrize("limit", [0, -3])
def test_non_positive_limit_returns_nothing(limit: int) -> None:
    assert rank([make_game(1, release=utc(2026, 12, 1, 12))], limit=limit) == []


def test_duplicate_ids_keep_the_first_ranked_copy_only() -> None:
    games = [
        make_game(1, "Same", release=utc(2026, 12, 1, 12), hypes=5),
        make_game(1, "Same", release=utc(2026, 12, 1, 12), hypes=5),
    ]
    assert rank(games) == [1]


def test_duplicate_titles_keep_the_most_hyped_entry() -> None:
    games = [
        make_game(1, "Hades II", release=utc(2026, 12, 1, 12), hypes=10),
        make_game(2, "HADES II™", release=utc(2026, 12, 2, 12), hypes=80),
        make_game(3, "Other", release=utc(2026, 12, 3, 12), hypes=1),
    ]
    assert rank(games) == [2, 3]


def test_titles_that_normalize_to_nothing_are_not_merged() -> None:
    games = [
        make_game(1, "★★★", release=utc(2026, 12, 1, 12)),
        make_game(2, "???", release=utc(2026, 12, 2, 12)),
    ]
    assert rank(games) == [1, 2]


def test_titles_are_deduplicated_even_when_the_copy_is_beyond_the_limit() -> None:
    games = [
        make_game(1, "Dupe", release=utc(2026, 12, 1, 12), hypes=100),
        make_game(2, "Dupe", release=utc(2026, 12, 2, 12), hypes=90),
        make_game(3, "Next", release=utc(2026, 12, 3, 12), hypes=80),
    ]
    assert rank(games, limit=2) == [1, 3]


def test_min_hypes_filters_low_interest_games() -> None:
    games = [
        make_game(1, release=utc(2026, 12, 1, 12), hypes=4),
        make_game(2, release=utc(2026, 12, 2, 12), hypes=5),
    ]
    assert rank(games, min_hypes=5) == [2]


def test_window_limits_how_far_ahead_games_are_listed() -> None:
    games = [
        make_game(1, release=utc(2026, 12, 21, 12)),
        make_game(2, release=utc(2026, 12, 22, 12)),
    ]
    assert rank(games, window_days=30) == [1]
    assert rank(games, window_days=None) == [1, 2]


def test_preset_filter_applies_before_selection() -> None:
    games = [
        make_game(1, release=utc(2026, 12, 1, 12), hypes=99, platform_ids=(ANDROID,)),
        make_game(2, release=utc(2026, 12, 2, 12), hypes=1, platform_ids=(PS5,)),
        make_game(3, release=utc(2026, 12, 3, 12), hypes=1, platform_ids=(PC,)),
    ]
    assert rank(games, preset=PlatformPreset.ALL, limit=1) == [1]
    assert rank(games, preset=PlatformPreset.PC_CONSOLE) == [2, 3]
    assert rank(games, preset=PlatformPreset.PLAYSTATION) == [2]


def test_preset_uses_the_matching_platform_release_date() -> None:
    game = make_game(
        1,
        platform_ids=(PC, PS5),
        release_dates=(
            entry(PC, utc(2026, 11, 10, 12)),
            entry(PS5, utc(2026, 12, 10, 12)),
        ),
    )
    # Already out on PC, but still upcoming for PlayStation players.
    assert rank([game], preset=PlatformPreset.ALL) == []
    assert rank([game], preset=PlatformPreset.PLAYSTATION) == [1]


def test_dlc_and_editions_are_excluded_unless_enabled() -> None:
    games = [
        make_game(1, release=utc(2026, 12, 1, 12)),
        make_game(2, release=utc(2026, 12, 2, 12), game_type=GameType.EXPANSION),
        make_game(3, release=utc(2026, 12, 3, 12), game_type=GameType.DLC_ADDON),
        make_game(4, release=utc(2026, 12, 4, 12), game_type=GameType.BUNDLE),
        make_game(5, release=utc(2026, 12, 5, 12), game_type=None),
        make_game(6, release=utc(2026, 12, 6, 12), version_parent=1),
    ]
    assert rank(games) == [1, 5]
    assert rank(games, include_dlc=True) == [1, 2, 5]


def test_sort_chronologically_orders_ranked_games() -> None:
    ranked = rank_upcoming(
        [
            make_game(1, release=utc(2026, 12, 9, 12)),
            make_game(2, release=utc(2026, 12, 1, 12)),
        ],
        today=TODAY,
    )
    assert [r.game.id for r in sort_chronologically(reversed(ranked))] == [2, 1]
    assert ranked[0].release_date == date(2026, 12, 1)
