"""Ticker -> CIK resolution, shared by xbrl.py and news.py (both need a
zero-padded 10-digit CIK, not a ticker, to build their SEC URLs).

WHY this is its own module rather than duplicated in xbrl.py and news.py:
the same mapping, the same cache entry, and the same "ticker not found"
handling are needed by both callers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from backend.data.cache import Cache
from backend.data.errors import CikNotFoundError, UpstreamSchemaError
from backend.data.http import SecHttpClient

_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_CACHE_SOURCE = "cik_map"


class CikResolver:
    """Resolves a ticker to its zero-padded 10-digit SEC CIK.

    KNOWN, DOCUMENTED LIMITATION: `company_tickers.json` is a live,
    current-day snapshot with no historical/point-in-time version. `as_of`
    is still accepted and threaded through (C3 — no exceptions, ever) and
    used as part of the cache freshness key, but it does NOT change which
    mapping is fetched. A ticker that changed its CIK association at some
    point in history (rare) is a known, unsolved gap for a system whose
    purpose is eventually enabling backtesting at arbitrary historical
    `as_of` dates — silently ignoring this would violate §0.4's "no silent
    fallbacks," so it's documented here instead, to revisit later.
    """

    def __init__(self, http: SecHttpClient, cache: Cache) -> None:
        self._http = http
        self._cache = cache

    async def resolve(self, ticker: str, as_of: date) -> str:
        """Returns the zero-padded 10-digit CIK for `ticker`.

        Raises `CikNotFoundError` for an unknown ticker — callers in
        xbrl.py/news.py catch this and convert it to `DataUnavailable`,
        since a mistyped or delisted ticker is a normal, expected outcome,
        not a bug (see errors.py's module docstring on that distinction).
        """
        mapping = await self._get_mapping(as_of)
        cik = mapping.get(ticker.upper())
        if cik is None:
            raise CikNotFoundError(f"No CIK found for ticker {ticker!r}")
        return cik

    async def _get_mapping(self, as_of: date) -> dict[str, str]:
        # WHY date.today() as the cache key, not `as_of`: this snapshot has
        # no historical version (see class docstring) — keying on the
        # fetch day approximates the configured
        # `cache_snapshot_ttl_hours` (default 24h) as "refetch once per
        # calendar day," which is precise enough for Phase 1's scale.
        fetch_day = datetime.now(UTC).date()
        cached = await self._cache.get(source=_CACHE_SOURCE, args={}, as_of=fetch_day)
        if cached is not None:
            return _build_mapping(cached)

        raw = await self._http.get_json(_TICKER_MAP_URL)
        await self._cache.set(source=_CACHE_SOURCE, args={}, as_of=fetch_day, payload=raw)
        return _build_mapping(raw)


def _build_mapping(raw: dict[str, object]) -> dict[str, str]:
    """`company_tickers.json` is `{"0": {"cik_str": 320193, "ticker": "AAPL", ...}, "1": {...}}`.

    `cik_str` is NOT zero-padded in the source file — we pad it ourselves.
    """
    mapping: dict[str, str] = {}
    for entry in raw.values():
        if not isinstance(entry, dict) or "ticker" not in entry or "cik_str" not in entry:
            raise UpstreamSchemaError(
                "company_tickers.json entry missing expected 'ticker'/'cik_str' fields"
            )
        ticker = str(entry["ticker"]).upper()
        cik = str(entry["cik_str"]).zfill(10)
        mapping[ticker] = cik
    return mapping
