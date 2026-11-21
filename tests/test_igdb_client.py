"""Tests for Twitch authentication, the TTL cache and the IGDB client."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import httpx
import pytest

from gamereleases.igdb.auth import REFRESH_MARGIN_SECONDS, TwitchAuth, TwitchAuthError
from gamereleases.igdb.client import (
    SEARCH_TTL_SECONDS,
    UPCOMING_TTL_SECONDS,
    IgdbClient,
    IgdbError,
    RateLimiter,
    TtlCache,
)

START = date(2026, 11, 21)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSleep:
    """Records requested delays and moves the fake clock forward."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self._clock.advance(delay)


class FakeServer:
    """Stands in for both the Twitch token endpoint and the IGDB API."""

    def __init__(self) -> None:
        self.token_requests: list[httpx.Request] = []
        self.api_requests: list[httpx.Request] = []
        self.api_responses: list[httpx.Response | Exception] = []
        self.token_status = 200
        self.token_body: object | None = None
        self.expires_in = 3600

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        # Yield so that concurrent callers really interleave.
        await asyncio.sleep(0)
        if request.url.host == "id.twitch.tv":
            self.token_requests.append(request)
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"message": "nope"})
            body = self.token_body or {
                "access_token": f"token-{len(self.token_requests)}",
                "expires_in": self.expires_in,
                "token_type": "bearer",
            }
            return httpx.Response(200, json=body)
        self.api_requests.append(request)
        if not self.api_responses:
            return httpx.Response(200, json=[])
        item = self.api_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Stack:
    def __init__(
        self,
        server: FakeServer,
        clock: FakeClock,
        sleep: FakeSleep,
        http: httpx.AsyncClient,
    ) -> None:
        self.server = server
        self.clock = clock
        self.sleep = sleep
        self.http = http
        self.auth = TwitchAuth("client-id", "client-secret", http, clock=clock)
        self.client = IgdbClient("client-id", self.auth, http, clock=clock, sleep=sleep)


@pytest.fixture
async def stack() -> AsyncIterator[Stack]:
    server = FakeServer()
    clock = FakeClock()
    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    try:
        yield Stack(server, clock, FakeSleep(clock), http)
    finally:
        await http.aclose()


def game_json(game_id: int, name: str) -> dict[str, object]:
    return {"id": game_id, "name": name, "slug": f"game-{game_id}", "hypes": 1}


def ok(*games: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json=list(games))


# --- OAuth token handling ----------------------------------------------------


async def test_token_is_requested_with_client_credentials_in_the_body(
    stack: Stack,
) -> None:
    token = await stack.auth.get_token()

    assert token == "token-1"
    request = stack.server.token_requests[0]
    assert request.method == "POST"
    assert request.url.query == b""
    body = request.content.decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=client-id" in body
    assert "client_secret=client-secret" in body


async def test_token_is_cached_while_it_is_valid(stack: Stack) -> None:
    first = await stack.auth.get_token()
    stack.clock.advance(1800)
    second = await stack.auth.get_token()

    assert first == second == "token-1"
    assert len(stack.server.token_requests) == 1


async def test_token_is_refreshed_before_it_expires(stack: Stack) -> None:
    await stack.auth.get_token()

    # Still outside the refresh margin: reuse.
    stack.clock.advance(3600 - REFRESH_MARGIN_SECONDS - 1)
    assert await stack.auth.get_token() == "token-1"

    # Inside the margin: replace.
    stack.clock.advance(2)
    assert await stack.auth.get_token() == "token-2"
    assert len(stack.server.token_requests) == 2


async def test_token_valid_for_less_than_the_margin_is_always_renewed(
    stack: Stack,
) -> None:
    stack.server.expires_in = 100
    await stack.auth.get_token()
    await stack.auth.get_token()
    assert len(stack.server.token_requests) == 2


async def test_concurrent_callers_share_one_token_request(stack: Stack) -> None:
    tokens = await asyncio.gather(*(stack.auth.get_token() for _ in range(8)))

    assert set(tokens) == {"token-1"}
    assert len(stack.server.token_requests) == 1


async def test_concurrent_refreshes_after_rejection_hit_twitch_once(
    stack: Stack,
) -> None:
    rejected = await stack.auth.get_token()
    tokens = await asyncio.gather(
        *(stack.auth.refresh_rejected(rejected) for _ in range(8))
    )

    assert set(tokens) == {"token-2"}
    assert len(stack.server.token_requests) == 2


async def test_rejected_token_that_was_already_replaced_is_not_refetched(
    stack: Stack,
) -> None:
    old = await stack.auth.get_token()
    stack.clock.advance(3600)
    new = await stack.auth.get_token()

    assert await stack.auth.refresh_rejected(old) == new
    assert len(stack.server.token_requests) == 2


async def test_rejection_of_the_current_token_forces_a_new_one(stack: Stack) -> None:
    current = await stack.auth.get_token()
    assert await stack.auth.refresh_rejected(current) == "token-2"


@pytest.mark.parametrize("status", [400, 401, 403, 500])
async def test_failed_token_request_raises_without_leaking_secrets(
    stack: Stack, status: int
) -> None:
    stack.server.token_status = status

    with pytest.raises(TwitchAuthError) as excinfo:
        await stack.auth.get_token()

    assert str(status) in str(excinfo.value)
    assert "client-secret" not in str(excinfo.value)


@pytest.mark.parametrize(
    "body",
    [
        {"expires_in": 3600},
        {"access_token": "", "expires_in": 3600},
        {"access_token": "abc"},
        {"access_token": "abc", "expires_in": "soon"},
        [1, 2],
    ],
)
async def test_malformed_token_response_raises(stack: Stack, body: object) -> None:
    stack.server.token_body = body
    with pytest.raises(TwitchAuthError, match="malformed"):
        await stack.auth.get_token()


async def test_failed_refresh_leaves_the_next_call_free_to_retry(
    stack: Stack,
) -> None:
    stack.server.token_status = 500
    with pytest.raises(TwitchAuthError):
        await stack.auth.get_token()

    stack.server.token_status = 200
    assert await stack.auth.get_token() == "token-2"


async def test_transport_failure_during_token_request_is_wrapped() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
        auth = TwitchAuth("id", "secret", http)
        with pytest.raises(TwitchAuthError, match="ConnectError"):
            await auth.get_token()


# --- TTL cache ---------------------------------------------------------------


def test_cache_returns_values_until_they_expire() -> None:
    clock = FakeClock()
    cache: TtlCache[str, int] = TtlCache(60, clock=clock)
    cache.set("a", 1)

    assert cache.get("a") == 1
    clock.advance(59.9)
    assert cache.get("a") == 1
    clock.advance(0.1)
    assert cache.get("a") is None
    assert len(cache) == 0


def test_cache_miss_returns_none() -> None:
    assert TtlCache[str, int](60).get("missing") is None


def test_setting_a_key_again_restarts_its_lifetime() -> None:
    clock = FakeClock()
    cache: TtlCache[str, int] = TtlCache(60, clock=clock)
    cache.set("a", 1)
    clock.advance(50)
    cache.set("a", 2)
    clock.advance(50)

    assert cache.get("a") == 2


def test_cache_evicts_expired_then_oldest_entries_when_full() -> None:
    clock = FakeClock()
    cache: TtlCache[str, int] = TtlCache(60, max_entries=2, clock=clock)
    cache.set("old", 1)
    clock.advance(30)
    cache.set("mid", 2)
    cache.set("new", 3)

    assert cache.get("old") is None
    assert cache.get("mid") == 2
    assert cache.get("new") == 3

    clock.advance(60)  # "mid" and "new" are now expired too
    cache.set("fresh", 4)
    assert len(cache) == 1


def test_cache_clear() -> None:
    cache: TtlCache[str, int] = TtlCache(60)
    cache.set("a", 1)
    cache.clear()
    assert cache.get("a") is None


# --- rate limiting -----------------------------------------------------------


async def test_requests_are_spaced_to_four_per_second() -> None:
    clock = FakeClock()
    sleep = FakeSleep(clock)
    limiter = RateLimiter(4, clock=clock, sleep=sleep)

    for _ in range(5):
        await limiter.acquire()

    assert sleep.delays == pytest.approx([0.25, 0.25, 0.25, 0.25])


async def test_idle_time_is_not_banked_as_burst_capacity() -> None:
    clock = FakeClock()
    sleep = FakeSleep(clock)
    limiter = RateLimiter(4, clock=clock, sleep=sleep)

    await limiter.acquire()
    clock.advance(10)
    await limiter.acquire()
    await limiter.acquire()

    assert sleep.delays == pytest.approx([0.25])


async def test_concurrent_acquires_are_serialized() -> None:
    clock = FakeClock()
    sleep = FakeSleep(clock)
    limiter = RateLimiter(4, clock=clock, sleep=sleep)

    await asyncio.gather(*(limiter.acquire() for _ in range(4)))

    assert sum(sleep.delays) == pytest.approx(0.75)


# --- upcoming games ----------------------------------------------------------


async def test_upcoming_request_is_authenticated_and_well_formed(
    stack: Stack,
) -> None:
    stack.server.api_responses = [ok(game_json(1, "Alpha"), game_json(2, "Beta"))]

    games = await stack.client.upcoming_games(start=START, days=30)

    assert [g.name for g in games] == ["Alpha", "Beta"]
    request = stack.server.api_requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.igdb.com/v4/games"
    assert request.headers["Client-ID"] == "client-id"
    assert request.headers["Authorization"] == "Bearer token-1"
    assert request.headers["Accept"] == "application/json"

    query = request.content.decode()
    lower = int(datetime(2026, 11, 20, tzinfo=UTC).timestamp())
    upper = int(datetime(2026, 12, 23, tzinfo=UTC).timestamp()) - 1
    assert f"first_release_date >= {lower}" in query
    assert f"first_release_date <= {upper}" in query
    assert "version_parent = null" in query
    assert "game_type = (0)" in query
    assert "sort hypes desc" in query
    for field in ("cover.image_id", "release_dates.date_format", "videos.video_id"):
        assert field in query


async def test_dlc_inclusion_changes_the_query(stack: Stack) -> None:
    await stack.client.upcoming_games(start=START, days=30, include_dlc=True)
    assert "game_type = (0,2)" in stack.server.api_requests[0].content.decode()


async def test_upcoming_window_is_clamped_to_ninety_days(stack: Stack) -> None:
    await stack.client.upcoming_games(start=START, days=500)
    upper = int(datetime(2027, 2, 21, tzinfo=UTC).timestamp()) - 1  # start + 92 d
    assert f"<= {upper}" in stack.server.api_requests[0].content.decode()


async def test_upcoming_results_are_cached_for_thirty_minutes(stack: Stack) -> None:
    stack.server.api_responses = [ok(game_json(1, "Alpha")), ok(game_json(2, "Beta"))]

    first = await stack.client.upcoming_games(start=START, days=30)
    stack.clock.advance(UPCOMING_TTL_SECONDS - 1)
    second = await stack.client.upcoming_games(start=START, days=30)

    assert first == second
    assert len(stack.server.api_requests) == 1

    stack.clock.advance(1)
    third = await stack.client.upcoming_games(start=START, days=30)
    assert [g.name for g in third] == ["Beta"]
    assert len(stack.server.api_requests) == 2


async def test_bypassing_the_cache_refetches_and_refreshes_it(stack: Stack) -> None:
    stack.server.api_responses = [ok(game_json(1, "Old")), ok(game_json(2, "New"))]

    await stack.client.upcoming_games(start=START, days=30)
    fresh = await stack.client.upcoming_games(start=START, days=30, bypass_cache=True)
    cached = await stack.client.upcoming_games(start=START, days=30)

    assert [g.name for g in fresh] == ["New"]
    assert [g.name for g in cached] == ["New"]
    assert len(stack.server.api_requests) == 2


async def test_different_parameters_use_separate_cache_entries(stack: Stack) -> None:
    await stack.client.upcoming_games(start=START, days=30)
    await stack.client.upcoming_games(start=START, days=60)
    await stack.client.upcoming_games(start=START, days=30, include_dlc=True)
    await stack.client.upcoming_games(start=date(2026, 11, 22), days=30)

    assert len(stack.server.api_requests) == 4


async def test_cached_results_cannot_be_corrupted_by_callers(stack: Stack) -> None:
    stack.server.api_responses = [ok(game_json(1, "Alpha"))]

    first = await stack.client.upcoming_games(start=START, days=30)
    first.clear()
    second = await stack.client.upcoming_games(start=START, days=30)

    assert [g.name for g in second] == ["Alpha"]


async def test_malformed_records_are_skipped(stack: Stack) -> None:
    stack.server.api_responses = [
        httpx.Response(200, json=[game_json(1, "Alpha"), {"id": 2}, "junk"])
    ]
    games = await stack.client.upcoming_games(start=START, days=30)
    assert [g.id for g in games] == [1]


async def test_clear_cache_forces_a_refetch(stack: Stack) -> None:
    await stack.client.upcoming_games(start=START, days=30)
    stack.client.clear_cache()
    await stack.client.upcoming_games(start=START, days=30)
    assert len(stack.server.api_requests) == 2


# --- game search -------------------------------------------------------------


async def test_search_builds_an_escaped_query(stack: Stack) -> None:
    await stack.client.search_games('Say "Hi" \\ now')

    query = stack.server.api_requests[0].content.decode()
    assert query.startswith('search "Say \\"Hi\\" \\\\ now";')
    assert "version_parent = null" in query


async def test_search_results_are_cached_for_fifteen_minutes(stack: Stack) -> None:
    stack.server.api_responses = [
        ok(game_json(1, "Hades")),
        ok(game_json(2, "Hades II")),
    ]

    await stack.client.search_games("hades")
    stack.clock.advance(SEARCH_TTL_SECONDS - 1)
    cached = await stack.client.search_games("hades")
    stack.clock.advance(1)
    fresh = await stack.client.search_games("hades")

    assert [g.name for g in cached] == ["Hades"]
    assert [g.name for g in fresh] == ["Hades II"]
    assert len(stack.server.api_requests) == 2


async def test_search_cache_key_ignores_case_and_spacing(stack: Stack) -> None:
    await stack.client.search_games("Elden  Ring")
    await stack.client.search_games(" elden ring ")
    assert len(stack.server.api_requests) == 1


async def test_blank_search_makes_no_request(stack: Stack) -> None:
    assert await stack.client.search_games("   ") == []
    assert stack.server.api_requests == []
    assert stack.server.token_requests == []


# --- authentication failures and retries --------------------------------------


async def test_unauthorized_response_triggers_one_refresh_and_retry(
    stack: Stack,
) -> None:
    stack.server.api_responses = [
        httpx.Response(401, json={"message": "expired"}),
        ok(game_json(1, "Alpha")),
    ]

    games = await stack.client.upcoming_games(start=START, days=30)

    assert [g.name for g in games] == ["Alpha"]
    assert len(stack.server.token_requests) == 2
    auth_headers = [r.headers["Authorization"] for r in stack.server.api_requests]
    assert auth_headers == ["Bearer token-1", "Bearer token-2"]
    # Only the 250 ms rate-limit gap: the 401 retry is not a backoff retry.
    assert stack.sleep.delays == [0.25]


async def test_second_unauthorized_response_is_an_error(stack: Stack) -> None:
    stack.server.api_responses = [httpx.Response(401), httpx.Response(401)]

    with pytest.raises(IgdbError, match="401"):
        await stack.client.upcoming_games(start=START, days=30)

    assert len(stack.server.token_requests) == 2
    assert len(stack.server.api_requests) == 2


async def test_rate_limited_requests_back_off_exponentially(stack: Stack) -> None:
    stack.server.api_responses = [
        httpx.Response(429),
        httpx.Response(429),
        httpx.Response(429),
        ok(game_json(1, "Alpha")),
    ]

    games = await stack.client.upcoming_games(start=START, days=30)

    assert [g.name for g in games] == ["Alpha"]
    backoff = [d for d in stack.sleep.delays if d >= 0.5]
    assert backoff == [0.5, 1.0, 2.0]


async def test_retry_after_header_is_honoured(stack: Stack) -> None:
    stack.server.api_responses = [
        httpx.Response(429, headers={"Retry-After": "3"}),
        ok(),
    ]
    await stack.client.upcoming_games(start=START, days=30)
    assert 3.0 in stack.sleep.delays


async def test_retry_after_is_capped(stack: Stack) -> None:
    stack.server.api_responses = [
        httpx.Response(429, headers={"Retry-After": "86400"}),
        ok(),
    ]
    await stack.client.upcoming_games(start=START, days=30)
    assert max(stack.sleep.delays) == 30.0


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_server_errors_are_retried(stack: Stack, status: int) -> None:
    stack.server.api_responses = [httpx.Response(status), ok(game_json(1, "Alpha"))]
    games = await stack.client.upcoming_games(start=START, days=30)
    assert [g.id for g in games] == [1]
    assert len(stack.server.api_requests) == 2


async def test_transport_errors_are_retried(stack: Stack) -> None:
    request = httpx.Request("POST", "https://api.igdb.com/v4/games")
    stack.server.api_responses = [
        httpx.ReadTimeout("slow", request=request),
        ok(game_json(1, "Alpha")),
    ]
    games = await stack.client.upcoming_games(start=START, days=30)
    assert [g.id for g in games] == [1]


async def test_retries_are_bounded(stack: Stack) -> None:
    stack.server.api_responses = [httpx.Response(503) for _ in range(20)]

    with pytest.raises(IgdbError, match="5 attempts"):
        await stack.client.upcoming_games(start=START, days=30)

    assert len(stack.server.api_requests) == 5


@pytest.mark.parametrize("status", [400, 403, 404])
async def test_client_errors_fail_immediately(stack: Stack, status: int) -> None:
    stack.server.api_responses = [httpx.Response(status)]

    with pytest.raises(IgdbError, match=str(status)):
        await stack.client.upcoming_games(start=START, days=30)

    assert len(stack.server.api_requests) == 1


async def test_failures_are_not_cached(stack: Stack) -> None:
    stack.server.api_responses = [httpx.Response(400), ok(game_json(1, "Alpha"))]

    with pytest.raises(IgdbError):
        await stack.client.upcoming_games(start=START, days=30)
    games = await stack.client.upcoming_games(start=START, days=30)

    assert [g.id for g in games] == [1]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(200, json={"message": "not a list"}),
    ],
)
async def test_unexpected_payloads_raise(
    stack: Stack, response: httpx.Response
) -> None:
    stack.server.api_responses = [response]
    with pytest.raises(IgdbError):
        await stack.client.upcoming_games(start=START, days=30)


async def test_api_requests_never_exceed_four_per_second(stack: Stack) -> None:
    await asyncio.gather(*(stack.client.search_games(f"query {i}") for i in range(9)))

    # Nine requests need at least eight 250 ms gaps.
    assert sum(stack.sleep.delays) == pytest.approx(2.0)


# --- Lookup by ID ------------------------------------------------------------


async def test_games_by_id_are_fetched_in_one_query(stack: Stack) -> None:
    stack.server.api_responses = [ok(game_json(2, "Two"), game_json(1, "One"))]

    games = await stack.client.get_games_by_ids([1, 2, 1])

    assert [game.id for game in games] == [1, 2]
    [request] = stack.server.api_requests
    query = request.content.decode()
    assert "where id = (1,2);" in query
    assert "limit 2;" in query


async def test_games_by_id_use_the_cache(stack: Stack) -> None:
    stack.server.api_responses = [ok(game_json(1, "One")), ok(game_json(2, "Two"))]

    await stack.client.get_games_by_ids([1])
    games = await stack.client.get_games_by_ids([1, 2])

    assert [game.name for game in games] == ["One", "Two"]
    assert len(stack.server.api_requests) == 2
    assert "where id = (2);" in stack.server.api_requests[1].content.decode()

    await stack.client.get_games_by_ids([1, 2])
    assert len(stack.server.api_requests) == 2


async def test_games_by_id_bypass_cache(stack: Stack) -> None:
    stack.server.api_responses = [ok(game_json(1, "One")), ok(game_json(1, "One v2"))]

    await stack.client.get_games_by_ids([1])
    games = await stack.client.get_games_by_ids([1], bypass_cache=True)

    assert games[0].name == "One v2"


async def test_games_by_id_omit_unknown_ids_and_skip_empty_requests(
    stack: Stack,
) -> None:
    assert await stack.client.get_games_by_ids([]) == []
    assert stack.server.api_requests == []

    stack.server.api_responses = [ok(game_json(1, "One"))]
    games = await stack.client.get_games_by_ids([404, 1])
    assert [game.id for game in games] == [1]


async def test_games_by_id_are_chunked(stack: Stack) -> None:
    ids = list(range(1, 502))

    await stack.client.get_games_by_ids(ids)

    assert len(stack.server.api_requests) == 2


async def test_upcoming_results_warm_the_game_cache(stack: Stack) -> None:
    stack.server.api_responses = [ok(game_json(5, "Five"))]
    await stack.client.upcoming_games(start=START, days=30)

    games = await stack.client.get_games_by_ids([5])

    assert [game.name for game in games] == ["Five"]
    assert len(stack.server.api_requests) == 1
