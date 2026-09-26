# ADR-0019: Run ClaudeClient.call_structured off the event loop via asyncio.to_thread

## Status
Accepted — 2026-09-22

## Context
Phase 4's exit criterion has two clauses: every new agent produces
validated claims, and "parallel execution works." Investigating what the
second clause actually requires surfaced a real, pre-existing issue in
Phase 2's code: `ClaudeClient.call_structured` (`backend/core/llm.py`)
wraps the synchronous `anthropic.Anthropic` client, not `AsyncAnthropic`.
The Financial Agent's `_emit_claims` (and, before this ADR, every new
Phase 4 agent's) called it directly from inside `async def run()`.

That's invisible with a single agent: the whole run is one Claude call, so
"blocking" and "not blocking" the event loop look identical from the
outside. It stops being invisible the moment `asyncio.gather` runs
multiple agents concurrently (CLAUDE.md §16: "agents run concurrently via
asyncio.gather") — whichever agent happens to be mid-Claude-call
monopolizes the event loop for that call's full network latency, and
every other "concurrently running" agent's own Claude call silently
queues up behind it instead of overlapping. The agents would still produce
correct claims (Python's cooperative scheduling guarantees correctness
regardless), but the run would take the *sum* of each agent's latency
instead of roughly the *maximum* — quietly defeating the actual point of
"parallel execution works."

## Decision
**Wrap every agent's `call_structured` call in `asyncio.to_thread`**, in
each of `financial.py`, `filings.py`, `news.py`, and `competitive.py`'s
`_emit_claims`:
```python
batch = await asyncio.to_thread(
    self._claude.call_structured,
    agent=self.agent_name,
    ...,
)
```
`_emit_claims` becomes an `async def` (previously a plain synchronous
method called without `await`), and its one call site in each agent's
`run()` becomes `return await self._emit_claims(...)`.

This is the minimal, behavior-preserving fix: `asyncio.to_thread` runs the
blocking call in a worker thread and returns control to the event loop
immediately, so other agents' coroutines can make progress while any one
agent waits on its Claude round trip. It is purely non-functional from
`call_structured`'s own perspective — same call, same arguments, same
return value, same exceptions propagated through the awaited result —
which is exactly why `tests/agents/test_financial.py`'s existing
mock-call-count assertions needed zero changes to keep passing.

## Alternatives considered
- **Rewrite `ClaudeClient` to use `anthropic.AsyncAnthropic` instead.**
  Rejected for this phase: a real, valid long-term improvement, but a
  larger, riskier change touching every existing call site and test in
  `backend/core/llm.py` (Phase 0/2 code, already shipped and tested) for
  the same practical effect `asyncio.to_thread` achieves with a
  three-line change per agent. Worth revisiting if/when `ClaudeClient`
  itself is next touched for an unrelated reason.
- **Leave it synchronous and accept serialized Claude calls under
  `asyncio.gather`.** Rejected: this would make Phase 4's own stated exit
  criterion ("parallel execution works") true only in the weakest possible
  sense (no crash, no data corruption) while the actual practical benefit
  of running agents concurrently — faster wall-clock time — silently
  wouldn't materialize. `scripts/phase4_agents_demo.py`'s timing print
  exists specifically to make this either verifiably true or verifiably
  false for a real run, not just assumed.
- **Only fix it for the newly-added Phase 4 agents, leaving the Financial
  Agent's existing synchronous call as-is.** Rejected: the Financial Agent
  is one of the four agents `scripts/phase4_agents_demo.py` runs
  concurrently — leaving it unfixed would still serialize the whole batch
  behind whichever agent runs first, defeating the fix for the other
  three.

## Consequences
Easy: `tests/agents/test_parallel_execution.py` is the direct, automated
proof this works — four agents sharing one `EvidenceStore`, run via
`asyncio.gather`, with every claim/evidence row landing correctly and
attributable to the right agent. The fix generalizes to any future agent
(Phase 5 onward) for free, since it's a per-agent, one-line pattern rather
than a special case.

Hard: every agent now spends one extra thread-pool round trip per Claude
call (negligible latency in practice, but a real, if small, architectural
detail to remember when reasoning about this codebase's concurrency
model). The underlying `ClaudeClient` is still synchronous internally —
this ADR treats the symptom at every call site, not the root cause in
`backend/core/llm.py` — worth a future ADR if that ever becomes the
bottleneck it currently isn't.
