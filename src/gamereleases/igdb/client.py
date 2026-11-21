"""Async IGDB v4 client with rate limiting, retries and caching."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Hashable, Sequence
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from itertools import batched

import httpx

from gamereleases.domain.models import MAX_WINDOW_DAYS, Game, GameType
from gamereleases.igdb.auth import TwitchAuth
from gamereleases.igdb.mapper import map_games

BASE_URL = "https://api.igdb.com/v4"

HTTP_UNAUTHORIZED = 401
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR = 500

UPCOMING_TTL_SECONDS = 30 * 60
SEARCH_TTL_SECONDS = 15 * 60

MAX_REQUESTS_PER_SECOND = 4
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 30.0
REQUEST_TIMEOUT_SECONDS = 15.0
CACHE_MAX_ENTRIES = 256
# Individual games are cached too, so reminders for many games fit at once.
GAME_CACHE_MAX_ENTRIES = 2048
# IGDB returns at most 500 records per request.
GAMES_BY_ID_CHUNK_SIZE = 500

DEFAULT_UPCOMING_LIMIT = 200
DEFAULT_SEARCH_LIMIT = 10

GAME_FIELDS = (
    "id, name, slug, summary, first_release_date, hypes, follows, total_rating, "
    "total_rating_count, game_type, version_parent, cover.image_id, genres.name, "
    "platforms.id, platforms.name, platforms.abbreviation, release_dates.date, "
    "release_dates.human, release_dates.date_format, release_dates.platform, "
    "websites.url, websites.type, videos.video_id"
)

logger = logging.getLogger(__name__)


class IgdbError(Exception):
    """Raised when an IGDB request fails for good."""


class TtlCache[K: Hashable, V]:
    """A small in-memory cache whose entries expire after a fixed lifetime."""

    def __init__(
        self,
        ttl_seconds: float,
        *,
        max_entries: int = CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[K, tuple[float, V]] = {}

    def get(self, key: K) -> V | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        return value

    def set(self, key: K, value: V) -> None:
        now = self._clock()
        if len(self._entries) >= self._max_entries and key not in self._entries:
            self._entries = {
                k: entry for k, entry in self._entries.items() if entry[0] > now
            }
            if len(self._entries) >= self._max_entries:
                # Dicts keep insertion order, so the first key is the oldest.
                del self._entries[next(iter(self._entries))]
        self._entries[key] = (now + self._ttl, value)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


class RateLimiter:
    """Spaces requests at least ``1 / rate`` seconds apart."""

    def __init__(
        self,
        rate_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = 1.0 / rate_per_second
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._next_slot = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._interval
            if wait > 0:
                await self._sleep(wait)


def _escape_apicalypse(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _day_start_timestamp(day: date) -> int:
    return int(datetime.combine(day, dt_time.min, tzinfo=UTC).timestamp())


def _game_type_clause(include_dlc: bool) -> str:
    types = [GameType.MAIN_GAME]
    if include_dlc:
        types.append(GameType.EXPANSION)
    return f"game_type = ({','.join(str(int(t)) for t in types)})"


def build_upcoming_query(
    start: date, days: int, *, include_dlc: bool, limit: int
) -> str:
    """Build the query for games releasing around ``[start, start + days]``.

    The range is padded by a day on each side so that games near the edges are
    not lost to time zone offsets; exact filtering happens during ranking.
    """
    lower = _day_start_timestamp(start - timedelta(days=1))
    upper = _day_start_timestamp(start + timedelta(days=days + 2)) - 1
    return (
        f"fields {GAME_FIELDS}; "
        f"where first_release_date >= {lower} & first_release_date <= {upper} "
        f"& version_parent = null & {_game_type_clause(include_dlc)}; "
        f"sort hypes desc; limit {limit};"
    )


def build_search_query(text: str, *, limit: int) -> str:
    """Build a name search query that excludes editions of other games."""
    return (
        f'search "{_escape_apicalypse(text)}"; '
        f"fields {GAME_FIELDS}; "
        f"where version_parent = null; limit {limit};"
    )


def build_games_by_ids_query(game_ids: Sequence[int]) -> str:
    """Build a query that fetches exactly the given games.

    IGDB applies a default limit of 10 records, so the limit is set explicitly.
    """
    ids = ",".join(str(game_id) for game_id in game_ids)
    return f"fields {GAME_FIELDS}; where id = ({ids}); limit {len(game_ids)};"


class IgdbClient:
    """Fetches games from IGDB, within its rate limit and with caching.

    Requests are spaced to at most ``MAX_REQUESTS_PER_SECOND``. A 401 triggers a
    single token refresh and retry; 429, 5xx and transport errors are retried
    with exponential backoff.
    """

    def __init__(
        self,
        client_id: str,
        auth: TwitchAuth,
        http: httpx.AsyncClient,
        *,
        max_retries: int = MAX_RETRIES,
        backoff_base: float = BACKOFF_BASE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client_id = client_id
        self._auth = auth
        self._http = http
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep
        self._limiter = RateLimiter(MAX_REQUESTS_PER_SECOND, clock=clock, sleep=sleep)
        self._upcoming_cache: TtlCache[Hashable, tuple[Game, ...]] = TtlCache(
            UPCOMING_TTL_SECONDS, clock=clock
        )
        self._search_cache: TtlCache[Hashable, tuple[Game, ...]] = TtlCache(
            SEARCH_TTL_SECONDS, clock=clock
        )
        self._game_cache: TtlCache[int, Game] = TtlCache(
            SEARCH_TTL_SECONDS, max_entries=GAME_CACHE_MAX_ENTRIES, clock=clock
        )

    async def upcoming_games(
        self,
        *,
        start: date,
        days: int,
        include_dlc: bool = False,
        limit: int = DEFAULT_UPCOMING_LIMIT,
        bypass_cache: bool = False,
    ) -> list[Game]:
        """Fetch games releasing within ``days`` days of ``start``, most hyped first."""
        days = max(1, min(days, MAX_WINDOW_DAYS))
        key = ("upcoming", start, days, include_dlc, limit)
        if not bypass_cache:
            cached = self._upcoming_cache.get(key)
            if cached is not None:
                return list(cached)
        query = build_upcoming_query(start, days, include_dlc=include_dlc, limit=limit)
        games = map_games(await self._post("games", query))
        self._upcoming_cache.set(key, tuple(games))
        self._remember(games)
        return games

    async def search_games(
        self,
        text: str,
        *,
        limit: int = DEFAULT_SEARCH_LIMIT,
        bypass_cache: bool = False,
    ) -> list[Game]:
        """Search games by name."""
        normalized = " ".join(text.split())
        if not normalized:
            return []
        key = ("search", normalized.casefold(), limit)
        if not bypass_cache:
            cached = self._search_cache.get(key)
            if cached is not None:
                return list(cached)
        games = map_games(
            await self._post("games", build_search_query(normalized, limit=limit))
        )
        self._search_cache.set(key, tuple(games))
        self._remember(games)
        return games

    async def get_games_by_ids(
        self,
        game_ids: Sequence[int],
        *,
        bypass_cache: bool = False,
    ) -> list[Game]:
        """Fetch specific games, preserving the order of ``game_ids``.

        Duplicates are collapsed and unknown IDs are omitted. Games already in
        the cache are not requested again; the rest are fetched in as few
        requests as IGDB allows.
        """
        unique = list(dict.fromkeys(game_ids))
        found: dict[int, Game] = {}
        missing: list[int] = []
        for game_id in unique:
            cached = None if bypass_cache else self._game_cache.get(game_id)
            if cached is None:
                missing.append(game_id)
            else:
                found[game_id] = cached
        for chunk in batched(missing, GAMES_BY_ID_CHUNK_SIZE):
            games = map_games(
                await self._post("games", build_games_by_ids_query(chunk))
            )
            self._remember(games)
            found.update((game.id, game) for game in games)
        return [found[game_id] for game_id in unique if game_id in found]

    def _remember(self, games: Sequence[Game]) -> None:
        for game in games:
            self._game_cache.set(game.id, game)

    def clear_cache(self) -> None:
        """Drop every cached response."""
        self._upcoming_cache.clear()
        self._search_cache.clear()
        self._game_cache.clear()

    def _backoff_delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                return min(float(retry_after), MAX_BACKOFF_SECONDS)
        return min(self._backoff_base * 2**attempt, MAX_BACKOFF_SECONDS)

    async def _post(self, endpoint: str, query: str) -> list[object]:
        """POST an Apicalypse query and return the decoded JSON array."""
        url = f"{BASE_URL}/{endpoint}"
        token = await self._auth.get_token()
        refreshed = False
        attempt = 0
        while True:
            await self._limiter.acquire()
            response: httpx.Response | None = None
            try:
                response = await self._http.post(
                    url,
                    content=query,
                    headers={
                        "Client-ID": self._client_id,
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except httpx.TransportError as exc:
                failure = type(exc).__name__
            else:
                if response.status_code == HTTP_UNAUTHORIZED and not refreshed:
                    token = await self._auth.refresh_rejected(token)
                    refreshed = True
                    continue
                if response.is_success:
                    return self._decode(response)
                if response.status_code != HTTP_TOO_MANY_REQUESTS and (
                    response.status_code < HTTP_SERVER_ERROR
                ):
                    raise IgdbError(
                        f"IGDB request to {endpoint} failed with status "
                        f"{response.status_code}"
                    )
                failure = f"status {response.status_code}"

            if attempt >= self._max_retries:
                raise IgdbError(
                    f"IGDB request to {endpoint} failed after "
                    f"{attempt + 1} attempts ({failure})"
                )
            delay = self._backoff_delay(attempt, response)
            logger.warning(
                "IGDB request to %s failed (%s); retrying in %.1f s",
                endpoint,
                failure,
                delay,
            )
            await self._sleep(delay)
            attempt += 1

    @staticmethod
    def _decode(response: httpx.Response) -> list[object]:
        try:
            payload: object = response.json()
        except ValueError as exc:
            raise IgdbError("IGDB returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise IgdbError("IGDB returned an unexpected payload")
        return list(payload)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
