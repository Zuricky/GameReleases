"""Release date resolution, filtering and ranking of upcoming games.

Everything here is a pure function of its arguments: the current time is always
passed in, never read from the clock.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gamereleases.domain.models import (
    DEFAULT_TIMEZONE,
    TOP_LIST_SIZE,
    DateFormat,
    Game,
    GameType,
    PlatformGroup,
    PlatformPreset,
    RankedGame,
)

# IGDB platform identifiers.
_PC_IDS = frozenset({6, 14, 3})  # Windows, Mac, Linux
_PS5_IDS = frozenset({167})
_PS4_IDS = frozenset({48})
_XBOX_SERIES_IDS = frozenset({169})
_XBOX_ONE_IDS = frozenset({49})
_SWITCH_IDS = frozenset({130, 508})  # Switch, Switch 2
_MOBILE_IDS = frozenset({34, 39})  # Android, iOS

PLATFORM_GROUP_IDS: Mapping[PlatformGroup, frozenset[int]] = {
    PlatformGroup.PC: _PC_IDS,
    PlatformGroup.PS5: _PS5_IDS,
    PlatformGroup.XBOX_SERIES: _XBOX_SERIES_IDS,
    PlatformGroup.SWITCH: _SWITCH_IDS,
    PlatformGroup.LAST_GEN: _PS4_IDS | _XBOX_ONE_IDS,
    PlatformGroup.MOBILE: _MOBILE_IDS,
}

_GROUP_BY_PLATFORM_ID: Mapping[int, PlatformGroup] = {
    platform_id: group
    for group, ids in PLATFORM_GROUP_IDS.items()
    for platform_id in ids
}

# Platform IDs accepted by each preset; ``None`` means "no filtering at all".
# Curated presets never list mobile platforms, so a game that is only available
# on mobile can never match one of them.
PRESET_PLATFORM_IDS: Mapping[PlatformPreset, frozenset[int] | None] = {
    PlatformPreset.ALL: None,
    PlatformPreset.PC_CONSOLE: (
        _PC_IDS | _PS5_IDS | _PS4_IDS | _XBOX_SERIES_IDS | _XBOX_ONE_IDS | _SWITCH_IDS
    ),
    PlatformPreset.PC_ONLY: _PC_IDS,
    PlatformPreset.PLAYSTATION: _PS5_IDS | _PS4_IDS,
    PlatformPreset.XBOX: _XBOX_SERIES_IDS | _XBOX_ONE_IDS,
    PlatformPreset.NINTENDO: _SWITCH_IDS,
}

_SYMBOL_REMOVAL = str.maketrans("", "", "™®©℠")

# Types kept by default, and the extra ones allowed when DLC is enabled.
_BASE_GAME_TYPES = frozenset({GameType.MAIN_GAME})
_DLC_GAME_TYPES = _BASE_GAME_TYPES | {GameType.EXPANSION}


def platform_group(platform_id: int) -> PlatformGroup:
    """Normalize an IGDB platform ID into a platform family."""
    return _GROUP_BY_PLATFORM_ID.get(platform_id, PlatformGroup.OTHER)


def preset_platform_ids(preset: PlatformPreset) -> frozenset[int] | None:
    """Return the platform IDs a preset accepts, or ``None`` for no filtering."""
    return PRESET_PLATFORM_IDS[preset]


def game_platform_ids(game: Game) -> frozenset[int]:
    """Collect every platform ID mentioned by a game, from any source."""
    ids = {platform.id for platform in game.platforms}
    ids.update(
        entry.platform_id
        for entry in game.release_dates
        if entry.platform_id is not None
    )
    return frozenset(ids)


def matches_preset(game: Game, preset: PlatformPreset) -> bool:
    """Return whether a game is available on at least one preset platform.

    Games without any platform information cannot be classified, so they only
    pass the unfiltered preset.
    """
    allowed = preset_platform_ids(preset)
    if allowed is None:
        return True
    return not allowed.isdisjoint(game_platform_ids(game))


def resolve_timezone(name: str) -> ZoneInfo:
    """Load a time zone by name, falling back to the default zone if unknown."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def today_in(now: datetime, tz: tzinfo) -> date:
    """Return the calendar date of ``now`` in the given time zone."""
    return _as_utc(now).astimezone(tz).date()


def _utc_date(moment: datetime) -> date:
    return datetime.fromtimestamp(_as_utc(moment).timestamp(), tz=UTC).date()


def resolve_release_date(
    game: Game,
    platform_ids: frozenset[int] | None = None,
) -> date | None:
    """Resolve the exact release calendar day of a game, if it has one.

    IGDB stores a release day as midnight UTC, so the day is read in UTC rather
    than in the guild zone, which would move it back a day west of UTC. Only
    exact-day entries count: month, year, quarter and TBD dates are never
    turned into a guessed day. When per-platform entries exist, the earliest
    exact one is used, restricted to ``platform_ids`` when given (entries that
    do not name a platform apply to every platform). Without per-platform
    entries the game-level ``first_release_date`` is used.
    """
    if game.release_dates:
        exact = [
            entry.date
            for entry in game.release_dates
            if entry.date is not None
            and entry.date_format is DateFormat.EXACT_DAY
            and (
                platform_ids is None
                or entry.platform_id is None
                or entry.platform_id in platform_ids
            )
        ]
        return _utc_date(min(exact, key=_as_utc)) if exact else None
    if game.first_release_date is None:
        return None
    return _utc_date(game.first_release_date)


def days_until(release_date: date, today: date) -> int:
    """Return the number of days from ``today`` to the release (negative if past)."""
    return (release_date - today).days


def normalize_title(title: str) -> str:
    """Reduce a title to a comparison key.

    Case, spacing, punctuation, trademark symbols and Latin accents are ignored;
    other scripts are preserved so unrelated titles do not collide.
    """
    kept: list[str] = []
    # Compatibility decomposition would turn "™" into the letters "TM".
    title = title.translate(_SYMBOL_REMOVAL)
    for char in unicodedata.normalize("NFKD", title):
        if unicodedata.combining(char) and kept and kept[-1].isascii():
            continue
        kept.append(char)
    folded = unicodedata.normalize("NFKC", "".join(kept)).casefold()
    return re.sub(r"[\W_]+", "", folded)


def _selection_key(ranked: RankedGame) -> tuple[int, int, str, int]:
    game = ranked.game
    return (-game.hypes, -game.follows, game.name.casefold(), game.id)


def _chronological_key(ranked: RankedGame) -> tuple[date, int, str, int]:
    game = ranked.game
    return (ranked.release_date, -game.hypes, game.name.casefold(), game.id)


def sort_chronologically(ranked: Iterable[RankedGame]) -> list[RankedGame]:
    """Sort by release date, then hypes descending, then name."""
    return sorted(ranked, key=_chronological_key)


def rank_upcoming(
    games: Iterable[Game],
    *,
    today: date,
    preset: PlatformPreset = PlatformPreset.ALL,
    min_hypes: int = 0,
    include_dlc: bool = False,
    window_days: int | None = None,
    limit: int = TOP_LIST_SIZE,
) -> list[RankedGame]:
    """Pick the most anticipated upcoming games and order them by release day.

    ``today`` is the current date in the guild's time zone (see ``today_in``).
    Candidates are filtered (game type, platform preset, hypes, exact release
    date not before ``today`` and optionally within ``window_days``), the most
    hyped ones are kept after deduplicating by ID and normalized title, and the
    survivors are returned in chronological order.
    """
    if limit <= 0:
        return []

    allowed_types = _DLC_GAME_TYPES if include_dlc else _BASE_GAME_TYPES
    platform_ids = preset_platform_ids(preset)
    last_day = today + timedelta(days=window_days) if window_days is not None else None

    candidates: list[RankedGame] = []
    for game in games:
        if game.version_parent is not None:
            continue
        if game.game_type is not None and game.game_type not in allowed_types:
            continue
        if game.hypes < min_hypes or not matches_preset(game, preset):
            continue
        release_date = resolve_release_date(game, platform_ids)
        if release_date is None or release_date < today:
            continue
        if last_day is not None and release_date > last_day:
            continue
        candidates.append(RankedGame(game=game, release_date=release_date))

    seen_ids: set[int] = set()
    seen_titles: set[str] = set()
    selected: list[RankedGame] = []
    for ranked in sorted(candidates, key=_selection_key):
        title_key = normalize_title(ranked.game.name)
        if ranked.game.id in seen_ids or (title_key and title_key in seen_titles):
            continue
        seen_ids.add(ranked.game.id)
        if title_key:
            seen_titles.add(title_key)
        selected.append(ranked)
        if len(selected) == limit:
            break

    return sort_chronologically(selected)
