"""The deterministic research plan.

In Increment 5a this IS the plan for every run. In Increment 5b the LLM
Research Manager takes over, and this becomes its recorded fallback when the
Manager's output fails validation twice — so it must stay a pure function of
its inputs that can never itself fail.

WHY skipping an agent is a precondition check here, not a judgement call:
the Competitive Agent has nothing to compare against when a ticker isn't in
the curated peer map (backend/agents/peer_map.py) — running it would only
return zero claims after spending a data fetch. That's a fact about the
inputs, so Python decides it.
"""

from __future__ import annotations

from collections.abc import Mapping

from backend.agents.filings import DEFAULT_RESEARCH_QUESTIONS
from backend.orchestration.state import AgentRoute, ResearchPlan, SkippedAgent


def deterministic_plan(ticker: str, peer_map: Mapping[str, tuple[str, ...]]) -> ResearchPlan:
    """Run every agent whose preconditions hold, with the default Filings
    questions.

    Args:
        ticker: the run's ticker.
        peer_map: the curated peer map the Competitive Agent will use —
            passed in (not imported) so the plan and the agent can never
            disagree about which peers exist.
    """
    routes = [
        AgentRoute(agent="financial", rationale="Always run: core XBRL fundamentals."),
        AgentRoute(agent="filings", rationale="Always run: 10-K narrative via RAG."),
        AgentRoute(agent="news", rationale="Always run: 8-K material events."),
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
        source="deterministic",
        routes=routes,
        skipped=skipped,
        filings_questions=list(DEFAULT_RESEARCH_QUESTIONS),
    )
