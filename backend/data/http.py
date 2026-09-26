"""Shared async HTTP client for direct SEC calls (companyfacts, submissions,
the ticker->CIK mapping file).

WHY this is separate from edgar.py/prices.py: those two wrap synchronous
third-party libraries (edgartools, yfinance) via `asyncio.to_thread` and
manage their own transport internally. Everything in xbrl.py/news.py/cik.py
instead talks to `data.sec.gov`/`www.sec.gov` directly over plain JSON, so
they share one real `httpx.AsyncClient`, one rate limiter, and one
User-Agent — set up once here rather than three times.

WHY not FRED too: FRED is a different host with no SEC identity requirement
and a different (unofficial, community-reported) rate limit. `macro.py`
owns its own bare `httpx.AsyncClient` instead of sharing this one.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from backend.core.config import Settings
from backend.data.errors import PermanentDataError, SecIdentityRejectedError, TransientDataError
from backend.data.retry import with_retry


class SecRateLimiter:
    """Enforces a minimum spacing between requests so combined traffic from
    this client stays under `settings.sec_requests_per_second`.

    WHY a single lock + last-request timestamp instead of a real token
    bucket: at Phase 1's scale (one research run, one ticker at a time)
    this is enough to stay under SEC's ceiling for our own direct calls.

    KNOWN, ACCEPTED GAP: this only throttles requests made through this
    client. `edgar.py` uses edgartools, which runs its own internal rate
    limiter (~9 req/s by default) against the same SEC infrastructure,
    entirely independently of this one. Under concurrent use, the combined
    real rate to SEC could momentarily exceed the 10/s ceiling. A
    cross-process shared limiter isn't justified at this scale — this is a
    documented, accepted simplification, not an oversight.

    Phase 5 update: agents now genuinely run concurrently, so this gap is
    reachable in a normal run. edgartools' rate can't be lowered from our
    config: it only reads EDGAR_RATE_LIMIT_PER_SEC from the environment at
    first import. See ADR-0001.
    """

    def __init__(self, requests_per_second: int) -> None:
        self._min_interval = 1.0 / requests_per_second
        self._lock = asyncio.Lock()
        self._last_request_at: float | None = None

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._last_request_at is not None:
                wait = self._last_request_at + self._min_interval - now
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()


class SecHttpClient:
    """One shared client for direct `data.sec.gov`/`www.sec.gov` JSON calls.

    The `client` constructor parameter lets tests inject a `respx`-mocked
    `httpx.AsyncClient` so no test ever reaches the real network — same
    injection pattern as Phase 0's `ClaudeClient`.
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": settings.edgar_identity},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        self._limiter = SecRateLimiter(settings.sec_requests_per_second)

    async def get_json(self, url: str) -> dict[str, Any]:
        """GET `url`, rate-limited, retried on transient failure, returning
        parsed JSON. Raises `PermanentDataError`/`SecIdentityRejectedError`
        on non-retryable failure, `TransientDataError` after retries are
        exhausted (see retry.py) — callers decide whether to catch and
        convert to `DataUnavailable` or let it propagate.
        """
        retrying_get = with_retry(
            max_attempts=self._settings.data_retry_max_attempts,
            base_delay_s=self._settings.data_retry_base_delay_s,
            max_delay_s=self._settings.data_retry_max_delay_s,
        )(self._get_json_once)
        return await retrying_get(url)

    async def _get_json_once(self, url: str) -> dict[str, Any]:
        await self._limiter.acquire()
        try:
            response = await self._client.get(url)
        except httpx.TimeoutException as exc:
            raise TransientDataError(f"Timed out calling {url}") from exc
        except httpx.TransportError as exc:
            raise TransientDataError(f"Transport error calling {url}") from exc

        if response.status_code == 403:
            raise SecIdentityRejectedError(
                f"SEC rejected our identity (403) for {url} — check EDGAR_IDENTITY."
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientDataError(f"{response.status_code} from {url}")
        if response.status_code >= 400:
            raise PermanentDataError(f"{response.status_code} from {url}")

        try:
            result: dict[str, Any] = response.json()
        except ValueError as exc:
            raise PermanentDataError(f"Non-JSON response from {url}") from exc
        return result

    async def aclose(self) -> None:
        await self._client.aclose()
