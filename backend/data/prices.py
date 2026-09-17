"""Daily price history via yfinance.

WHY yfinance only, no Stooq fallback: CLAUDE.md §4 names Stooq as the
fallback, but its plain CSV endpoint is currently gated behind a JS
anti-bot proof-of-work challenge and, per a corroborating ~April 2026
GitHub issue, now appears to require a per-key API of unverified free-tier
status. Per the explicit decision recorded in ADR-0011, Phase 1 ships
yfinance as the sole price source; failures return `DataUnavailable`
rather than a fabricated or silently-substituted result (C6).

WHY yfinance is treated as best-effort here, never on a critical path
(matching CLAUDE.md §4's own characterization): it now hard-depends on
`curl_cffi` for browser-TLS impersonation because Yahoo actively blocks
plain HTTP client traffic — confirmed via its current dependency metadata.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import yfinance as yf

from backend.core.config import Settings
from backend.data.errors import TransientDataError
from backend.data.models import DataUnavailable, PriceBar
from backend.data.retry import with_retry


class PricesClient:
    """Fetches daily OHLCV price history for one ticker."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def get_price_history(
        self, ticker: str, as_of: date, *, lookback_days: int = 365, interval: str = "1d"
    ) -> list[PriceBar] | DataUnavailable:
        """Returns daily bars from `as_of - lookback_days` through `as_of`.

        Any exception, timeout, or empty result is bounded by
        `with_retry`'s `max_attempts` (never retried forever) and converted
        to `DataUnavailable` — yfinance's own reliability is explicitly
        best-effort, so a failure here must never crash a research run.
        """
        start = as_of.fromordinal(as_of.toordinal() - lookback_days)

        retrying_fetch = with_retry(
            max_attempts=self._settings.data_retry_max_attempts,
            base_delay_s=self._settings.data_retry_base_delay_s,
            max_delay_s=self._settings.data_retry_max_delay_s,
        )(self._fetch_history)
        try:
            bars = await retrying_fetch(ticker, start, as_of, interval)
        except TransientDataError:
            return DataUnavailable(
                source="yfinance",
                identifier=ticker,
                as_of=as_of,
                reason=f"yfinance failed to return price history for {ticker!r} after retries",
                attempted_at=datetime.now(UTC),
            )

        if not bars:
            return DataUnavailable(
                source="yfinance",
                identifier=ticker,
                as_of=as_of,
                reason=f"No price data returned for {ticker!r} in the requested window",
                attempted_at=datetime.now(UTC),
            )
        return bars

    async def _fetch_history(
        self, ticker: str, start: date, as_of: date, interval: str
    ) -> list[PriceBar]:
        return await asyncio.to_thread(self._fetch_history_sync, ticker, start, as_of, interval)

    def _fetch_history_sync(
        self, ticker: str, start: date, as_of: date, interval: str
    ) -> list[PriceBar]:
        # WHY raise_errors=True (not yfinance's own default of False): the
        # default silently swallows network/HTTP failures into an empty
        # DataFrame, indistinguishable from "this ticker genuinely has no
        # data in this range." Raising lets us treat the two cases
        # differently — a real failure is retried by with_retry, while a
        # genuinely empty (but successful) result goes straight to
        # DataUnavailable without wasting retries on it.
        try:
            frame = yf.Ticker(ticker).history(
                start=start, end=as_of, interval=interval, raise_errors=True
            )
        except Exception as exc:
            # WHY a broad `except Exception` at this one boundary: yfinance
            # doesn't expose a stable, documented exception hierarchy to
            # catch more narrowly (it wraps whatever the underlying
            # curl_cffi/requests transport raises). This is the single seam
            # between our code and that library — everything past it is one
            # of our own typed exceptions.
            raise TransientDataError(f"yfinance failed for {ticker}") from exc

        bars: list[PriceBar] = []
        for index, row in frame.iterrows():
            bar_date = index.date()
            if bar_date > as_of:
                # Defensive re-check: don't trust the library's own end
                # boundary semantics, which have changed across versions.
                continue
            bars.append(
                PriceBar(
                    date=bar_date,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=int(row["Volume"]),
                )
            )
        return bars
