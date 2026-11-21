"""Maps raw IGDB payloads to domain models.

The payloads come from an external API, so every field is validated: values of
the wrong type are ignored instead of raising, and a record is dropped only
when it lacks an ID or a name.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from gamereleases.domain.models import (
    DateFormat,
    Game,
    GameType,
    Platform,
    PlatformRelease,
    StoreKind,
    StoreLink,
)

logger = logging.getLogger(__name__)

COVER_URL_TEMPLATE = "https://images.igdb.com/igdb/image/upload/t_cover_big/{}.jpg"
TRAILER_URL_TEMPLATE = "https://www.youtube.com/watch?v={}"
IGDB_GAME_URL_TEMPLATE = "https://www.igdb.com/games/{}"

# IGDB website type of an official site, used when no store host matches.
OFFICIAL_WEBSITE_TYPE = 1

_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True, slots=True)
class _HostRule:
    kind: StoreKind
    hosts: tuple[str, ...]
    # Only these path prefixes match, when set.
    path_prefixes: tuple[str, ...] = ()


# Matching is by host, not by IGDB's website type enum, which has drifted.
_HOST_RULES: tuple[_HostRule, ...] = (
    _HostRule(StoreKind.STEAM, ("store.steampowered.com",)),
    _HostRule(StoreKind.EPIC, ("store.epicgames.com",)),
    _HostRule(StoreKind.EPIC, ("epicgames.com",), path_prefixes=("/store",)),
    _HostRule(StoreKind.GOG, ("gog.com",)),
    _HostRule(StoreKind.PLAYSTATION, ("store.playstation.com",)),
    _HostRule(StoreKind.XBOX, ("xbox.com", "microsoft.com")),
    _HostRule(StoreKind.NINTENDO, ("nintendo.com", "nintendo.it")),
)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def _as_mappings(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    items = (_as_mapping(item) for item in value)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return [item for item in items if item is not None]


def _reference_id(value: object) -> int | None:
    """Read an ID that IGDB returns either bare or as an expanded object."""
    mapping = _as_mapping(value)
    return _as_int(mapping.get("id")) if mapping is not None else _as_int(value)


def _timestamp(value: object) -> datetime | None:
    seconds = _as_int(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def cover_url(image_id: str) -> str:
    """Build the large cover URL for an IGDB image ID."""
    return COVER_URL_TEMPLATE.format(image_id)


def trailer_url(video_id: str) -> str:
    """Build the YouTube watch URL for a video ID."""
    return TRAILER_URL_TEMPLATE.format(video_id)


def igdb_game_url(slug: str) -> str:
    """Build the public IGDB page URL for a game slug."""
    return IGDB_GAME_URL_TEMPLATE.format(slug)


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def _parse_web_url(url: str) -> tuple[str, str] | None:
    """Return ``(host, path)`` for an http(s) URL, or ``None`` for anything else."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    return host, parts.path.lower()


def classify_store_url(url: str) -> StoreKind | None:
    """Return the store a URL belongs to, judging by its host, or ``None``."""
    parsed = _parse_web_url(url)
    if parsed is None:
        return None
    host, path = parsed
    for rule in _HOST_RULES:
        if not any(_host_matches(host, domain) for domain in rule.hosts):
            continue
        if rule.path_prefixes and not path.startswith(rule.path_prefixes):
            continue
        return rule.kind
    return None


def extract_stores(
    websites: Iterable[Mapping[str, object]],
    slug: str | None,
) -> tuple[StoreLink, ...]:
    """Pick one link per destination, ordered by title-link priority.

    Store hosts win over the website type. A link that is not a known store is
    kept as the official site only if IGDB flags it as such. The IGDB page is
    always appended when a slug is available.
    """
    found: dict[StoreKind, str] = {}
    for website in websites:
        url = _as_str(website.get("url"))
        if url is None or _parse_web_url(url) is None:
            continue
        kind = classify_store_url(url)
        if kind is None:
            if _reference_id(website.get("type")) != OFFICIAL_WEBSITE_TYPE:
                continue
            kind = StoreKind.OFFICIAL
        found.setdefault(kind, url.strip())
    if slug:
        found.setdefault(StoreKind.IGDB, igdb_game_url(slug))
    return tuple(
        StoreLink(kind=kind, url=url)
        for kind, url in sorted(found.items(), key=lambda item: item[0].priority)
    )


def _map_release_date(raw: Mapping[str, object]) -> PlatformRelease:
    date_format = _as_int(raw.get("date_format"))
    try:
        parsed_format = DateFormat(date_format) if date_format is not None else None
    except ValueError:
        parsed_format = None
    return PlatformRelease(
        platform_id=_reference_id(raw.get("platform")),
        date=_timestamp(raw.get("date")),
        date_format=parsed_format,
        human=_as_str(raw.get("human")),
    )


def _map_platform(raw: Mapping[str, object]) -> Platform | None:
    platform_id = _as_int(raw.get("id"))
    if platform_id is None:
        return None
    name = _as_str(raw.get("name")) or f"Platform {platform_id}"
    return Platform(
        id=platform_id, name=name, abbreviation=_as_str(raw.get("abbreviation"))
    )


def _map_cover(raw: Mapping[str, object]) -> str | None:
    cover = _as_mapping(raw.get("cover"))
    image_id = _as_str(cover.get("image_id")) if cover else None
    if image_id is None or not _SAFE_ID.fullmatch(image_id):
        return None
    return cover_url(image_id)


def _map_trailer(raw: Mapping[str, object]) -> str | None:
    """Return the first usable video as a YouTube URL."""
    for video in _as_mappings(raw.get("videos")):
        video_id = _as_str(video.get("video_id"))
        if video_id is not None and _SAFE_ID.fullmatch(video_id):
            return trailer_url(video_id)
    return None


def _map_game_type(value: object) -> GameType | None:
    type_id = _reference_id(value)
    try:
        return GameType(type_id) if type_id is not None else None
    except ValueError:
        return None


def map_game(raw: Mapping[str, object]) -> Game | None:
    """Convert one IGDB game payload, or return ``None`` if it is unusable."""
    game_id = _as_int(raw.get("id"))
    name = _as_str(raw.get("name"))
    if game_id is None or name is None:
        return None
    slug = _as_str(raw.get("slug"))
    rating = raw.get("total_rating")

    platforms = (_map_platform(item) for item in _as_mappings(raw.get("platforms")))
    genres = (_as_str(item.get("name")) for item in _as_mappings(raw.get("genres")))

    return Game(
        id=game_id,
        name=name.strip(),
        slug=slug or "",
        summary=_as_str(raw.get("summary")),
        first_release_date=_timestamp(raw.get("first_release_date")),
        hypes=_as_int(raw.get("hypes")) or 0,
        follows=_as_int(raw.get("follows")) or 0,
        total_rating=(
            float(rating)
            if isinstance(rating, int | float) and not isinstance(rating, bool)
            else None
        ),
        total_rating_count=_as_int(raw.get("total_rating_count")) or 0,
        game_type=_map_game_type(raw.get("game_type")),
        version_parent=_reference_id(raw.get("version_parent")),
        cover_url=_map_cover(raw),
        trailer_url=_map_trailer(raw),
        genres=tuple(genre for genre in genres if genre is not None),
        platforms=tuple(p for p in platforms if p is not None),
        release_dates=tuple(
            _map_release_date(item) for item in _as_mappings(raw.get("release_dates"))
        ),
        stores=extract_stores(_as_mappings(raw.get("websites")), slug),
    )


def map_games(raw_games: Iterable[object]) -> list[Game]:
    """Convert a list of IGDB payloads, skipping malformed records."""
    games: list[Game] = []
    for raw in raw_games:
        mapping = _as_mapping(raw)
        game = map_game(mapping) if mapping is not None else None
        if game is None:
            logger.warning("Skipping malformed IGDB game record")
            continue
        games.append(game)
    return games
