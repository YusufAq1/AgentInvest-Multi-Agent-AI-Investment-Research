"""The research run's state: what flows through the LangGraph graph.

New-concept note (LangGraph is new to this project's author — see also
docs/LANGGRAPH_CONCEPTS.md): a LangGraph graph passes ONE state object
between nodes. A node never mutates it; it returns a *partial update* (a
dict of just the keys it wants to change), and LangGraph merges that update
into the state. How it merges each key is decided here, per key:

  - A plain key (`plan`, `completed`): last write wins.
  - A key annotated with a *reducer* (`Annotated[list[X], operator.add]`):
    LangGraph calls `reducer(current_value, update)` — here, list
    concatenation. This is what makes parallel agent nodes safe: four agent
    nodes running in the same step each return their own `claims` list, and
    LangGraph concatenates all four rather than letting the last one to
    finish overwrite the others.

WHY the state holds plain lists of Evidence/Claim rather than the live
`EvidenceStore` object the Phase 4 demo shared across agents: state is
*checkpointed* — serialised to Postgres after every step so an interrupted
run can resume (see backend/orchestration/runner.py). A checkpoint must be
data, not a live object holding methods and closures. Each agent node
therefore works against its own private `EvidenceStore` and returns what it
produced as a state delta; the `collect` node rebuilds one merged store
from state and re-validates everything. See ADR-0001.
"""

from __future__ import annotations

import operator
from datetime import date
from typing import Annotated, Final, Literal, NotRequired, TypedDict
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from backend.core.llm import LLMCallResult
from backend.evidence.models import Claim, DroppedClaim, Evidence

AgentName = Literal["financial", "filings", "news", "competitive", "valuation"]

# WHY an explicit tuple alongside the Literal: the graph needs to iterate
# over every possible agent node at build time (one node per name), and a
# Literal can't be iterated without typing.get_args reflection.
ALL_AGENTS: Final[tuple[AgentName, ...]] = (
    "financial",
    "filings",
    "news",
    "competitive",
    "valuation",
)


class AgentRoute(BaseModel):
    """One agent the plan decided to run, and why."""

    agent: AgentName
    rationale: str


class SkippedAgent(BaseModel):
    """One agent the plan decided NOT to run, and why. Recorded rather than
    silently omitted: a skipped agent is a gap in the run's coverage, and
    §9's data_completeness component (Phase 7) needs to see it."""

    agent: AgentName
    reason: str


class ResearchPlan(BaseModel):
    """What the run will research.

    `source` says who made the plan:
      - "llm": the Research Manager (backend/agents/manager.py)
      - "deterministic": planning.py's rule-based plan, used when a run is
        configured without the Manager (tests, or by choice)
      - "fallback": the deterministic plan, used because the Manager
        failed. `fallback_reason` says why. It's recorded and surfaced,
        never silent.
    """

    source: Literal["llm", "deterministic", "fallback"]
    routes: list[AgentRoute]
    skipped: list[SkippedAgent]
    filings_questions: list[str] = Field(default_factory=list)
    fallback_reason: str | None = None

    @model_validator(mode="after")
    def _routes_and_skips_are_disjoint(self) -> ResearchPlan:
        routed = [r.agent for r in self.routes]
        skipped = [s.agent for s in self.skipped]
        if len(set(routed)) != len(routed):
            raise ValueError(f"Duplicate agent routes: {routed}")
        if set(routed) & set(skipped):
            raise ValueError(f"Agents both routed and skipped: {set(routed) & set(skipped)}")
        if (self.source == "fallback") != (self.fallback_reason is not None):
            raise ValueError("fallback_reason is required for, and only for, a fallback plan")
        return self

    def routed_agents(self) -> list[AgentName]:
        return [route.agent for route in self.routes]


class AgentOutcome(BaseModel):
    """What happened to one agent in one run — the bulkhead's record.

    WHY this exists at all: in the Phase 4 demo, one agent raising cancelled
    the whole `asyncio.gather`. Now a failed agent becomes a recorded gap
    (CLAUDE.md C6: a gap is surfaced, never papered over) and the rest of
    the run continues.
    """

    agent: AgentName
    status: Literal["succeeded", "failed", "skipped"]
    claims_count: int = 0
    dropped_count: int = 0
    duration_ms: float = 0.0
    # Failure: "<ExceptionType>: <message>". Skip: the plan's reason.
    detail: str | None = None


class ResearchState(TypedDict):
    """The full state of one research run.

    `run_id`/`ticker`/`as_of` are the run's input. Everything else is
    written by nodes. `NotRequired` keys don't exist until the node that
    writes them has run; reducer keys start as an empty list.
    """

    run_id: UUID
    ticker: str
    # CLAUDE.md C3: as_of is part of the run's identity, set once at the
    # start, and every node reads it from here — never from "today".
    as_of: date

    plan: NotRequired[ResearchPlan]
    evidence: Annotated[list[Evidence], operator.add]
    claims: Annotated[list[Claim], operator.add]
    dropped_claims: Annotated[list[DroppedClaim], operator.add]
    agent_outcomes: Annotated[list[AgentOutcome], operator.add]
    llm_calls: Annotated[list[LLMCallResult], operator.add]
    completed: NotRequired[bool]


# Every custom type the checkpoint serializer may need to rebuild from
# Postgres — see backend/orchestration/runner.py's make_serializer().
CHECKPOINT_TYPES: Final[tuple[type[BaseModel], ...]] = (
    AgentOutcome,
    AgentRoute,
    Claim,
    DroppedClaim,
    Evidence,
    LLMCallResult,
    ResearchPlan,
    SkippedAgent,
)
