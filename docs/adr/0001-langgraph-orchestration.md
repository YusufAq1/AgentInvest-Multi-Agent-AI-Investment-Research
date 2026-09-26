# ADR-0001: LangGraph orchestration, adopted at Phase 5 (earlier than the spec's own trigger)

## Status
Accepted — 2026-09-26

## Context
CLAUDE.md §4 says "hand-rolled async Python first; LangGraph from Phase 5",
and §15 makes Phase 5 the decision point, with an explicit trigger: migrate
if the graph has **more than ~3 conditional branch points, or needs
resumable state**. This ADR number was reserved for that decision.

Measured against that trigger, the Phase 5 graph does **not** qualify:

```
plan -> (fan-out) financial | filings | news | competitive -> collect
```

That's one conditional branch (which agents the plan routes to), no loops,
and no requirement to resume a run. A run takes about a minute. The Phase 4
demo's `asyncio.gather` plus a per-agent `try/except` would meet Phase 5's
exit criterion ("one command runs the full research stage in parallel with
progress logging").

The Phase 4 demo did have three real problems, and any Phase 5 runner had to
fix them, LangGraph or not:
- one agent raising cancelled the whole `gather`
- cost was scraped from log records by a global handler, which would mix
  two concurrent runs' costs together
- nothing planned; every agent always ran with hardcoded Filings questions

## Decision
**Adopt LangGraph (1.x) now, with `AsyncPostgresSaver` checkpointing and
`thread_id = run_id`.** This was the project owner's explicit choice, made
after the recommendation above to stay hand-rolled. It is recorded honestly
as a deliberate early adoption, not as something the graph required. The
reasons:

1. **Learning.** LangGraph is on the owner's list of new concepts, and
   learning it on a small graph is cheaper than learning it mid-Phase 7.
2. **Phase 7 will cross the trigger anyway.** The critic loop is a
   conditional back-edge (critic -> targeted research -> critic, capped at 3
   iterations), followed by the Judge. Migrating a working graph then would
   mean rewriting while the debate layer is also new.
3. **Phase 8's SSE maps directly onto `graph.astream()`.** The progress
   events the CLI prints today are the same stream a browser will
   subscribe to.
4. **Resume comes for free.** A checkpoint after every superstep means an
   interrupted run continues without re-running or re-paying finished
   agents.

### Design consequences of checkpointing (the part worth defending)
- **State is data, so the shared `EvidenceStore` had to go.** A checkpoint
  must be serialisable, and a live store object isn't. Each agent node now
  runs against its own private `EvidenceStore` and returns its evidence,
  claims, drops, outcome and Claude call records as a state update. Those
  lists use `operator.add` reducers, so parallel updates are concatenated,
  never overwritten. This is safe because an agent only ever cites its own
  evidence (asserted by `tests/agents/test_parallel_execution.py`) and
  evidence ids are uuid4.
- **A fan-in `collect` node re-validates the merged result.** It rebuilds
  one store from state and re-runs `add_evidence` (C3: nothing after
  as_of) and `add_claim` (§6 rule 2: every cited id resolves). Unlike the
  agent bulkhead, it lets those raise: failing here means a bug in our code,
  and a run that can't prove its citations must not produce output.
- **Failure bulkhead.** Each agent node catches `Exception` (one place
  only) and records `AgentOutcome(status="failed", detail=...)`. A failed
  agent contributes no claims or evidence (half an agent's output is harder
  to reason about than none), but its drops and Claude cost are kept.
  `KeyboardInterrupt`/`CancelledError` are `BaseException` and still
  propagate, which is exactly what makes a run resumable.
- **Cost as state.** `ClaudeClient` gained an optional `on_result`
  callback. Each node collects its own `LLMCallResult`s and returns them in
  `llm_calls`, so a run's cost is checkpointed with the work it paid for.

## Alternatives considered
- **Stay hand-rolled (`asyncio.TaskGroup` + per-agent try/except).**
  Recommended, and it fully meets §15's rule. Rejected by the owner for the
  reasons above. If it is ever revisited, the orchestration logic is
  contained in `backend/orchestration/graph.py`, and the agents don't know
  LangGraph exists.
- **LangGraph without a checkpointer.** This avoids psycopg and the
  serialisation constraints entirely. Rejected because §15 names
  `PostgresSaver` + `thread_id` as the target, and resumability is the
  concrete capability that justifies the framework at this size.
- **LangGraph with the SQLite checkpointer.** This avoids a second Postgres
  driver. Rejected because run state then lives outside the project's one
  datastore (CLAUDE.md §4: Postgres as the single datastore; see
  ADR-0013), and Phase 8's API would need to read it
  from a local file.

## Consequences
**Easy now:** Phase 7's loop is one conditional edge. Phase 8's SSE forwards
`astream`. Interrupted runs resume. Per-run cost is a sum over state.

**Costs, stated plainly:**
- **Three new direct dependencies:** `langgraph`,
  `langgraph-checkpoint-postgres`, and `psycopg[binary]`, which is a
  **second Postgres driver** next to asyncpg. The checkpointer only
  supports psycopg 3; our own schema still uses asyncpg only. Transitively
  this also brings in `langchain-core` (we import `RunnableConfig` from it,
  because that type appears in LangGraph's own signatures) and `langsmith`.
  LangSmith sends nothing unless `LANGSMITH_TRACING` /
  `LANGCHAIN_TRACING_V2` is set to `true` (verified in the installed
  `langsmith.utils.tracing_is_enabled`).
- **Windows event loop.** psycopg's async mode refuses Windows' default
  `ProactorEventLoop`, so the CLI runs on a `SelectorEventLoop` on win32,
  and `tests/orchestration/conftest.py` does the same for tests.
- **LangGraph owns four tables in our database** (`checkpoints`,
  `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`),
  created idempotently by `saver.setup()` on each run.
  `backend/db/migrations/env.py` excludes them from Alembic autogenerate,
  which would otherwise generate a migration dropping them.
- **Serializer allowlist.** LangGraph's default serializer rebuilds any
  class named in a checkpoint. We restrict it to `CHECKPOINT_TYPES`. Verified
  caveat: an unlisted type is **not** rejected. It comes back as a plain dict
  with a logged warning, so `tests/orchestration/test_graph.py` round-trips
  every type in a real run's final state.
- **Interrupted-node cost is under-counted.** If a node is interrupted
  after making a Claude call but before returning, that call's cost was paid
  but never checkpointed, so it's missing from `llm_calls`. It is still in
  the `llm_call` JSON log line. The planned `llm_calls` DB table (§14),
  written per call, would close this.
- **SEC rate limit under real concurrency.** Our `SecHttpClient` (one
  instance per run, shared by every agent) and edgartools' own ~9 req/s
  limiter throttle independently. edgartools only reads
  `EDGAR_RATE_LIMIT_PER_SEC` from the environment once, at first import of
  `edgar`, and has no runtime setter (verified in `edgar/httpclient.py`,
  v5.58.0). So we can't split the 10 req/s budget from config without
  import-order tricks. The documented gap in `backend/data/http.py` is now
  reachable: Filings plus another SEC-bound agent can briefly exceed
  10 req/s combined. Accepted for now. Revisit if SEC ever returns 403/429
  during a run.

**Revisit:** at Phase 7 (confirm the loop fits cleanly), and at Phase 8
(the SSE endpoint reuses `astream`, and whether `llm_calls` should become
a real table).
