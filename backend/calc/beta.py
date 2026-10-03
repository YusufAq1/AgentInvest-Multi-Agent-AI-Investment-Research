"""Equity beta from price history: monthly returns and an OLS regression.

New-concept note: beta measures how much a stock's return moves with the
market's. It's the slope of a regression of the stock's returns on the
market's returns:

    β = cov(r_stock, r_market) / var(r_market)

β = 1.3 means that, historically, when the market returned +1% in a month
the stock returned about +1.3% on average. CAPM (wacc.py) turns that into a
cost of equity.

WHY monthly returns (the caller chooses the window; 5 years is the
default in config): daily returns understate beta for less-liquid stocks
(prices react to market moves with a lag) and are noisy, while monthly
returns over 5 years is the most common convention (≈60 observations).
WHY simple returns, not log returns: CAPM is stated in simple returns, and
at monthly frequency the difference is small.

Pure functions, no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from backend.calc.errors import ValuationInputError
from backend.calc.ratios import RatioResult

Month = tuple[int, int]  # (year, month)


def monthly_returns(closes: Sequence[tuple[date, float]]) -> dict[Month, float]:
    """Simple month-over-month returns from dated closing prices.

    Takes the LAST close of each calendar month, then returns
    `close_m / close_{m-1} - 1`, keyed by the later month. A return is only
    produced between two *consecutive* calendar months, so a gap in the
    data (a missing month) never silently becomes a two-month return.

    Callers should pass total-return (dividend-adjusted) closes: beta is
    about returns, and a dividend is part of the return.
    """
    month_end: dict[Month, tuple[date, float]] = {}
    for day, close in closes:
        if close <= 0:
            raise ValuationInputError(f"monthly_returns: non-positive close {close} on {day}")
        key = (day.year, day.month)
        if key not in month_end or day > month_end[key][0]:
            month_end[key] = (day, close)

    months = sorted(month_end)
    returns: dict[Month, float] = {}
    for previous, current in zip(months, months[1:], strict=False):
        if _next_month(previous) != current:
            continue
        returns[current] = month_end[current][1] / month_end[previous][1] - 1
    return returns


def _next_month(month: Month) -> Month:
    year, m = month
    return (year + 1, 1) if m == 12 else (year, m + 1)


def ols_beta(
    *,
    stock_returns: Mapping[Month, float],
    market_returns: Mapping[Month, float],
    min_observations: int,
) -> RatioResult:
    """Slope of stock returns on market returns, over the months both have.

    Uses sample covariance and variance (n - 1). The n - 1 cancels in the
    ratio, but it keeps the reported covariance/variance conventional.

    Raises ValuationInputError if fewer than `min_observations` months
    overlap (a beta from a handful of points is noise, not a measurement),
    or if the market's return variance is zero.

    WHY `inputs` holds summary statistics, not the return series:
    RatioResult inputs are scalars. The full series is reproducible from
    the cited price evidence; the summary is what the formula consumes.
    """
    common = sorted(set(stock_returns) & set(market_returns))
    n = len(common)
    if n < min_observations:
        raise ValuationInputError(
            f"ols_beta: {n} overlapping monthly returns, need at least {min_observations}"
        )
    xs = [market_returns[m] for m in common]
    ys = [stock_returns[m] for m in common]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / (n - 1)
    market_variance = sum((x - mean_x) ** 2 for x in xs) / (n - 1)
    if market_variance == 0:
        raise ValuationInputError("ols_beta: market returns have zero variance")
    return RatioResult(
        name="beta",
        formula="covariance / market_variance",
        inputs={
            "covariance": covariance,
            "market_variance": market_variance,
            "observations": float(n),
        },
        value=covariance / market_variance,
    )
