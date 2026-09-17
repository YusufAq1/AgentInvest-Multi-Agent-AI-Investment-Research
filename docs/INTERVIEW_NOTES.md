# Interview Notes

A running Q&A file, appended to as each component lands, so the design of
this system stays defensible cold. See `CLAUDE.md` §11.3.

## Why does every agent default to Haiku instead of Sonnet or Opus?

Because the workload — extraction, classification, routing, per-chunk
summarization — is squarely within what a small, cheap model handles well,
and this is a system meant to be run repeatedly, not demoed once. Defaulting
to a bigger model "to be safe" makes cost unpredictable for no measured
benefit. The rule: start every agent on Haiku, escalate only when an eval
shows it measurably fails a specific task, and record that evidence in an
ADR when it happens. Only the Critic, the Investment Judge, and final
narrative synthesis are expected to ever need Sonnet — because those tasks
involve finding genuine contradictions or synthesizing the highest-stakes
output in the pipeline, where a miss undermines the whole debate-layer
premise. See ADR-0008.

## Why uv instead of pip or Poetry?

One tool covers dependency resolution, virtualenv management, and running
scripts inside that environment, backed by a real lockfile (`uv.lock`) so
CI installs exactly what local development uses. pip needs `pip-tools`
bolted on to get a comparable lockfile; Poetry works but resolves
dependencies more slowly on non-trivial trees, which matters once
PyTorch-adjacent dependencies (BGE-M3, Phase 3) show up. See ADR-0010.

## How do you know a Claude call's cost is actually right?

`compute_cost()` in `backend/core/llm.py` is a pure function: token counts
in, a dollar amount out, using the per-model pricing table in
`Settings.model_pricing`. It's unit-tested against hand-worked values
(`tests/test_llm.py`) including the cache-read and cache-write terms, so
the arithmetic is verified independently of ever calling the real API. The
model never reports its own cost — Python computes it from the token counts
the API returns, the same principle (C4: numbers never come from an LLM)
that will later apply to every financial calculation in `backend/calc/`.

## How do you guarantee no look-ahead bias in data retrieval?

Every public method in `backend/data/` takes `as_of` and enforces it in
code — never by trusting an upstream API's own date filtering alone. Two
correctness details matter more than they look:

1. **XBRL facts are filtered on `filed`, never `end`/`fy`/`fp`.** Those
   fields describe the fiscal period a number reports on, not when it
   became public. A FY2023 10-K might be `filed` in early 2024 — filtering
   on the fiscal period instead of `filed` would leak a number that didn't
   exist yet at `as_of` into a point-in-time query. This is the exact bug
   class the project exists to prevent (C3).
2. **FRED needs both `observation_end` and `realtime_end`.** FRED tracks
   revision vintages — a GDP print gets revised months after first release.
   `observation_end` alone would let a later-revised value for an in-range
   period leak through; `realtime_end` is what actually excludes revisions
   published after `as_of`.

Every source module re-checks its own dates defensively in Python even when
the upstream API (or `edgartools`) claims to filter by date already — never
trust a third party's filtering without a local, testable check. The test
suite proves this directly: each source has a test that constructs a
fixture entry whose *period* looks in-range but whose *filed/publish* date
is after `as_of`, and asserts it's excluded — the data-layer's version of
§12's evidence leakage test, one phase earlier.

## Why does missing data return a value instead of raising?

C6 says a missing-data marker must "propagate into the report" — that's a
return-value contract, not an exception. If `get_company_facts()` raised on
every empty result, every future agent would need a `try/except` around
each of ~15 data calls across five sources, and forgetting one would crash
an entire research run instead of degrading one section of it.

But not every failure is treated this way. `backend/data` uses a hybrid:
*expected* misses (rate limit exhausted after retries, an unknown ticker,
no data in the requested window) are caught and returned as
`DataUnavailable`. A genuinely *unexpected* failure — a misconfigured EDGAR
identity, a response shape that no longer parses — is deliberately left to
propagate as a real exception. Converting everything to `DataUnavailable`
indiscriminately would let real bugs masquerade as ordinary missing-data
cases, which is worse than a loud crash during development.

## Why does yfinance have no fallback yet, when the spec calls for Stooq?

Because I checked, rather than trusting the spec: Stooq's plain CSV
endpoint is currently gated behind a JS anti-bot challenge and appears to
now require an API key with an unverified daily quota (confirmed live,
2026-09-16, corroborated by a GitHub issue from ~April 2026). Building a
fallback against a source I can't verify is durable would just move the
reliability problem, not solve it. Phase 1 ships yfinance-only, with
`DataUnavailable` on failure — an explicit, documented gap (ADR-0011), not
a silently downgraded feature.
