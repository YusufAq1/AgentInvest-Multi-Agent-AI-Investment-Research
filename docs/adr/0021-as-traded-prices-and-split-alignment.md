# ADR-0021: As-traded prices and split-aligned share counts for market cap

## Status
Accepted — 2026-10-03

## Context
The reverse DCF (ADR-0006) needs a market capitalisation as of `as_of`:
price × shares. Two look-ahead problems hid in that one multiplication,
both found while planning and building Phase 6.

**1. yfinance's historical prices know the future.** `PricesClient`
called `Ticker.history()` with yfinance's default `auto_adjust=True`. That
back-adjusts every historical price for each split and dividend up to
*today*, including those after as_of. Verified against yfinance 1.7.0:
- **NVDA, 2024-05-31:** `auto_adjust=True` gives 109.32. `auto_adjust=False`
  gives 109.63, which is dividend-unadjusted but still **split-adjusted**.
  The price that actually traded that day was **$1,096.33**, ten times
  higher because of the 10:1 split on 2024-06-10.
- Paired with XBRL's as-reported share count of 2.46B (pre-split), the
  adjusted price gives a market cap of about $0.27T instead of the real
  **$2.70T**.

This is C3 leakage: the series encodes a corporate action that hadn't
happened yet. Even with no split, dividend adjustment shifts past levels
by information from after as_of.

**2. The share count can lag the price across a split.** XBRL's share
count comes from a filing's cover page, for example a 10-Q's count as of
2024-05-24. For as_of 2024-06-30, the as-traded price (06-28) is post-split
while the latest reported count is pre-split. That's a 10× error even with
correct prices, and it involves no look-ahead at all: both dates are before
as_of.

**Also found and fixed in the same code:**
- **yfinance's `end` is exclusive.** `end=2024-06-13` returns bars through
  06-12. `end=as_of` silently dropped the as_of day's own bar. It now
  passes `as_of + 1 day`, and the `> as_of` filter stays as the guard.

## Decision
`PricesClient.get_price_history(..., adjustment=...)` (`backend/data/prices.py`):
- **`"total_return"`** (the default, and the existing behaviour):
  `auto_adjust=True`. Used for **returns** (beta). A dividend is part of
  the return, and the adjustment cancels within each period's return.
- **`"as_traded"`**: fetch `auto_adjust=False`, then multiply each bar back
  by the ratios of all splits dated **strictly after** that bar
  (`unadjust_for_splits`, a pure function). A split takes effect at the
  open, so its own day's bar is already post-split. Used for any price
  **level** that's multiplied by an as-reported count.
  - Using post-as_of splits here is **not** look-ahead. They only *reverse*
    the vendor's future-informed adjustment, recovering a number that was
    public on the bar's date. Nothing from after as_of survives into the
    result.
- **`get_splits(ticker, as_of)`** returns only splits on or before as_of,
  so every other use of split data follows C3.

`backend/agents/valuation_inputs.py` multiplies the latest reported share
count by `split_factor_between(splits, count_date, price_date)`, the
splits in `(count_date, price_date]`, both on or before as_of. The share
input's note records the factor applied.

**Live verification (2026-10-02):**

| as_of | Price | Shares | Market cap |
|---|---|---|---|
| 2024-05-31 | 1,096.33 | 2.46B | 2.70T |
| 2024-06-30 | 123.54 | 2.46B × 10 = 24.6B | 3.04T |

Both match NVDA's actual market caps on those dates.

## Alternatives considered
- **Keep the adjusted price and adjust the share count to today's basis.**
  Algebraically the same market cap. Rejected because every printed price
  would then be one that never traded, which is confusing in a report that
  shows its inputs. It also still leaves the dividend adjustment in the
  level.
- **Use a historical market-cap source.** None is free and point-in-time
  (C1).
- **Ignore it; splits are rare.** Rejected. Large caps split often (NVDA,
  AAPL, TSLA, GOOGL, AMZN all did in 2020–2024), and the error is 2–20×,
  not a rounding difference.

## Consequences
- Every valuation price is a price that actually traded, and it's printed
  with its date.
- Beta keeps the total-return series, so its behaviour is unchanged.
- Tests use NVDA-shaped fixtures: the un-adjusted close, the
  strictly-after rule, `get_splits` filtering to as_of, and share
  restatement across a split.
- **Dependence on yfinance's split list:** a missing split would reintroduce
  the error. There's no free second source to cross-check against (ADR-0011).
- **`raise_errors` is deprecated** in yfinance 1.7.0. It still works, and
  `prices.py` relies on it to tell "failed" apart from "empty". Revisit
  before upgrading yfinance.
