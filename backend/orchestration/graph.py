"""The research graph: plan -> specialist agents in parallel -> collect.

```
START -> plan --(conditional fan-out)--> financial ─┐
                                    ├──> filings   ─┤
                                    ├──> news      ─┼──> collect -> END
                                    └──> competitive┘
```

New-concept note (see docs/LANGGRAPH_CONCEPTS.md for the longer version):
LangGraph runs a graph in *supersteps*. Every node triggered in the same
superstep runs concurrently, and the next superstep starts only when they
have all finished. `plan`'s conditional edge returns a LIST of node names;
every node in that list runs in one superstep, so the routed agents run in
parallel. Each agent node has a plain edge to `collect`, so `collect` is
triggered once, in the superstep after them — this is the fan-in. A
LangGraph *checkpoint* is saved after every superstep, which is what makes
a run resumable (backend/orchestration/runner.py).

WHY agent dependencies are closures, not state: clients (HTTP, DB session
factory, Anthropic SDK) are live objects that can't be checkpointed. The
graph is rebuilt with fresh clients on every process start — including
when resuming — and only the serialisable research state is persisted.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from backend.agents.base import ResearchAgent
from backend.core.config import Settings
from backend.core.llm import ClaudeClient, LLMCallResult
from backend.evidence.store import EvidenceStore
from backend.orchestration.planning import Planner
from backend.orchestration.state import (
    ALL_AGENTS,
    AgentName,
    AgentOutcome,
    ResearchPlan,
    ResearchState,
)

logger = logging.getLogger("agentinvest.orchestration")

COLLECT_NODE = "collect"
PLAN_NODE = "plan"


@dataclass(frozen=True)
class AgentContext:
    """Everything an agent factory needs to build one agent for one run.

    `store` is private to this agent's node (see state.py for why), and
    `claude` reports every call it makes back to the node for cost
    tracking.
    """

    store: EvidenceStore
    claude: ClaudeClient
    plan: ResearchPlan
    settings: Settings


AgentFactory = Callable[[AgentContext], ResearchAgent]
ClaudeFactory = Callable[[Callable[[LLMCallResult], None]], ClaudeClient]
# Builds the run's planner around a cost-tracked ClaudeClient. The real one
# builds the Research Manager; tests use DeterministicPlanner, which ignores
# the client.
PlannerFactory = Callable[[ClaudeClient], Planner]


@dataclass(frozen=True)
class RunDependencies:
    """The live, non-serialisable things the graph's nodes close over.

    WHY factories rather than pre-built agents: an agent is bound to one
    EvidenceStore and one ClaudeClient, both of which are per-node, per-run.
    Factories also let tests swap in fake agents without touching the graph
    (tests/orchestration/test_graph.py).
    """

    settings: Settings
    agent_factories: Mapping[AgentName, AgentFactory]
    claude_factory: ClaudeFactory
    planner_factory: PlannerFactory


def _plan_node(deps: RunDependencies) -> Callable[..., Any]:
    async def plan(state: ResearchState) -> dict[str, Any]:
        # WHY the planner's cost is tracked like an agent's: the Research
        # Manager is a paid Claude call, and "what did this run cost?" must
        # include it (CLAUDE.md §5).
        calls: list[LLMCallResult] = []
        planner = deps.planner_factory(deps.claude_factory(calls.append))
        research_plan = await planner.plan(state["ticker"], state["as_of"])
        skipped = [
            AgentOutcome(agent=s.agent, status="skipped", detail=s.reason)
            for s in research_plan.skipped
        ]
        get_stream_writer()(
            {
                "event": "plan_ready",
                "source": research_plan.source,
                "routed": research_plan.routed_agents(),
                "skipped": [s.agent for s in research_plan.skipped],
                "fallback_reason": research_plan.fallback_reason,
            }
        )
        return {"plan": research_plan, "agent_outcomes": skipped, "llm_calls": calls}

    return plan


def _route_after_plan(state: ResearchState) -> list[str]:
    """The conditional fan-out: every routed agent runs in the next
    superstep. An empty plan goes straight to `collect`, so a run always
    finishes with a verified (possibly empty) result rather than hanging."""
    routed: list[str] = list(state["plan"].routed_agents())
    return routed or [COLLECT_NODE]


def _agent_node(name: AgentName, deps: RunDependencies) -> Callable[..., Any]:
    async def run_agent(state: ResearchState) -> dict[str, Any]:
        writer = get_stream_writer()
        writer({"event": "agent_started", "agent": name})

        store = EvidenceStore(run_id=state["run_id"], as_of=state["as_of"])
        # WHY a per-node list: this node's cost is returned as part of its
        # own state update, so it's checkpointed with the node's result and
        # a resumed run never re-counts (or re-pays) a finished agent.
        calls: list[LLMCallResult] = []
        claude = deps.claude_factory(calls.append)
        context = AgentContext(
            store=store, claude=claude, plan=state["plan"], settings=deps.settings
        )

        start = time.monotonic()
        try:
            agent = deps.agent_factories[name](context)
            claims = await agent.run(state["ticker"], state["as_of"])
        # WHY catch Exception here, and only here: this is the run's bulkhead.
        # An agent failing — a Claude refusal, a truncated tool call, SEC or
        # Postgres being down — must become a recorded, visible gap in the
        # run (CLAUDE.md C6), not a crash that throws away the other agents'
        # finished work. Nothing is swallowed: the failure is logged with its
        # traceback, surfaced in AgentOutcome, and the cost already spent is
        # still reported. CancelledError/KeyboardInterrupt are BaseException,
        # not Exception, so Ctrl-C and task cancellation still propagate
        # (which is what lets a checkpointed run be resumed).
        except Exception as exc:
            duration_ms = (time.monotonic() - start) * 1000
            logger.exception(
                "agent_failed",
                extra={"agent": name, "run_id": str(state["run_id"]), "ticker": state["ticker"]},
            )
            outcome = AgentOutcome(
                agent=name,
                status="failed",
                dropped_count=len(store.dropped_claims()),
                duration_ms=duration_ms,
                detail=f"{type(exc).__name__}: {exc}",
            )
            writer({"event": "agent_finished", **outcome.model_dump()})
            # WHY no evidence/claims from a failed agent: it may have stopped
            # halfway (evidence written, claims not), and half an agent's
            # output is harder to reason about than none. Drops and cost are
            # kept — they're facts about what happened.
            return {
                "agent_outcomes": [outcome],
                "dropped_claims": store.dropped_claims(),
                "llm_calls": calls,
            }

        outcome = AgentOutcome(
            agent=name,
            status="succeeded",
            claims_count=len(claims),
            dropped_count=len(store.dropped_claims()),
            duration_ms=(time.monotonic() - start) * 1000,
        )
        writer({"event": "agent_finished", **outcome.model_dump()})
        return {
            "evidence": store.all_evidence(),
            "claims": store.claims(),
            "dropped_claims": store.dropped_claims(),
            "agent_outcomes": [outcome],
            "llm_calls": calls,
        }

    run_agent.__name__ = f"run_{name}_agent"
    return run_agent


def _collect_node(state: ResearchState) -> dict[str, Any]:
    """Fan-in: rebuild ONE store from every agent's output and re-validate.

    Each agent already validated its own claims against its own store; this
    is the run-wide second check, over the merged result the later phases
    (Bull/Bear, Judge) will read:
      - `add_evidence` re-asserts C3: nothing published after as_of.
      - `add_claim` re-asserts §6 rule 2: every cited evidence_id resolves.

    WHY let these raise rather than catching them like the agent bulkhead
    does: if either fails here, some node returned a claim or evidence row
    that should have been impossible — a bug in this codebase, not an
    external failure. A run that can't prove its own citations must not
    produce output.
    """
    store = EvidenceStore(run_id=state["run_id"], as_of=state["as_of"])
    for evidence in state["evidence"]:
        store.add_evidence(evidence)
    for claim in state["claims"]:
        store.add_claim(claim)
    return {"completed": True}


def build_research_graph(
    deps: RunDependencies, checkpointer: BaseCheckpointSaver[Any] | None
) -> CompiledStateGraph[ResearchState, None, ResearchState, ResearchState]:
    """Build and compile the research graph.

    Args:
        deps: live clients/factories the nodes close over.
        checkpointer: where to save a checkpoint after each superstep.
            `AsyncPostgresSaver` for real runs; `InMemorySaver` in tests.
            None disables checkpointing (no resume).
    """
    builder: StateGraph[ResearchState, None, ResearchState, ResearchState] = StateGraph(
        ResearchState
    )
    builder.add_node(PLAN_NODE, _plan_node(deps))
    for name in ALL_AGENTS:
        builder.add_node(name, _agent_node(name, deps))
        builder.add_edge(name, COLLECT_NODE)
    builder.add_node(COLLECT_NODE, _collect_node)

    builder.add_edge(START, PLAN_NODE)
    builder.add_conditional_edges(PLAN_NODE, _route_after_plan, [*ALL_AGENTS, COLLECT_NODE])
    builder.add_edge(COLLECT_NODE, END)
    return builder.compile(checkpointer=checkpointer)
