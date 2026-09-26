# LangGraph concepts, as used in AgentInvest

A short explanation of the LangGraph ideas `backend/orchestration/` relies
on, written for someone who knows Python and asyncio but hasn't used
LangGraph. See ADR-0001 for *why* we use it.

## The graph
A LangGraph graph is a set of **nodes** (plain Python functions, sync or
async) joined by **edges**. Ours:

```
START -> plan --(conditional)--> financial ─┐
                           ├──> filings   ─┤
                           ├──> news      ─┼──> collect -> END
                           └──> competitive┘
```

You describe it with `StateGraph(ResearchState)`, `add_node`, `add_edge`
and `add_conditional_edges`, then `compile()` it into something runnable.

## State, updates and reducers
There is one **state** object per run (`ResearchState`, a TypedDict in
`backend/orchestration/state.py`). A node receives the current state and
returns a **partial update**, a dict containing only the keys it changes.
It never mutates the state it was given.

How an update is merged is decided per key:
- **No reducer** (e.g. `plan`): the new value replaces the old one.
- **With a reducer** (e.g. `claims: Annotated[list[Claim], operator.add]`):
  LangGraph calls `operator.add(old, new)`, i.e. list concatenation.

Reducers are what make parallel nodes safe. Four agent nodes each return
`{"claims": [...their claims...]}` in the same step, and LangGraph
concatenates all four. Without the reducer, whichever finished last would
overwrite the rest.

## Supersteps: how parallelism happens
LangGraph executes in **supersteps**. Every node triggered in the same
superstep runs concurrently, and the next superstep starts only when all of
them have finished.

- `plan`'s conditional edge (`_route_after_plan`) returns a **list** of node
  names, for example `["financial", "filings", "news", "competitive"]`. All
  of them are triggered together, so they run in one superstep, in
  parallel. This is the **fan-out**.
- Each agent node has a plain edge to `collect`. `collect` is therefore
  triggered in the *next* superstep, once, after every agent is done. This
  is the **fan-in**.

Under the hood it's asyncio, so the ADR-0019 rule still applies: a node
that blocks the event loop blocks its siblings. That's why agents call
Claude via `asyncio.to_thread`.

## Checkpoints, threads and resume
A **checkpointer** saves the state after every superstep. Ours is
`AsyncPostgresSaver`, which writes to LangGraph's own `checkpoint*` tables.
Tests use `InMemorySaver`.

Checkpoints are grouped by a **thread_id**, passed in the run config as
`{"configurable": {"thread_id": ...}}`. We use the run's `run_id`, so one
research run is one thread.

To **resume**, you call the graph again with the same thread_id and
`None` as the input. LangGraph loads the last checkpoint and continues. It
also saves each finished node's update *within* a superstep, so if three of
four agents finished before a crash, only the fourth reruns.
`tests/orchestration/test_graph.py::test_interrupted_run_resumes_without_rerunning_finished_agents`
demonstrates exactly this.

Because checkpoints are serialised, **state must be data**: models and
lists, not live objects like clients or an `EvidenceStore`. Live things are
captured in node closures instead (`RunDependencies` in `graph.py`) and
rebuilt fresh on every process start.

## Streaming
`graph.astream(..., stream_mode=["custom", "updates"])` yields events while
the graph runs:
- `"custom"`: anything a node writes via `get_stream_writer()`. We emit
  `plan_ready`, `agent_started` and `agent_finished`.
- `"updates"`: each node's raw state update as it completes.

The CLI prints these as progress. Phase 8's SSE endpoint will forward the
same stream to the browser.
