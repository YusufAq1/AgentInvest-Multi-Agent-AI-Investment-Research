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

## Current status: Phase 5 (Increment 5a) — LangGraph orchestration

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

### Phase 4 — Filings, News, Competitive agents (`backend/agents/`)

**Shared plumbing extracted first** (ADR-0016): Financial Agent's private
XBRL alias/period-matching helpers moved to `backend/agents/xbrl_facts.py`
(`CONCEPT_ALIASES`, `select_anchor`, `select_matching`, `select_prior_year`
— renamed from `_`-prefixed originals) once the Competitive Agent needed
the identical logic; `_validate_claim_batch`'s `EvidenceNotFoundError`→
`ValueError` bridge moved onto `EvidenceStore` itself
(`EvidenceStore.validate_claim_batch`) once four agents needed it
verbatim. Both extractions are confirmed behavior-preserving —
`tests/agents/test_financial.py` passes unmodified.

**`backend/agents/filings.py`** — the first real consumer of Phase 3's RAG
pipeline. Orchestrates fetch → `index_filing` → `hybrid_search` itself
(no Manager exists yet to hand it pre-indexed data), against a hardcoded
set of 5 default research questions (a documented first-cut default,
module constant, same category as `xbrl_facts.py`'s `CONCEPT_ALIASES`).
Deduplicates hits by `chunk_id` across questions before building Evidence
— the same "don't blow up evidence count" discipline as the Financial
Agent's lazy construction, applied to a different failure shape (one
chunk legitimately answering more than one question). `backend/rag/
retrieval.py` gained two new functions for this: `resolve_chunk_citation`
(fetches the `char_start`/`char_end`/`document_id` `RetrievedChunk`
deliberately doesn't carry) and `get_full_text_by_accession` (feeds the
`documents={accession: full_text}` mapping citation-validity checks need)
— both DB-touching, so they live in `retrieval.py`, not the DB-free
`backend/evidence/validation.py`, which needed zero changes: its
`("sec_filing", "news")` containment branch already accepted a caller
-supplied `documents` mapping.

**`backend/agents/news.py`** — classifies 8-K materiality from structured
item-code metadata only (`NewsClient` never fetches narrative text — a
deliberate Phase 1 scoping decision, not a gap this phase papers over).
Citation quote is a deterministic, Python-constructed canonical string
(`"8-K filed {date} (accession {accession}): {code} ({label}); ..."`),
the same category of construction as `"computed"` evidence's formula
string — never freeform prose Claude could have written. This makes the
citation-validity containment check tautological for `news` evidence
(quote and "document" are the same string) — an accepted, explicitly
documented tradeoff, not a hidden one. See ADR-0017 for the full
reasoning and the honest fidelity gap this leaves (no ability to cite
specific narrative detail from inside an 8-K).

**`backend/agents/competitive.py`** — compares a ticker's XBRL revenue and
net margin against a hand-curated peer map (`backend/agents/peer_map.py`,
a starter set of 5 large caps — SIC-code lookup was rejected as
misclassifying modern large-caps; see ADR-0018). Market-share enforcement
needed zero new validation code: no data source in this project can
produce market-share/TAM figures, so Python never creates that Evidence,
so Claude can never cite one, so `validate_claim_batch` structurally
rejects any `claim_type="evidence"` attempt. The one gap this doesn't
close — an `"inference"` claim citing real revenue evidence while
asserting an unrelated market-share number — is closed by an explicit
prompt instruction instead, since `inference`/`assumption` claims aren't
checked for whether their evidence actually supports the specific
assertion (CLAUDE.md §6 rule 5).

**The concurrency fix** (ADR-0019): investigating what "parallel execution
works" actually requires surfaced that `ClaudeClient.call_structured`
wraps the synchronous `anthropic.Anthropic` client, and the Financial
Agent (Phase 2) called it directly inside `async def run()` — invisible
with one agent, but under `asyncio.gather` it would serialize every
concurrently-"running" agent's Claude call behind whichever one runs
first. Fixed by wrapping the call in `asyncio.to_thread` in all four
agents' `_emit_claims` (now `async def`) — purely non-functional, and
`test_financial.py`'s existing assertions needed no changes.
`tests/agents/test_parallel_execution.py` is the direct, automated proof:
all four agents share one `EvidenceStore`, run via `asyncio.gather`, and
every claim/evidence row is confirmed to land correctly with nothing
lost, duplicated, or cross-attributed to the wrong agent.
`scripts/phase4_agents_demo.py` prints real wall-clock timing for a live
run against real APIs — the visible confirmation the fix actually
shortens wall time, not just "doesn't crash."

### Phase 5 — Orchestration (`backend/orchestration/`)

**Increment 5a: LangGraph runner with a deterministic plan.** ADR-0001
records the decision to adopt LangGraph now, earlier than §15's own trigger,
and says so honestly. `docs/LANGGRAPH_CONCEPTS.md` explains the concepts.

```
START -> plan -> (fan-out) financial | filings | news | competitive -> collect -> END
```

- **`state.py`**: `ResearchState`, a TypedDict with `operator.add`
  reducers on `evidence`, `claims`, `dropped_claims`, `agent_outcomes` and
  `llm_calls`, so parallel agent updates concatenate. It also defines
  `ResearchPlan` (`routes`, `skipped`, `filings_questions`, `source`) and
  `AgentOutcome` (`succeeded`/`failed`/`skipped`, counts, duration,
  detail). `CHECKPOINT_TYPES` is the serializer allowlist.
- **`planning.py`**: `deterministic_plan()`. It routes every agent, skips
  Competitive when the ticker isn't in `PEER_MAP`, and gives Filings the
  default questions. This is 5a's only planner and becomes 5b's recorded
  fallback.
- **`graph.py`**: `build_research_graph(deps, checkpointer)`.
  - Each agent node builds a private `EvidenceStore` and a `ClaudeClient`
    whose `on_result` collects that node's calls. It runs the unchanged
    agent and returns its output as a state update.
  - The node is the run's only failure **bulkhead**: `except Exception`
    becomes `AgentOutcome(status="failed")`, other agents continue, and the
    cost already spent is kept.
  - **`collect`** rebuilds one merged store and re-runs `add_evidence`
    (C3) and `add_claim` (§6 rule 2) over everything, raising on any
    violation.
  - Agents are built through `RunDependencies.agent_factories`. That's
    also the test seam.
- **`runner.py`**:
  - `open_run_dependencies()` creates one shared `SecHttpClient` per run,
    since the rate limiter is per instance.
  - `open_checkpointer()` opens `AsyncPostgresSaver` on psycopg 3, runs
    `setup()` idempotently, and applies the serializer allowlist.
  - `start_run()` / `resume_run()` stream `plan_ready`, `agent_started`,
    `agent_finished` and `run_collected` events. `thread_id = run_id`.
- **`scripts/run_research.py`**: the one command. It supports
  `--ticker/--as-of` or `--resume <run_id>`, prints progress with elapsed
  time, a per-agent outcome table, the claims, the total cost and the §18
  disclaimer (`backend/core/disclaimer.py`). It runs on a
  `SelectorEventLoop` on Windows, because psycopg's async mode rejects
  Proactor.
- **Supporting changes:**
  - `ClaudeClient(on_result=...)` and `total_cost()` in `core/llm.py`,
    replacing the demos' log-scraping cost collector
  - a `research_questions` constructor argument on `FilingsAgent`
  - Alembic `include_object` excludes LangGraph's `checkpoint*` tables

**Tests:**
- `tests/orchestration/test_graph.py` is DB-free, using `InMemorySaver`
  with the production serializer. It covers attribution, cost summing, the
  failure bulkhead, the peer-map skip, event ordering, as_of leakage,
  `collect` rejecting a fabricated citation and leaked evidence, resume
  without rerunning finished agents, and the serializer round-trip and
  allowlist.
- `tests/orchestration/test_checkpointer_postgres.py` round-trips a run's
  state through real Postgres across two connections.

**Known gaps, documented in ADR-0001:**
- A Claude call inside an *interrupted* node is paid but not checkpointed.
  It's still in the JSON log, but the `llm_calls` table (§14) is still not
  built.
- The SEC combined-rate gap (`backend/data/http.py`) is now reachable
  under real concurrency.

Not yet implemented: the LLM Research Manager (Increment 5b), valuation,
the debate layer, the API, or the frontend. This file will grow with each phase — see `CLAUDE.md` §15
for the phase plan and §11 for the documentation standard this file
follows.
