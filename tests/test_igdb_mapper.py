"""Tests for IGDB payload mapping."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gamereleases.domain.models import DateFormat, GameType, StoreKind
from gamereleases.igdb.mapper import (
    classify_store_url,
    cover_url,
    extract_stores,
    map_game,
    map_games,
    trailer_url,
)

OFFICIAL = 1


def site(url: str, type_id: int | None = None) -> dict[str, object]:
    website: dict[str, object] = {"url": url}
    if type_id is not None:
        website["type"] = type_id
    return website


def kinds(stores: tuple[object, ...]) -> list[StoreKind]:
    return [store.kind for store in stores]  # pyright: ignore[reportAttributeAccessIssue]


# --- store classification ---------------------------------------------------


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://store.steampowered.com/app/1245620/ELDEN_RING/", StoreKind.STEAM),
        ("http://STORE.STEAMPOWERED.COM/app/1", StoreKind.STEAM),
        ("https://store.epicgames.com/en-US/p/some-game", StoreKind.EPIC),
        ("https://www.epicgames.com/store/en-US/p/some-game", StoreKind.EPIC),
        ("https://www.gog.com/en/game/witcher_3", StoreKind.GOG),
        ("https://gog.com/game/x", StoreKind.GOG),
        ("https://store.playstation.com/en-it/concept/10000", StoreKind.PLAYSTATION),
        ("https://www.xbox.com/en-US/games/store/x/abc", StoreKind.XBOX),
        ("https://www.microsoft.com/store/p/x/abc", StoreKind.XBOX),
        ("https://www.nintendo.com/us/store/products/x/", StoreKind.NINTENDO),
        ("https://www.nintendo.it/Giochi/x", StoreKind.NINTENDO),
    ],
)
def test_store_is_detected_from_the_host(url: str, kind: StoreKind) -> None:
    assert classify_store_url(url) is kind


@pytest.mark.parametrize(
    "url",
    [
        "https://evilsteampowered.com/app/1",
        "https://store.steampowered.com.evil.example/app/1",
        "https://example.com/?next=https://store.steampowered.com/app/1",
        "https://www.epicgames.com/help/en-US",
        "https://steamcommunity.com/app/1",
        "https://example.com/game",
        "ftp://store.steampowered.com/app/1",
        "javascript:alert(1)",
        "store.steampowered.com/app/1",
        "https://",
        "",
    ],
)
def test_non_store_urls_are_not_classified(url: str) -> None:
    assert classify_store_url(url) is None


def test_malformed_url_does_not_raise() -> None:
    assert classify_store_url("http://[invalid") is None


# --- store extraction and priority -------------------------------------------


def test_stores_are_sorted_by_priority_with_igdb_last() -> None:
    websites = [
        site("https://www.nintendo.com/store/x"),
        site("https://store.playstation.com/x"),
        site("https://store.steampowered.com/app/1"),
        site("https://www.gog.com/game/x"),
        site("https://store.epicgames.com/p/x"),
        site("https://www.xbox.com/games/x"),
        site("https://game.example", OFFICIAL),
    ]
    stores = extract_stores(websites, "some-game")
    assert kinds(stores) == [
        StoreKind.STEAM,
        StoreKind.EPIC,
        StoreKind.GOG,
        StoreKind.PLAYSTATION,
        StoreKind.XBOX,
        StoreKind.NINTENDO,
        StoreKind.OFFICIAL,
        StoreKind.IGDB,
    ]
    assert stores[-1].url == "https://www.igdb.com/games/some-game"


def test_title_link_prefers_the_highest_priority_store() -> None:
    websites = [
        site("https://store.playstation.com/x"),
        site("https://store.epicgames.com/p/x"),
    ]
    assert extract_stores(websites, "g")[0].kind is StoreKind.EPIC


def test_host_wins_over_the_website_type() -> None:
    # An official-typed link on a store host is a store link, not the website.
    stores = extract_stores(
        [site("https://store.steampowered.com/app/1", OFFICIAL)], None
    )
    assert kinds(stores) == [StoreKind.STEAM]


def test_store_is_detected_regardless_of_the_declared_type() -> None:
    stores = extract_stores([site("https://store.steampowered.com/app/1", 999)], None)
    assert kinds(stores) == [StoreKind.STEAM]


def test_only_the_first_link_per_store_is_kept() -> None:
    websites = [
        site("https://store.steampowered.com/app/1"),
        site("https://store.steampowered.com/app/2"),
    ]
    stores = extract_stores(websites, None)
    assert [s.url for s in stores] == ["https://store.steampowered.com/app/1"]


def test_unrecognized_links_are_dropped_unless_official() -> None:
    websites = [
        site("https://twitter.com/game", 5),
        site("https://reddit.com/r/game", 14),
        site("https://game.example"),
    ]
    assert extract_stores(websites, None) == ()


def test_official_site_is_taken_from_the_website_type() -> None:
    stores = extract_stores([site("https://game.example", OFFICIAL)], None)
    assert [(s.kind, s.url) for s in stores] == [
        (StoreKind.OFFICIAL, "https://game.example")
    ]


def test_official_type_may_be_an_expanded_reference() -> None:
    website: dict[str, object] = {"url": "https://game.example", "type": {"id": 1}}
    assert kinds(extract_stores([website], None)) == [StoreKind.OFFICIAL]


def test_official_link_with_unsafe_scheme_is_dropped() -> None:
    assert extract_stores([site("javascript:alert(1)", OFFICIAL)], None) == ()


def test_igdb_page_is_the_fallback_when_no_other_link_exists() -> None:
    stores = extract_stores([], "elden-ring")
    assert [(s.kind, s.url) for s in stores] == [
        (StoreKind.IGDB, "https://www.igdb.com/games/elden-ring")
    ]


def test_no_links_at_all_without_a_slug() -> None:
    assert extract_stores([], None) == ()
    assert extract_stores([], "") == ()


def test_blank_and_non_string_urls_are_ignored() -> None:
    websites: list[dict[str, object]] = [{"url": ""}, {"url": None}, {"url": 3}, {}]
    assert extract_stores(websites, None) == ()


def test_store_urls_are_trimmed() -> None:
    stores = extract_stores([site("  https://www.gog.com/game/x \n")], None)
    assert stores[0].url == "https://www.gog.com/game/x"


# --- images and videos ------------------------------------------------------


def test_cover_and_trailer_url_formulas() -> None:
    assert cover_url("co1abc") == (
        "https://images.igdb.com/igdb/image/upload/t_cover_big/co1abc.jpg"
    )
    assert trailer_url("dQw4w9WgXcQ") == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


# --- full game mapping -------------------------------------------------------


def full_payload() -> dict[str, object]:
    return {
        "id": 119133,
        "name": "  Elden Ring ",
        "slug": "elden-ring",
        "summary": "An open world action RPG.",
        "first_release_date": 1645747200,
        "hypes": 120,
        "follows": 4000,
        "total_rating": 95.5,
        "total_rating_count": 2500,
        "game_type": 0,
        "cover": {"id": 1, "image_id": "co4jni"},
        "genres": [{"id": 12, "name": "RPG"}, {"id": 31, "name": "Adventure"}],
        "platforms": [
            {"id": 6, "name": "PC (Microsoft Windows)", "abbreviation": "PC"},
            {"id": 167, "name": "PlayStation 5"},
        ],
        "release_dates": [
            {
                "date": 1645747200,
                "date_format": 0,
                "platform": 6,
                "human": "Feb 25, 2022",
            },
            {"date": 1646092800, "date_format": 1, "platform": {"id": 167}},
        ],
        "websites": [
            {"url": "https://store.steampowered.com/app/1245620", "type": 13},
            {"url": "https://eldenring.example", "type": 1},
        ],
        "videos": [{"video_id": "E3Huy2cdih0"}, {"video_id": "second"}],
    }


def test_full_payload_is_mapped() -> None:
    game = map_game(full_payload())

    assert game is not None
    assert game.id == 119133
    assert game.name == "Elden Ring"
    assert game.slug == "elden-ring"
    assert game.summary == "An open world action RPG."
    assert game.first_release_date == datetime(2022, 2, 25, tzinfo=UTC)
    assert game.hypes == 120
    assert game.follows == 4000
    assert game.total_rating == 95.5
    assert game.total_rating_count == 2500
    assert game.game_type is GameType.MAIN_GAME
    assert game.version_parent is None
    assert game.cover_url == (
        "https://images.igdb.com/igdb/image/upload/t_cover_big/co4jni.jpg"
    )
    assert game.trailer_url == "https://www.youtube.com/watch?v=E3Huy2cdih0"
    assert game.genres == ("RPG", "Adventure")
    assert [(p.id, p.abbreviation) for p in game.platforms] == [(6, "PC"), (167, None)]
    assert [(r.platform_id, r.date_format) for r in game.release_dates] == [
        (6, DateFormat.EXACT_DAY),
        (167, DateFormat.MONTH),
    ]
    assert game.release_dates[0].human == "Feb 25, 2022"
    assert kinds(game.stores) == [StoreKind.STEAM, StoreKind.OFFICIAL, StoreKind.IGDB]


def test_minimal_payload_uses_safe_defaults() -> None:
    game = map_game({"id": 7, "name": "Bare"})

    assert game is not None
    assert game.slug == ""
    assert game.summary is None
    assert game.first_release_date is None
    assert game.hypes == 0
    assert game.follows == 0
    assert game.total_rating is None
    assert game.total_rating_count == 0
    assert game.game_type is None
    assert game.genres == ()
    assert game.platforms == ()
    assert game.release_dates == ()
    assert game.stores == ()


def test_missing_cover_falls_back_to_none() -> None:
    payload = full_payload()
    del payload["cover"]
    game = map_game(payload)
    assert game is not None and game.cover_url is None


@pytest.mark.parametrize(
    "cover",
    [None, {}, {"image_id": ""}, {"image_id": None}, {"image_id": 5}, "co4jni"],
)
def test_unusable_cover_values_fall_back_to_none(cover: object) -> None:
    payload = full_payload()
    payload["cover"] = cover
    game = map_game(payload)
    assert game is not None and game.cover_url is None


def test_cover_image_id_with_path_characters_is_rejected() -> None:
    payload = full_payload()
    payload["cover"] = {"image_id": "../../evil"}
    game = map_game(payload)
    assert game is not None and game.cover_url is None


def test_missing_trailer_falls_back_to_none() -> None:
    payload = full_payload()
    del payload["videos"]
    game = map_game(payload)
    assert game is not None and game.trailer_url is None


def test_empty_video_list_has_no_trailer() -> None:
    payload = full_payload()
    payload["videos"] = []
    game = map_game(payload)
    assert game is not None and game.trailer_url is None


def test_first_usable_video_becomes_the_trailer() -> None:
    payload = full_payload()
    payload["videos"] = [{"video_id": ""}, {"name": "no id"}, {"video_id": "good_id-1"}]
    game = map_game(payload)
    assert game is not None
    assert game.trailer_url == "https://www.youtube.com/watch?v=good_id-1"


def test_video_id_that_could_inject_url_parts_is_rejected() -> None:
    payload = full_payload()
    payload["videos"] = [{"video_id": "abc&redirect=evil"}]
    game = map_game(payload)
    assert game is not None and game.trailer_url is None


def test_unknown_enum_values_are_tolerated() -> None:
    payload = full_payload()
    payload["game_type"] = 9999
    payload["release_dates"] = [{"date": 1645747200, "date_format": 42, "platform": 6}]
    game = map_game(payload)

    assert game is not None
    assert game.game_type is None
    assert game.release_dates[0].date_format is None


def test_expanded_references_are_accepted() -> None:
    payload = full_payload()
    payload["game_type"] = {"id": 2, "type": "Expansion"}
    payload["version_parent"] = {"id": 55}
    game = map_game(payload)

    assert game is not None
    assert game.game_type is GameType.EXPANSION
    assert game.version_parent == 55


def test_wrongly_typed_fields_are_ignored() -> None:
    game = map_game(
        {
            "id": 1,
            "name": "Odd",
            "hypes": "many",
            "follows": True,
            "total_rating": "high",
            "first_release_date": "tomorrow",
            "genres": "RPG",
            "platforms": [None, 3, {"name": "no id"}],
            "release_dates": {"date": 1},
            "websites": "https://store.steampowered.com/app/1",
            "videos": [None, "x"],
        }
    )

    assert game is not None
    assert game.hypes == 0
    assert game.follows == 0
    assert game.total_rating is None
    assert game.first_release_date is None
    assert game.genres == ()
    assert game.platforms == ()
    assert game.release_dates == ()
    assert game.stores == ()
    assert game.trailer_url is None


def test_absurd_timestamp_does_not_raise() -> None:
    game = map_game({"id": 1, "name": "Far", "first_release_date": 10**15})
    assert game is not None and game.first_release_date is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": "No id"},
        {"id": 1},
        {"id": 1, "name": ""},
        {"id": 1, "name": "   "},
        {"id": "1", "name": "String id"},
        {"id": True, "name": "Bool id"},
    ],
)
def test_records_without_id_or_name_are_dropped(payload: dict[str, object]) -> None:
    assert map_game(payload) is None


def test_map_games_skips_malformed_records_and_keeps_order() -> None:
    games = map_games(
        [{"id": 2, "name": "B"}, "garbage", None, {"id": 1}, {"id": 3, "name": "C"}]
    )
    assert [g.id for g in games] == [2, 3]
