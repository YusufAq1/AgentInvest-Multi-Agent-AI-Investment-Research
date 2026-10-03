"""FRED macro data (risk-free rate, CPI, real GDP).

WHY a raw httpx call instead of the `fredapi` PyPI package: `fredapi` is
lightly maintained (no release since May 2024) and doesn't cleanly expose
the `realtime_end` parameter this module depends on for point-in-time
correctness — one fewer dependency, more control over the exact request.

WHY this doesn't share http.py's SecHttpClient: that client is scoped to
`data.sec.gov`/`www.sec.gov` with SEC's identity/rate-limit requirements.
FRED is a different host with its own (unofficial, community-reported —
not found in FRED's own docs) ~120 req/min limit; sharing SEC's limiter
would either be meaningless or wrongly conflate the two.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Final, Literal

import httpx

from backend.core.config import Settings
from backend.data.cache import Cache
from backend.data.errors import PermanentDataError, TransientDataError
from backend.data.models import DataUnavailable, MacroObservation
from backend.data.retry import with_retry

_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
_CACHE_SOURCE = "fred_observations"

# Series IDs verified 2026-09-16 against FRED's own documentation.
# GDPC1 (real GDP) is used deliberately, not plain GDP (nominal).
SERIES_IDS: Final[dict[str, str]] = {
    "risk_free_rate": "DGS10",
    "cpi": "CPIAUCSL",
    "real_gdp": "GDPC1",
}

SeriesName = Literal["risk_free_rate", "cpi", "real_gdp"]


class MacroClient:
    """Fetches point-in-time-correct FRED observations."""

    def __init__(
        self, settings: Settings, cache: Cache, client: httpx.AsyncClient | None = None
    ) -> None:
        self._settings = settings
        self._cache = cache
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))

    async def get_series(
        self, series: SeriesName, as_of: date, *, lookback_days: int = 3650
    ) -> list[MacroObservation] | DataUnavailable:
        """Returns observations for `series` from `as_of - lookback_days`
        through `as_of`, using ONLY values that were the published vintage
        as of `as_of` (see module docstring on `realtime_end`).
        """
        series_id = SERIES_IDS[series]
        observation_start = as_of.fromordinal(as_of.toordinal() - lookback_days)

        raw = await self._get_raw_observations(
            series_id, observation_start=observation_start, as_of=as_of
        )
        observations = [
            MacroObservation(series_id=series_id, date=date.fromisoformat(obs["date"]), value=v)
            for obs in raw.get("observations", [])
            if (v := _parse_value(obs["value"])) is not None
        ]

        if not observations:
            return DataUnavailable(
                source="fred",
                identifier=series_id,
                as_of=as_of,
                reason=f"No FRED observations for {series_id} in the requested window",
                attempted_at=datetime.now(UTC),
            )
        return observations

    async def _get_raw_observations(
        self, series_id: str, *, observation_start: date, as_of: date
    ) -> dict[str, Any]:
        args = {
            "series_id": series_id,
            "observation_start": observation_start.isoformat(),
            "observation_end": as_of.isoformat(),
            "realtime_start": as_of.isoformat(),
            "realtime_end": as_of.isoformat(),
        }
        cached = await self._cache.get(source=_CACHE_SOURCE, args=args, as_of=as_of)
        if cached is not None:
            return cached

        retrying_get = with_retry(
            max_attempts=self._settings.data_retry_max_attempts,
            base_delay_s=self._settings.data_retry_base_delay_s,
            max_delay_s=self._settings.data_retry_max_delay_s,
        )(self._fetch_once)
        raw = await retrying_get(series_id, observation_start, as_of)
        await self._cache.set(source=_CACHE_SOURCE, args=args, as_of=as_of, payload=raw)
        return raw

    async def _fetch_once(
        self, series_id: str, observation_start: date, as_of: date
    ) -> dict[str, Any]:
        params = {
            "series_id": series_id,
            "api_key": self._settings.fred_api_key.get_secret_value(),
            "file_type": "json",
            "observation_start": observation_start.isoformat(),
            # Both of these are required for true point-in-time correctness:
            # observation_end excludes future-dated periods; realtime_end
            # excludes values *revised* after as_of for an in-range period
            # (e.g. a GDP print revised months after first release). Using
            # only one is insufficient.
            #
            # WHY realtime_start = as_of too (fixed in Phase 6): sending only
            # realtime_end makes FRED default realtime_start to 1776-07-04,
            # which for a daily series like DGS10 spans thousands of
            # vintages and FRED rejects it with 400 ("4562 vintage dates ...
            # exceeds the maximum ... (2000)"). Pinning both ends to as_of
            # asks for exactly one vintage, "the data as it was known on
            # as_of", which is the point-in-time question in the first place.
            "observation_end": as_of.isoformat(),
            "realtime_start": as_of.isoformat(),
            "realtime_end": as_of.isoformat(),
        }
        try:
            response = await self._client.get(_OBSERVATIONS_URL, params=params)
        except httpx.TimeoutException as exc:
            raise TransientDataError(f"Timed out calling FRED for {series_id}") from exc
        except httpx.TransportError as exc:
            raise TransientDataError(f"Transport error calling FRED for {series_id}") from exc

        # FRED's official docs don't publish an exact rate limit — the
        # community-corroborated figure is ~120 req/min, unconfirmed
        # officially. Treat 429 as transient regardless of the exact number.
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientDataError(f"{response.status_code} from FRED for {series_id}")
        if response.status_code >= 400:
            raise PermanentDataError(f"{response.status_code} from FRED for {series_id}")

        try:
            result: dict[str, Any] = response.json()
        except ValueError as exc:
            raise PermanentDataError(f"Non-JSON response from FRED for {series_id}") from exc
        return result

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse_value(raw: str) -> float | None:
    # FRED represents a missing observation as the literal string ".".
    if raw == ".":
        return None
    return float(raw)
