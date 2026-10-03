"""The Research Manager: plans which agents run and what Filings researches.

New-concept note: in an agent architecture, a *manager* (or planner,
router) is the agent that decides what the other agents do, rather than
doing research itself. Here that means two things, both CLAUDE.md §7
"research planning and routing" judgement calls given to Claude:
  1. Routing: for each specialist agent, run it or skip it, with a reason.
  2. Planning: the ticker-specific questions the Filings Agent searches its
     10-K for (a bank's risks aren't a chipmaker's).

What stops this from being "the LLM does whatever it likes":
  - **Closed choices.** The output schema only admits known agent names
    (a Literal), so an invented agent can't parse.
  - **Rules checked in code** (`_validate_plan`, via `call_structured`'s
    `extra_validation`): every agent routed or skipped exactly once, at
    least one routed, Competitive only if the ticker has curated peers, and
    a bounded number of Filings questions. A violation gets one retry with
    the error fed back. A second failure means the deterministic plan runs
    instead, marked `source="fallback"` with the reason.
  - **Trusted inputs only (C7).** The Manager sees the ticker, as_of, and
    SEC's own name/SIC metadata, wrapped in data-not-instructions
    delimiters. It never sees retrieved filing or news text, so injected
    content has no path to change the plan (§10: "Never let retrieved text
    change ... the research plan").
See ADR-0020.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import date

from pydantic import BaseModel, Field

from backend.agents.prompt_loader import load_prompt
from backend.core.config import Settings
from backend.core.llm import ClaudeAPIError, ClaudeClient
from backend.data.company import CompanyClient
from backend.data.errors import AgentInvestDataError
from backend.data.models import CompanyProfile, DataUnavailable
from backend.orchestration.planning import deterministic_plan
from backend.orchestration.state import ALL_AGENTS, AgentRoute, ResearchPlan, SkippedAgent

logger = logging.getLogger("agentinvest.agents.manager")

_TOOL_NAME = "emit_research_plan"


class ManagerOutput(BaseModel):
    """What Claude must return: the plan minus its provenance fields
    (`source`/`fallback_reason` are set by Python, never by the model)."""

    routes: list[AgentRoute]
    skipped: list[SkippedAgent]
    filings_questions: list[str] = Field(default_factory=list)


def _validate_plan(
    output: ManagerOutput,
    *,
    ticker: str,
    peer_map: Mapping[str, tuple[str, ...]],
    min_questions: int,
    max_questions: int,
) -> None:
    """The routing rules, enforced in code. Raises ValueError, which
    `call_structured` turns into one retry with this message fed back."""
    routed = [r.agent for r in output.routes]
    skipped = [s.agent for s in output.skipped]
    decided = routed + skipped
    if sorted(decided) != sorted(ALL_AGENTS):
        raise ValueError(
            f"Every agent must appear exactly once, in routes or skipped. "
            f"Expected {sorted(ALL_AGENTS)}, got routes={routed}, skipped={skipped}."
        )
    if not routed:
        raise ValueError("At least one agent must be routed; an empty plan researches nothing.")
    if "competitive" in routed and not peer_map.get(ticker):
        raise ValueError(
            f"'competitive' cannot be routed: {ticker} has no curated peer group, so the "
            "Competitive Agent has nothing to compare against. Skip it instead."
        )
    if "filings" in routed:
        questions = [q.strip() for q in output.filings_questions if q.strip()]
        if not min_questions <= len(questions) <= max_questions:
            raise ValueError(
                f"'filings' is routed, so provide between {min_questions} and "
                f"{max_questions} non-empty filings_questions (got {len(questions)})."
            )


def _format_profile(profile: CompanyProfile) -> str:
    return (
        f"name: {profile.name}\n"
        f"sic: {profile.sic or 'unknown'}\n"
        f"sic_description: {profile.sic_description or 'unknown'}"
    )


class ResearchManager:
    """Plans a run with Haiku, falling back to the deterministic plan.

    Implements backend/orchestration/planning.py's `Planner` protocol.
    """

    def __init__(
        self,
        company: CompanyClient,
        claude: ClaudeClient,
        settings: Settings,
        peer_map: Mapping[str, tuple[str, ...]],
    ) -> None:
        self._company = company
        self._claude = claude
        self._settings = settings
        self._peer_map = peer_map

    @property
    def agent_name(self) -> str:
        return "manager"

    async def plan(self, ticker: str, as_of: date) -> ResearchPlan:
        try:
            profile = await self._company.get_company_profile(ticker, as_of)
        # WHY: SEC being down (after retries) or rejecting us is a planning
        # failure like any other. The run proceeds on the fallback plan, and
        # the agents will each hit, and record, the same outage themselves.
        except AgentInvestDataError as exc:
            return self._fallback(
                ticker, f"Company profile fetch failed: {type(exc).__name__}: {exc}"
            )
        if isinstance(profile, DataUnavailable):
            return self._fallback(ticker, f"Company profile unavailable: {profile.reason}")

        peers = self._peer_map.get(ticker, ())
        system_prompt = load_prompt("manager_agent_v1").format(
            min_questions=self._settings.manager_min_filings_questions,
            max_questions=self._settings.manager_max_filings_questions,
        )
        user_message = (
            f"Ticker: {ticker}\n"
            f"As-of date: {as_of.isoformat()}\n"
            f"Curated peer group: {', '.join(peers) if peers else 'none'}\n\n"
            "<company_profile>\n"
            f"{_format_profile(profile)}\n"
            "</company_profile>\n\n"
            "Emit the research plan."
        )

        def validate(output: ManagerOutput) -> None:
            _validate_plan(
                output,
                ticker=ticker,
                peer_map=self._peer_map,
                min_questions=self._settings.manager_min_filings_questions,
                max_questions=self._settings.manager_max_filings_questions,
            )

        try:
            # WHY asyncio.to_thread: see ADR-0019. call_structured is sync.
            output = await asyncio.to_thread(
                self._claude.call_structured,
                agent=self.agent_name,
                model_cls=ManagerOutput,
                messages=[{"role": "user", "content": user_message}],
                system=system_prompt,
                model=self._settings.manager_agent_model,
                max_tokens=self._settings.manager_agent_max_tokens,
                tool_name=_TOOL_NAME,
                tool_description=(
                    "Emit the research plan: which agents to run or skip (with reasons) "
                    "and the Filings Agent's research questions."
                ),
                extra_validation=validate,
            )
        # WHY catch every ClaudeAPIError (a validation failure after the one
        # retry, a refusal, a truncated output, an API outage): the planner
        # must never be what kills a run. Planning failed, so the run goes
        # ahead on the deterministic plan, and the reason is recorded.
        except ClaudeAPIError as exc:
            logger.warning(
                "manager_fallback", extra={"ticker": ticker, "reason": f"{type(exc).__name__}"}
            )
            return self._fallback(ticker, f"Research Manager failed: {type(exc).__name__}: {exc}")

        return ResearchPlan(
            source="llm",
            routes=output.routes,
            skipped=output.skipped,
            filings_questions=[q.strip() for q in output.filings_questions if q.strip()],
        )

    def _fallback(self, ticker: str, reason: str) -> ResearchPlan:
        return deterministic_plan(ticker, self._peer_map, fallback_reason=reason)
