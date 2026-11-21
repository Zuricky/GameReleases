"""Twitch OAuth2 client-credentials token management for IGDB."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

HTTP_OK = 200
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
# Refresh when fewer than this many seconds of validity remain.
REFRESH_MARGIN_SECONDS = 300.0

logger = logging.getLogger(__name__)


class TwitchAuthError(Exception):
    """Raised when an access token cannot be obtained."""


@dataclass(frozen=True, slots=True)
class _Token:
    value: str
    expires_at: float


class TwitchAuth:
    """Caches an app access token and refreshes it before it expires.

    A single lock serializes refreshes, so concurrent callers that find the
    token stale trigger one request to Twitch instead of one each.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        http: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: _Token | None = None

    def _is_fresh(self, token: _Token | None) -> bool:
        return (
            token is not None
            and token.expires_at - self._clock() > REFRESH_MARGIN_SECONDS
        )

    async def get_token(self) -> str:
        """Return a valid access token, fetching a new one if needed."""
        token = self._token
        if token is not None and self._is_fresh(token):
            return token.value
        async with self._lock:
            # Another task may have refreshed while this one waited.
            if self._token is not None and self._is_fresh(self._token):
                return self._token.value
            return await self._fetch()

    async def refresh_rejected(self, rejected: str) -> str:
        """Return a new token after the API rejected ``rejected`` with a 401.

        If another task already replaced the rejected token, the current one is
        returned without another round trip to Twitch.
        """
        async with self._lock:
            current = self._token
            if current is not None and current.value != rejected:
                return current.value
            return await self._fetch()

    async def _fetch(self) -> str:
        try:
            response = await self._http.post(
                TOKEN_URL,
                # Sent in the body so the secret never appears in a logged URL.
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "client_credentials",
                },
            )
        except httpx.HTTPError as exc:
            raise TwitchAuthError(
                f"token request failed: {type(exc).__name__}"
            ) from exc
        if response.status_code != HTTP_OK:
            raise TwitchAuthError(
                f"token request rejected with status {response.status_code}"
            )
        try:
            payload = response.json()
            value = payload["access_token"]
            lifetime = float(payload["expires_in"])
        except (ValueError, KeyError, TypeError) as exc:
            raise TwitchAuthError("token response is malformed") from exc
        if not isinstance(value, str) or not value:
            raise TwitchAuthError("token response is malformed")

        self._token = _Token(value=value, expires_at=self._clock() + lifetime)
        logger.info("Obtained a Twitch access token valid for %.0f s", lifetime)
        return value
