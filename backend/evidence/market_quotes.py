"""Canonical quote strings for market-data and macro evidence.

WHY this exists: a `price_series` or `macro` evidence row (an as-traded
close, a beta, the 10-year Treasury yield) has no stored source document to
check a quote against. yfinance and FRED responses aren't archived the way
filings are. So, like the News Agent's 8-K rows (ADR-0017), the quote is a
deterministic rendering of the row's own structured fields. The
Valuation Agent builds quotes with these functions, and
backend/evidence/validation.py re-renders and compares.

What that check catches: a quote edited without its value (or the reverse),
a beta whose value isn't covariance / variance, and a risk-free rate whose
decimal doesn't match its published percent.

What it can't catch, and why that's documented rather than hidden: a value
that was wrong when it was fetched. That needs an archived copy of the
vendor response, which is future work.

`repr(float)` is used for every number because it round-trips exactly, so
re-rendering can never differ from the original because of formatting.
"""

from __future__ import annotations

from typing import Any


class UnknownMarketDatumError(ValueError):
    """The location's `kind` has no canonical rendering."""


def render_market_quote(location: dict[str, Any]) -> str:
    kind = location.get("kind")
    if kind == "as_traded_close":
        return (
            f"{location['ticker']} as-traded close on {location['date']}: "
            f"{location['value']!r} USD (yfinance, un-adjusted for later splits; ADR-0021)"
        )
    if kind == "beta":
        return (
            f"{location['ticker']} OLS beta vs {location['market']} over "
            f"{location['observations']!r} monthly total returns to {location['as_of']}: "
            f"{location['value']!r} (covariance {location['covariance']!r}, market variance "
            f"{location['market_variance']!r})"
        )
    if kind == "risk_free_rate":
        return (
            f"FRED {location['series_id']} on {location['date']} (vintage as of "
            f"{location['as_of']}): {location['percent']!r}% = {location['value']!r} as a decimal"
        )
    raise UnknownMarketDatumError(f"No canonical quote for market datum kind {kind!r}")
