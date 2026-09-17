# Architecture

AgentInvest takes a ticker and an as-of date and produces an auditable,
evidence-linked equity research report. The intended end-state pipeline
(see `Overview.md` for the full narrative walkthrough):

```
(ticker, as_of) -> Research Manager -> specialist agents (parallel)
                 -> Evidence Store (citation-enforced claims)
                 -> Bull / Bear agents (read-only over Evidence Store)
                 -> Critic (flags unsupported claims, can trigger more research)
                 -> Investment Judge -> Research Report (thesis, evidence,
                    falsification conditions, computed confidence)
```

The two structural rules that make this different from a single prompted
LLM call:

1. **Every material claim needs a citation that resolves to stored source
   text**, checked in Python (string containment against the original
   document), not just requested in a prompt.
2. **Numbers never come from an LLM.** All arithmetic — ratios, growth
   rates, DCF, confidence scores — is a pure, unit-tested Python function.
   The LLM interprets results; it never computes them.

## Current status: Phase 1 — Data layer

### Phase 0 — Foundation
- `backend/core/config.py` — process settings (Anthropic key, default
  model, per-model pricing table, Postgres connection).
- `backend/core/logging.py` — structured (JSON-lines) logging setup.
- `backend/core/llm.py` — `ClaudeClient`, a thin wrapper around the
  Anthropic SDK that computes and logs the cost of every call.
- `scripts/hello_world.py` — proves the above works end-to-end with one
  real Claude call.
- Docker Compose brings up Postgres + pgvector, but nothing reads or
  writes to it yet.

### Phase 1 — Data layer (`backend/data/`)

All external I/O now lives in `backend/data/`, one class per source, each
constructed once from `Settings` — mirroring `ClaudeClient`'s pattern:

- `edgar.py` (`EdgarClient`) — filings via `edgartools`.
- `xbrl.py` (`XBRLClient`) — structured fundamentals via SEC XBRL
  `companyfacts` (numbers, never text extraction).
- `prices.py` (`PricesClient`) — daily price history via `yfinance` only;
  see ADR-0011 for why Stooq's spec'd fallback isn't implemented yet.
- `macro.py` (`MacroClient`) — risk-free rate/CPI/real GDP via FRED.
- `news.py` (`NewsClient`) — 8-K material events (RSS/GDELT deferred to
  the Phase 4 News Agent, which is the first thing that needs them).

Shared plumbing, added beyond CLAUDE.md §13's literal file list because
§16 requires retry/backoff and rate limiting to be written once and reused,
not copy-pasted across five modules:
- `models.py` — shared value types, including `DataUnavailable`.
- `errors.py` — the exception hierarchy (see "Hybrid DataUnavailable
  design" below).
- `retry.py` — one async exponential-backoff-with-jitter decorator, used
  by every client.
- `http.py` — one shared `httpx.AsyncClient` + rate limiter for direct
  `data.sec.gov`/`www.sec.gov` calls (not used by `edgar.py`/`prices.py`,
  which wrap synchronous third-party libraries via `asyncio.to_thread`
  instead, or by `macro.py`, which talks to a different host with its own
  rate-limit characteristics).
- `cik.py` — ticker→CIK resolution, shared by `xbrl.py` and `news.py`.
- `cache.py` — local SQLite, keyed `(source, args_hash, as_of)`, deliberately
  not the Postgres `cache` table from §14 (that migrates once `backend/db/`
  is built for real, likely Phase 2).

**Hybrid `DataUnavailable` design.** Every public method catches its own
*expected* failure modes (a rate limit exhausted after retries, an unknown
ticker, no data in the requested window) and returns `T | DataUnavailable`
— satisfying C6's "propagates into the report." A genuinely *unexpected*
failure (a misconfigured EDGAR identity, a response shape that no longer
parses) is deliberately left to propagate as a real exception, so a bug can
never masquerade as an ordinary missing-data case. See `errors.py`'s module
docstring for the full reasoning.

**Cache semantics for snapshot-shaped sources.** `companyfacts`,
`submissions`, and `company_tickers.json` have no upstream `as_of`
parameter at all — SEC always returns the full current history. These are
cached as a raw blob keyed by *fetch day* (approximating the configured
`cache_snapshot_ttl_hours`, default 24h), and the real point-in-time filter
(`filed <= as_of` / `filingDate <= as_of`) is applied in pure Python **on
every read**, cache hit or miss alike — never assumed correct just because
something is already cached.

**Accepted, documented gap: dual rate limiters.** `edgar.py` (via
`edgartools`) throttles its own SEC traffic internally (~9 req/s default),
entirely independently of `http.py`'s `SecRateLimiter` (10 req/s) used by
`xbrl.py`/`news.py`/`cik.py`. Under concurrent use, combined real traffic to
SEC could momentarily exceed the 10 req/s ceiling. A cross-process shared
limiter isn't justified at Phase 1's scale (one research run, one ticker at
a time) — documented here as a conscious simplification, not an oversight.

Not yet implemented: the Evidence Store, any research agent, retrieval,
valuation, the debate layer, the API, or the frontend. This file will grow
with each phase — see `CLAUDE.md` §15 for the phase plan and §11 for the
documentation standard this file follows.
