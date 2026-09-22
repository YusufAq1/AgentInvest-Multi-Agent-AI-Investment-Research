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

## Current status: Phase 3 — RAG over filings

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

### Phase 3 — RAG over filings (`backend/db/`, `backend/rag/`)

This is the first phase that writes to a real Postgres database. Phases
0-2 deliberately deferred it (SQLite cache, in-memory Evidence Store); RAG
retrieval is the first thing that genuinely needs cross-run, queryable,
pgvector-indexed storage. See ADR-0013 for why the primary connection
target is a Supabase project rather than the `docker-compose.yml` Postgres
CLAUDE.md originally specified (Docker wasn't available in this dev
environment) — `docker-compose.yml` is unchanged and is exactly what CI's
test database still uses.

**`backend/db/`** — SQLAlchemy async engine/session (`session.py`) and ORM
models (`models.py`) for CLAUDE.md §14's `documents`/`document_chunks`
tables, migrated via Alembic (`migrations/`, run through an async
`connection.run_sync(...)` pattern rather than a second sync DB driver —
this project's only Postgres driver anywhere is asyncpg). `document_chunks`
carries pgvector's `embedding vector(1024)` (an HNSW index,
`vector_cosine_ops`, `m=16, ef_construction=64`, built on the empty table
in the initial migration per §14) and a *generated* `tsvector` column
(`to_tsvector('english', text)`, always in sync with `text`, no
application-level maintenance) plus a GIN index on it. `char_start`/
`char_end` are added beyond §14's literal metadata list — see ADR-0015 for
why they're necessary for citation-quote verification, the same principle
already applied to `xbrl_fact` evidence.

**`backend/rag/chunking.py`** (pure, no I/O — joins `backend.calc.*`/
`backend.evidence.*` under `mypy --strict`) — section-aware chunking via
`edgartools`' detected sections. `build_full_text()` reconstructs a
filing's storable text by concatenating each section's own extracted
text, and offsets are computed *while building that text*, not trusted
from edgartools' `start_offset`/`end_offset` or searched for inside a
separately-fetched document text — real-data testing against a live AAPL
10-K showed both of those were unreliable (every section reported
`start_offset=0`, and a substring-search fallback failed for every
section too, since `Section.text()` and `Document.text()` come from
different, non-byte-comparable extraction paths inside edgartools). See
ADR-0015's revision history for the full falsification-and-pivot story —
a design that passed its own hand-crafted unit tests but produced zero
usable chunks the first time it met a real filing, caught only because
this project verifies against real data before calling a phase done.
`confidence`/`detection_method` are still used, to skip sections
edgartools itself flags as unreliably detected. Long sections are
sub-chunked by token count (BGE-M3's own tokenizer, via
`backend/rag/embeddings.py`'s `get_tokenizer()` — no second dependency)
with overlap, using the tokenizer's `offset_mapping` to recover exact char
spans with no substring search needed. Re-verified end-to-end against the
same real filing after the fix: 110 chunks, zero offset mismatches.

**`backend/rag/embeddings.py`** — local `BAAI/bge-m3` dense embeddings via
`sentence-transformers`, CPU-only (free, per C1). A lazy, double-checked-
locking singleton — importing this module (which chunking.py does,
transitively, for its tokenizer) never forces the ~2GB model download;
only an actual `embed_texts`/`get_tokenizer` call does, and both are
mockable in tests without ever loading a real model.

**`backend/rag/indexing.py`** — fetch (a new, additive
`EdgarClient.get_filing_document` method returning a filing's full text
plus section metadata) → chunk → embed (one batched call per filing) →
write. Idempotent: a re-run with an unchanged chunk count for an accession
skips embedding+writing entirely; a changed chunk count (e.g. after a
chunking-logic change) deletes and rebuilds that accession's chunks in one
transaction rather than patching a stale partial set.

**`backend/rag/retrieval.py`** — hybrid search: a dense leg (pgvector
`vector_cosine_ops` HNSW ordering) and a full-text leg (`tsvector @@
plainto_tsquery`, ranked by `ts_rank`), each independently filtered to
`ticker` and `filing_date <= as_of` (retrieval's own as_of enforcement —
`tests/rag/test_retrieval.py` includes a leakage test proving a
post-as_of chunk never surfaces regardless of similarity score, this
phase's version of CLAUDE.md §12's most important test). The two ranked
id lists are fused in Python via Reciprocal Rank Fusion
(`fuse_rankings()`, unit-tested against hand-computed orderings with no
database at all) rather than a single blended-score SQL query — see
ADR-0014 for why RRF specifically, and why cosine distance and `ts_rank`
can't be usefully blended directly.

**Testing**: `tests/rag/conftest.py` adds this project's first real-
database test fixtures (`db_engine`, `db_session`, `session_factory`),
using SQLAlchemy's documented "join a session into an external
transaction" pattern (`join_transaction_mode="create_savepoint"`) so every
test starts from a clean slate via rollback, without truncating tables.
CI (`.github/workflows/ci.yml`) now runs a `pgvector/pgvector:pg16` service
container so indexing/retrieval tests exercise real HNSW/`tsvector`
behavior — a mock can't meaningfully fake either. Every other test suite
in this repo stays fully DB-free; scoping these fixtures to `tests/rag/`
keeps that property visible.

**`evaluation/`** — a hand-labelled retrieval eval set
(`evaluation/datasets/retrieval_eval.jsonl`, one question → correct
`(accession, item)` per line) and `evaluation/retrieval_eval.py`, computing
recall@5 and NDCG@5 — this phase's literal exit criterion.

**Measured result** (2026-09-21, against the real AAPL FY2023 10-K
indexed via `scripts/rag_demo.py`, 10 questions after the correction
below): **recall@5 = 0.900, NDCG@5 = 0.826.**

The dataset originally had 15 questions built purely from the SEC's
mandated Item-topic structure (e.g. "Item 1 = Business"), without reading
each section's actual content first. The first real eval run scored
0.600/15 with 6 misses; inspecting the indexed chunks showed 5 of those
misses (Items 10-14: directors, compensation, ownership, related-party,
accountant fees) were unanswerable by construction — Apple's 10-K answers
all five with a single boilerplate line ("incorporated herein by
reference" to the Proxy Statement), a common pattern for large filers who
detail that information in a separate DEF 14A rather than the 10-K itself.
Those 5 rows were removed with an explanation left in the dataset file for
future labelling. The one remaining, genuine miss (Item 3, Legal
Proceedings — which does contain real content, the Epic Games litigation)
is an actual retrieval-quality finding, not a labelling error: Item 1A
(Risk Factors) is both larger (more chunks competing for top-5 slots) and
contains its own general "legal proceedings and government investigations"
risk-factor language, which out-competed the dedicated Item 3 section on
both legs of the hybrid search for that specific query.

Not yet implemented: the remaining specialist agents (Filings, News,
Competitive — Phase 4, which is what actually calls `hybrid_search`),
valuation, the debate layer, the API, or the frontend. This file will grow
with each phase — see `CLAUDE.md` §15 for the phase plan and §11 for the
documentation standard this file follows.
