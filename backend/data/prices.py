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

Two price series, because one of them leaks the future (ADR-0021).
yfinance back-adjusts historical prices for every split and dividend up to
TODAY, including ones after as_of:
  - "total_return" (yfinance's auto_adjust=True): adjusted for splits AND
    dividends. Correct for RETURNS (beta), since a dividend is part of the
    return and the adjustment cancels out within each return. Wrong as a
    price LEVEL: it isn't the price anyone could trade at on that date.
  - "as_traded": the price that actually printed on that date. Built from
    auto_adjust=False (dividend-unadjusted but still split-adjusted,
    verified against yfinance 1.7.0) by multiplying back the splits dated
    after each bar. Use this for anything that multiplies price by an
    as-reported share count (market cap).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Literal

import yfinance as yf

from backend.core.config import Settings
from backend.data.errors import TransientDataError
from backend.data.models import DataUnavailable, PriceBar, StockSplit
from backend.data.retry import with_retry

PriceAdjustment = Literal["total_return", "as_traded"]


def split_factor_after(splits: Sequence[StockSplit], day: date) -> float:
    """Product of the ratios of every split dated strictly AFTER `day`.

    WHY strictly after: a split takes effect at the open, so the bar on the
    split date already trades post-split and needs no un-adjusting.
    """
    factor = 1.0
    for split in splits:
        if split.date > day:
            factor *= split.ratio
    return factor


def split_factor_between(splits: Sequence[StockSplit], after: date, through: date) -> float:
    """Product of the ratios of splits dated in (after, through]: how many
    shares one share held on `after` had become by `through`."""
    factor = 1.0
    for split in splits:
        if after < split.date <= through:
            factor *= split.ratio
    return factor


def unadjust_for_splits(bars: Sequence[PriceBar], splits: Sequence[StockSplit]) -> list[PriceBar]:
    """Turn split-adjusted bars back into as-traded bars.

    yfinance divides every price before a split by the split ratio (and
    multiplies volume by it), so multiplying back by the ratios of all
    splits after each bar's date undoes that. Example: NVDA on 2024-05-31
    is reported as 109.63 split-adjusted. One later split (10.0 on
    2024-06-10) gives 109.63 × 10 = 1,096.33, which is what it traded at.

    WHY using post-as_of splits here is not look-ahead (C3): they're used
    only to REVERSE the vendor's own future-informed adjustment, which
    recovers a number that was public on that date. No information from
    after as_of survives into the result.
    """
    unadjusted: list[PriceBar] = []
    for bar in bars:
        factor = split_factor_after(splits, bar.date)
        unadjusted.append(
            PriceBar(
                date=bar.date,
                open=bar.open * factor,
                high=bar.high * factor,
                low=bar.low * factor,
                close=bar.close * factor,
                volume=round(bar.volume / factor),
            )
        )
    return unadjusted


class PricesClient:
    """Fetches daily OHLCV price history and split history for one ticker."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def get_price_history(
        self,
        ticker: str,
        as_of: date,
        *,
        lookback_days: int = 365,
        interval: str = "1d",
        adjustment: PriceAdjustment = "total_return",
    ) -> list[PriceBar] | DataUnavailable:
        """Returns daily bars from `as_of - lookback_days` through `as_of`
        (inclusive), in the requested `adjustment` (see module docstring).

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
            bars = await retrying_fetch(
                ticker, start, as_of, interval, adjustment == "total_return"
            )
            if adjustment == "as_traded" and bars:
                # ALL splits, including after as_of: only used to undo the
                # vendor's adjustment (see unadjust_for_splits).
                all_splits = await self._fetch_all_splits_with_retry(ticker)
                bars = unadjust_for_splits(bars, all_splits)
        except TransientDataError:
            return self._unavailable(
                ticker,
                as_of,
                f"yfinance failed to return price history for {ticker!r} after retries",
            )

        if not bars:
            return self._unavailable(
                ticker, as_of, f"No price data returned for {ticker!r} in the requested window"
            )
        return bars

    async def get_splits(self, ticker: str, as_of: date) -> list[StockSplit] | DataUnavailable:
        """Splits dated on or before `as_of`, oldest first. An empty list
        means "no splits", which is common and not a failure.

        WHY filtered to as_of: anything a caller does with splits beyond
        undoing the vendor's adjustment (e.g. aligning an as-reported share
        count to a later price date) must only use splits known by as_of.
        """
        try:
            splits = await self._fetch_all_splits_with_retry(ticker)
        except TransientDataError:
            return self._unavailable(
                ticker, as_of, f"yfinance failed to return splits for {ticker!r} after retries"
            )
        return [s for s in splits if s.date <= as_of]

    def _unavailable(self, ticker: str, as_of: date, reason: str) -> DataUnavailable:
        return DataUnavailable(
            source="yfinance",
            identifier=ticker,
            as_of=as_of,
            reason=reason,
            attempted_at=datetime.now(UTC),
        )

    async def _fetch_all_splits_with_retry(self, ticker: str) -> list[StockSplit]:
        retrying = with_retry(
            max_attempts=self._settings.data_retry_max_attempts,
            base_delay_s=self._settings.data_retry_base_delay_s,
            max_delay_s=self._settings.data_retry_max_delay_s,
        )(self._fetch_all_splits)
        return await retrying(ticker)

    async def _fetch_all_splits(self, ticker: str) -> list[StockSplit]:
        return await asyncio.to_thread(self._fetch_all_splits_sync, ticker)

    def _fetch_all_splits_sync(self, ticker: str) -> list[StockSplit]:
        try:
            series = yf.Ticker(ticker).splits
        except Exception as exc:
            # WHY broad: same single yfinance seam as _fetch_history_sync.
            raise TransientDataError(f"yfinance splits failed for {ticker}") from exc
        splits = [
            StockSplit(date=index.date(), ratio=float(ratio))
            for index, ratio in series.items()
            if float(ratio) > 0
        ]
        return sorted(splits, key=lambda s: s.date)

    async def _fetch_history(
        self, ticker: str, start: date, as_of: date, interval: str, auto_adjust: bool
    ) -> list[PriceBar]:
        return await asyncio.to_thread(
            self._fetch_history_sync, ticker, start, as_of, interval, auto_adjust
        )

    def _fetch_history_sync(
        self, ticker: str, start: date, as_of: date, interval: str, auto_adjust: bool
    ) -> list[PriceBar]:
        # WHY raise_errors=True (not yfinance's own default of False): the
        # default silently swallows network/HTTP failures into an empty
        # DataFrame, indistinguishable from "this ticker genuinely has no
        # data in this range." Raising lets us treat the two cases
        # differently — a real failure is retried by with_retry, while a
        # genuinely empty (but successful) result goes straight to
        # DataUnavailable without wasting retries on it.
        # NOTE: yfinance 1.7.0 marks raise_errors deprecated (it still
        # works) in favour of yf.config.debug.hide_exceptions. Revisit
        # before upgrading yfinance.
        #
        # WHY end = as_of + 1 day: yfinance's `end` is EXCLUSIVE (verified:
        # end=2024-06-13 returns bars through 06-12). Passing as_of itself
        # silently dropped the as_of day's own bar. The `> as_of` filter
        # below stays as the guard.
        try:
            frame = yf.Ticker(ticker).history(
                start=start,
                end=as_of + timedelta(days=1),
                interval=interval,
                auto_adjust=auto_adjust,
                raise_errors=True,
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
