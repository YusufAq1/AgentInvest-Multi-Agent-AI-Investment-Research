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

## Current status: Phase 2 — Evidence Store + Financial Agent

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

### Phase 2 — Evidence Store + Financial Agent (`backend/evidence/`, `backend/calc/`, `backend/agents/`)

`backend/evidence/` is the anti-hallucination mechanism CLAUDE.md §6
describes, implemented in code:

- `models.py` — `Evidence`, `Claim` (with a Pydantic validator enforcing
  "claim_type='evidence' requires ≥1 evidence_id"), `ClaimBatch` (the
  Financial Agent's structured-output shape), `DroppedClaim`.
- `store.py` — `EvidenceStore`, in-memory and per-run. Enforces "every
  evidence_id must resolve to a row that exists" (`resolve`/`add_claim`)
  and a look-ahead guard (`add_evidence` rejects `published_at > as_of`).
  Deliberately not Postgres-backed yet — see ADR-0012.
- `validation.py` — citation-validity checks, two paths by `source_type`:
  substring containment for prose/XBRL-JSON, recompute-and-compare against
  resolved input evidence for `computed` values (a derived ratio has no
  upstream document to contain it in). This is the direct, tested mechanism
  behind this phase's exit criterion ("CI fails if a fabricated citation is
  introduced deliberately") — see ADR-0012 for the full design and the
  documented batch-vs-per-claim retry tradeoff.

`backend/calc/ratios.py` — four pure ratio functions (`gross_margin`,
`net_margin`, `current_ratio`, `yoy_revenue_growth`), each returning a
`RatioResult` carrying its formula and inputs so both the Financial Agent
(building `computed` Evidence) and `validation.py` (recomputing to verify)
share one `RATIO_FUNCS` dispatch table. This package and `backend/evidence/`
are the first to run under `mypy --strict` (CLAUDE.md §16) — the override
that was a commented-out placeholder since Phase 0 is now active.

**The Financial Agent calls Claude only to phrase and classify, never to
compute.** Python creates every `Evidence` row deterministically from XBRL
data — both raw facts (`xbrl_fact`) and computed ratios (`computed`) —
*before* Claude is ever called. A forced-tool-use Haiku call
(`ClaudeClient.call_structured`, extending the Phase 0 `ClaudeClient`)
receives that evidence set and can only cite `evidence_id`s from it; a
hallucinated or out-of-set id fails `EvidenceStore.resolve`, triggering the
same one-retry-then-drop mechanism as a schema-level Pydantic failure. This
is what makes the claims impossible to fabricate rather than merely
unlikely to.

`ClaudeClient.call_structured` is generic over any Pydantic model (not
hardcoded to `Claim`) — every later agent (Filings, News, Bull, Bear,
Critic) reuses it unchanged for its own structured output.

Not yet implemented: retrieval/RAG, the remaining specialist agents,
valuation, the debate layer, the API, or the frontend. This file will grow
with each phase — see `CLAUDE.md` §15 for the phase plan and §11 for the
documentation standard this file follows.
