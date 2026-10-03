"""Research planning: the `Planner` seam and the deterministic plan.

The graph's `plan` node asks a `Planner` for the run's plan. There are two:
  - `ResearchManager` (backend/agents/manager.py): Haiku decides, and this
    is what real runs use.
  - `DeterministicPlanner` (below): rule-based. Tests use it, and it's the
    Manager's recorded fallback. So `deterministic_plan` must stay a pure
    function of its inputs that can never itself fail.

WHY skipping an agent is a precondition check here, not a judgement call:
the Competitive Agent has nothing to compare against when a ticker isn't in
the curated peer map (backend/agents/peer_map.py) — running it would only
return zero claims after spending a data fetch. That's a fact about the
inputs, so Python decides it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Protocol

from backend.agents.filings import DEFAULT_RESEARCH_QUESTIONS
from backend.orchestration.state import AgentRoute, ResearchPlan, SkippedAgent


class Planner(Protocol):
    """Anything that can plan a run. Must never raise for an expected
    failure (a bad LLM output, missing company data). It returns a
    fallback plan instead, so planning can't be what kills a run."""

    async def plan(self, ticker: str, as_of: date) -> ResearchPlan: ...


class DeterministicPlanner:
    """The rule-based planner, as a `Planner`."""

    def __init__(self, peer_map: Mapping[str, tuple[str, ...]]) -> None:
        self._peer_map = peer_map

    async def plan(self, ticker: str, as_of: date) -> ResearchPlan:
        return deterministic_plan(ticker, self._peer_map)


def deterministic_plan(
    ticker: str,
    peer_map: Mapping[str, tuple[str, ...]],
    *,
    fallback_reason: str | None = None,
) -> ResearchPlan:
    """Run every agent whose preconditions hold, with the default Filings
    questions.

    Args:
        ticker: the run's ticker.
        peer_map: the curated peer map the Competitive Agent will use —
            passed in (not imported) so the plan and the agent can never
            disagree about which peers exist.
        fallback_reason: set when this plan stands in for a failed
            Research Manager. The plan is then marked source="fallback"
            with the reason attached.
    """
    routes = [
        AgentRoute(agent="financial", rationale="Always run: core XBRL fundamentals."),
        AgentRoute(agent="filings", rationale="Always run: 10-K narrative via RAG."),
        AgentRoute(agent="news", rationale="Always run: 8-K material events."),
        AgentRoute(agent="valuation", rationale="Always run: reverse DCF on the market price."),
    ]
    skipped: list[SkippedAgent] = []
    if peer_map.get(ticker):
        routes.append(AgentRoute(agent="competitive", rationale="Ticker has a curated peer group."))
    else:
        skipped.append(
            SkippedAgent(
                agent="competitive",
                reason=f"{ticker} is not in the curated peer map — no peers to compare.",
            )
        )
    return ResearchPlan(
        source="fallback" if fallback_reason is not None else "deterministic",
        fallback_reason=fallback_reason,
        routes=routes,
        skipped=skipped,
        filings_questions=list(DEFAULT_RESEARCH_QUESTIONS),
    )
