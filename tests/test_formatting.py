"""Tests for embed data formatting."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gamereleases.domain.formatting import (
    COUNTDOWN_MAX_DAYS,
    EMBED_DESCRIPTION_LIMIT,
    PRESET_LABELS,
    EmbedData,
    build_calendar,
    build_overview,
    discord_timestamp,
    escape_markdown,
    format_countdown,
    format_duration,
    group_by_release_date,
    hash_payload,
    release_timestamp,
)
from gamereleases.domain.models import Game, PlatformPreset, RankedGame

TODAY = date(2026, 11, 21)
SYNCED_AT = datetime(2026, 11, 21, 10, 30, tzinfo=UTC)
SYNCED_UNIX = int(SYNCED_AT.timestamp())


def ranked(game_id: int, name: str, release: date) -> RankedGame:
    return RankedGame(
        game=Game(id=game_id, name=name, slug=f"g{game_id}"), release_date=release
    )


# --- countdown --------------------------------------------------------------


@pytest.mark.parametrize(
    ("release", "expected"),
    [
        (date(2026, 11, 21), "Esce oggi!"),
        (date(2026, 11, 22), "Tra 1 giorno"),
        (date(2026, 11, 23), "Tra 2 giorni"),
        (date(2026, 12, 21), "Tra 30 giorni"),
        (date(2026, 12, 22), "22/12/2026"),
        (date(2027, 3, 5), "05/03/2027"),
        (date(2026, 11, 20), "Uscito"),
    ],
)
def test_countdown_labels(release: date, expected: str) -> None:
    assert format_countdown(release, TODAY) == expected


def test_countdown_switches_to_a_date_right_after_the_threshold() -> None:
    last = date.fromordinal(TODAY.toordinal() + COUNTDOWN_MAX_DAYS)
    after = date.fromordinal(last.toordinal() + 1)
    assert format_countdown(last, TODAY).startswith("Tra ")
    assert re.fullmatch(r"\d{2}/\d{2}/\d{4}", format_countdown(after, TODAY))


# --- timestamps and escaping -------------------------------------------------


def test_release_timestamp_is_noon_utc() -> None:
    assert release_timestamp(date(2026, 12, 1)) == int(
        datetime(2026, 12, 1, 12, tzinfo=UTC).timestamp()
    )


def test_release_timestamp_keeps_the_day_for_far_east_and_west_viewers() -> None:
    stamp = release_timestamp(date(2026, 12, 1))
    for offset_hours in (-11, 0, 11):
        shifted = datetime.fromtimestamp(stamp + offset_hours * 3600, tz=UTC)
        assert shifted.date() == date(2026, 12, 1)


def test_release_timestamp_renders_the_same_day_in_new_york() -> None:
    new_york = ZoneInfo("America/New_York")
    for day in (date(2026, 7, 10), date(2026, 12, 1)):
        local = datetime.fromtimestamp(release_timestamp(day), tz=new_york)
        assert local.date() == day


def test_overview_dates_are_noon_utc_stamps_for_a_new_york_viewer() -> None:
    day = date(2026, 12, 5)
    embed = build_overview(
        [ranked(1, "Alpha", day)], synced_at=SYNCED_AT, preset=PlatformPreset.ALL
    )
    match = re.search(r"<t:(\d+):d>", embed.description)
    assert match is not None
    local = datetime.fromtimestamp(int(match.group(1)), tz=ZoneInfo("America/New_York"))
    assert local.date() == day
    assert match.group(1) == str(int(datetime(2026, 12, 5, 12, tzinfo=UTC).timestamp()))


def test_discord_timestamp_tag() -> None:
    assert discord_timestamp(1700000000, "R") == "<t:1700000000:R>"


def test_markdown_characters_are_escaped() -> None:
    assert escape_markdown("A*B_C~D`E|F>G[H]I\\J") == r"A\*B\_C\~D\`E\|F\>G\[H\]I\\J"
    assert escape_markdown("Plain Title 2") == "Plain Title 2"


# --- overview ---------------------------------------------------------------


def test_overview_lists_games_with_dates_and_sync_time() -> None:
    items = [
        ranked(1, "Alpha", date(2026, 12, 1)),
        ranked(2, "Beta", date(2026, 12, 5)),
    ]
    embed = build_overview(items, synced_at=SYNCED_AT, preset=PlatformPreset.PC_CONSOLE)

    assert embed.title == "🎮 Prossime uscite"
    lines = embed.description.splitlines()
    assert lines[0] == f"• **Alpha** — <t:{release_timestamp(date(2026, 12, 1))}:d>"
    assert lines[1] == f"• **Beta** — <t:{release_timestamp(date(2026, 12, 5))}:d>"
    assert f"Ultimo aggiornamento: <t:{SYNCED_UNIX}:R>" in embed.description
    assert "Piattaforme: PC + Console" in embed.description


def test_overview_is_limited_to_the_top_ten_by_default() -> None:
    items = [ranked(i, f"Game {i}", date(2026, 12, i)) for i in range(1, 16)]
    embed = build_overview(items, synced_at=SYNCED_AT, preset=PlatformPreset.ALL)

    assert embed.description.count("•") == 10
    assert "Game 10" in embed.description
    assert "Game 11" not in embed.description


def test_overview_limit_can_be_changed() -> None:
    items = [ranked(i, f"Game {i}", date(2026, 12, i)) for i in range(1, 6)]
    embed = build_overview(
        items, synced_at=SYNCED_AT, preset=PlatformPreset.ALL, limit=3
    )
    assert embed.description.count("•") == 3


def test_empty_overview_still_reports_sync_and_filter() -> None:
    embed = build_overview([], synced_at=SYNCED_AT, preset=PlatformPreset.XBOX)

    assert "Nessuna uscita in programma" in embed.description
    assert "•" not in embed.description
    assert f"<t:{SYNCED_UNIX}:R>" in embed.description
    assert "Piattaforme: Xbox" in embed.description


def test_overview_treats_a_naive_sync_time_as_utc() -> None:
    embed = build_overview(
        [], synced_at=SYNCED_AT.replace(tzinfo=None), preset=PlatformPreset.ALL
    )
    assert f"<t:{SYNCED_UNIX}:R>" in embed.description


def test_overview_escapes_and_truncates_game_names() -> None:
    items = [
        ranked(1, "*Bold* _Title_", date(2026, 12, 1)),
        ranked(2, "X" * 200, date(2026, 12, 2)),
    ]
    embed = build_overview(items, synced_at=SYNCED_AT, preset=PlatformPreset.ALL)
    first, second = embed.description.splitlines()[:2]

    assert r"**\*Bold\* \_Title\_**" in first
    assert "…" in second
    assert len(second) < 100


def test_every_preset_has_an_italian_label() -> None:
    assert set(PRESET_LABELS) == set(PlatformPreset)
    assert PRESET_LABELS[PlatformPreset.ALL] == "Tutte"
    assert PRESET_LABELS[PlatformPreset.PC_ONLY] == "Solo PC"


# --- calendar ---------------------------------------------------------------


def test_games_are_grouped_by_release_day_in_order() -> None:
    items = [
        ranked(3, "C", date(2026, 12, 9)),
        ranked(1, "A", date(2026, 12, 1)),
        ranked(2, "B", date(2026, 12, 1)),
    ]
    groups = group_by_release_date(items)

    assert [day for day, _ in groups] == [date(2026, 12, 1), date(2026, 12, 9)]
    assert [[r.game.name for r in games] for _, games in groups] == [["A", "B"], ["C"]]


def test_calendar_groups_games_under_a_dated_header() -> None:
    items = [
        ranked(1, "Alpha", date(2026, 11, 21)),
        ranked(2, "Beta", date(2026, 11, 22)),
        ranked(3, "Gamma", date(2026, 11, 22)),
    ]
    embed = build_calendar(items, today=TODAY, days=30)

    assert embed.title == "📅 Calendario uscite (prossimi 30 giorni)"
    assert embed.description.splitlines() == [
        f"**<t:{release_timestamp(date(2026, 11, 21))}:d>** — Esce oggi!",
        "• Alpha",
        f"**<t:{release_timestamp(date(2026, 11, 22))}:d>** — Tra 1 giorno",
        "• Beta",
        "• Gamma",
    ]


def test_calendar_title_for_a_single_day() -> None:
    embed = build_calendar([], today=TODAY, days=1)
    assert embed.title == "📅 Calendario uscite (prossimo giorno)"


def test_empty_calendar_has_a_placeholder() -> None:
    embed = build_calendar([], today=TODAY, days=30)
    assert embed.description == "Nessuna uscita in programma."


def test_calendar_is_cut_short_with_a_count_of_hidden_games() -> None:
    items = [
        ranked(i, f"Game number {i} " + "x" * 40, date(2026, 12, 1 + i % 28))
        for i in range(400)
    ]
    embed = build_calendar(items, today=TODAY, days=90)

    assert len(embed.description) <= EMBED_DESCRIPTION_LIMIT
    last_line = embed.description.splitlines()[-1]
    match = re.fullmatch(r"…e altre (\d+) uscite", last_line)
    assert match is not None
    shown = embed.description.count("• ")
    assert shown + int(match.group(1)) == 400


# --- payload hashing --------------------------------------------------------


def test_hash_is_a_sha256_hex_digest() -> None:
    digest = hash_payload({"a": 1})
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_hash_ignores_mapping_key_order() -> None:
    assert hash_payload({"a": 1, "b": [1, 2]}) == hash_payload({"b": [1, 2], "a": 1})


def test_hash_is_stable_across_calls() -> None:
    payload = {"title": "Esce oggi!", "buttons": ["remind", "trailer"]}
    assert hash_payload(payload) == hash_payload(dict(payload))


def test_hash_changes_when_content_changes() -> None:
    base = {"title": "A", "buttons": ["remind"]}
    assert hash_payload(base) != hash_payload({"title": "B", "buttons": ["remind"]})
    assert hash_payload(base) != hash_payload({"title": "A", "buttons": []})
    assert hash_payload([1, 2]) != hash_payload([2, 1])


def test_hash_has_a_known_value() -> None:
    # Pinned so an accidental change of the canonical form is noticed: the
    # digest is compared against values already stored for existing panels.
    assert hash_payload({"a": 1}) == (
        "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
    )


def test_hash_does_not_escape_non_ascii_text() -> None:
    assert hash_payload("Tra 1 giorno — è") == hash_payload("Tra 1 giorno — è")
    assert hash_payload("è") != hash_payload("e")


def test_hash_accepts_embed_data_and_dates() -> None:
    embed = EmbedData(title="T", description="D", url="https://example.com")
    same = EmbedData(title="T", description="D", url="https://example.com")
    other = EmbedData(title="T", description="D2", url="https://example.com")

    assert hash_payload(embed) == hash_payload(same)
    assert hash_payload(embed) != hash_payload(other)
    assert hash_payload({"day": date(2026, 12, 1)}) == hash_payload(
        {"day": "2026-12-01"}
    )


def test_hash_is_independent_of_set_iteration_order() -> None:
    assert hash_payload({3, 1, 2}) == hash_payload({2, 3, 1})


def test_hash_rejects_unserializable_values() -> None:
    with pytest.raises(TypeError):
        hash_payload({"bad": object()})


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (timedelta(seconds=5), "5s"),
        (timedelta(seconds=59.9), "59s"),
        (timedelta(minutes=1), "1m"),
        (timedelta(hours=2, minutes=5), "2h 5m"),
        (timedelta(days=3, hours=4, minutes=14, seconds=40), "3g 4h 14m"),
        (timedelta(days=1), "1g"),
        (timedelta(seconds=-30), "0s"),
    ],
)
def test_format_duration(duration: timedelta, expected: str) -> None:
    assert format_duration(duration) == expected
