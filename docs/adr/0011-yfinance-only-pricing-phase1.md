# ADR-0011: yfinance-only pricing in Phase 1 (Stooq fallback deferred)

## Status
Accepted — 2026-09-17

## Context
CLAUDE.md §4 names the price-data stack as "`yfinance`, with Stooq fallback
and mandatory caching," treating yfinance as best-effort and never on a
critical path, with Stooq as the reliability backstop.

While implementing Phase 1's `backend/data/prices.py`, I verified Stooq's
actual current state rather than trusting the spec at face value (per the
working agreement's instruction to re-verify facts before relying on
them). As of 2026-09-16:

- A direct HTTP GET against Stooq's classic CSV endpoint
  (`https://stooq.com/q/d/l/?s=<symbol>.us&i=d`) no longer returns CSV. It
  returns an HTML page with an embedded JavaScript proof-of-work challenge
  (a hashcash-style anti-bot gate), even with a browser-like User-Agent
  header, confirmed by a live request.
- A corroborating `pandas-datareader` GitHub issue (#1012, opened
  2026-04-13, "Stooq now requires API key") confirms Stooq changed its
  access model around Q1–Q2 2026, breaking the classic plain-GET approach
  industry-wide, not just for this project.
- Stooq's own API-key acquisition flow requires completing a CAPTCHA/JS
  challenge in-browser, and even with a key, users report a daily request
  quota ("Exceeded the daily hits limit"). I could not verify the exact
  current quota or confirm the free tier is frictionless and durable
  enough to build against.

C1 requires all data sources to be free; a fallback whose free-tier
viability I can't verify — and which may currently be no more reliable
than the primary source it's meant to back up — isn't a fallback worth
building blind. The working agreement's §0.4 ("no silent fallbacks... do
not quietly substitute a simpler approach") means this gets surfaced and
decided explicitly, not worked around silently.

## Decision
Phase 1 ships **yfinance as the sole price-data source**. `PricesClient`
(`backend/data/prices.py`) has no Stooq integration. On any failure —
exception, timeout, or a genuinely empty result — it returns
`DataUnavailable` (C6), bounded by the shared retry policy
(`data_retry_max_attempts`), never retried forever and never fabricated.

This is an explicit, user-confirmed decision, not a silent substitution:
the gap was raised, the alternatives below were discussed, and yfinance-only
was chosen as the smallest change that keeps Phase 1 moving without
building against an unverified, possibly-broken fallback.

## Alternatives considered
- **Implement Stooq's new gated API anyway.** Rejected: the free-tier
  quota and durability are unverified; building against it risks the
  fallback itself becoming the unreliable path, and burns implementation
  time on a source that may not hold up.
- **Scrape Stooq's HTML/solve the JS challenge programmatically.** Rejected
  outright: fragile, likely violates Stooq's terms of use, and exactly the
  kind of brittle workaround the project's data layer is designed to avoid
  (CLAUDE.md explicitly treats yfinance itself as fragile for similar
  reasons — doubling down on another fragile scraping path doesn't help).
- **Swap in a different free-tier fallback (Alpha Vantage, Twelve Data).**
  Rejected for Phase 1: this would deviate from CLAUDE.md §4's named stack
  in a different direction, introducing a new external service and API key
  not part of the original design — a decision worth making deliberately
  later if yfinance's reliability proves insufficient in practice, not as
  a same-day substitution for Stooq.
- **Block Phase 1 until Stooq's access model is fully resolved.** Rejected:
  §4 itself already treats prices as "never on a critical path" — blocking
  the entire data layer on one non-critical source contradicts that
  framing.

## Consequences
Easy: one fewer client to build, test, and maintain this phase; prices
failures are already handled gracefully via `DataUnavailable`, so no
downstream consumer needs to know whether a fallback exists.

Hard: Phase 1 has no redundancy for price data — if yfinance is blocked or
broken for a given run, `PricesClient` returns `DataUnavailable` with
nothing to fall back to, and any downstream calculation depending on price
history (WACC's beta regression, the reverse DCF in Phase 6) degrades
along with it. Revisit this ADR if: (a) yfinance's reliability proves
insufficient in practice, or (b) Stooq's or another free source's access
model is independently verified as durable and frictionless enough to
build against.
