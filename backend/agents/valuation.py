"""The Valuation Agent: what growth is the market price assuming, and is
that plausible against the company's own history? (CLAUDE.md §8)

Python does everything numeric: assembling inputs from XBRL, prices and
FRED (valuation_inputs.py), computing the reverse DCF (valuation_compute.py),
and turning every number into recomputable evidence (valuation_evidence.py).
Claude is called once, only to interpret the headline rows (implied growth
against historical growth, whether growth creates value) and phrase claims
citing them. It never sees a number it could change, and every number it
cites recomputes in CI from source data (backend/evidence/validation.py).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from backend.agents.prompt_loader import load_prompt
from backend.agents.valuation_compute import compute_valuation
from backend.agents.valuation_evidence import ValuationEvidence, build_valuation_evidence
from backend.agents.valuation_inputs import ValuationInputAssembler, ValuationUnavailable
from backend.core.config import Settings
from backend.core.llm import AgentInvestError, ClaudeClient, StructuredOutputError
from backend.evidence.models import Claim, ClaimBatch, Evidence
from backend.evidence.store import EvidenceStore

logger = logging.getLogger("agentinvest.agents.valuation")

# What each headline row means, in words Claude can use without doing
# arithmetic. Order is the order shown in the prompt.
_HEADLINE_LABELS: dict[str, str] = {
    "implied_growth": "Revenue growth per year the market price implies (reverse DCF)",
    "revenue_cagr_3y": "Historical revenue growth per year, last 3 fiscal years",
    "revenue_cagr_5y": "Historical revenue growth per year, last 5 fiscal years",
    "operating_margin": "Operating margin, average of last 3 fiscal years",
    "incremental_investment_rate": "Net investment per $1 of incremental revenue (3 years)",
    "value_neutral_investment_rate": "Break-even investment rate (growth adds value below it)",
    "wacc": "Weighted average cost of capital",
    "cost_of_equity": "Cost of equity (CAPM)",
    "cost_of_debt": "Cost of debt",
    "beta": "Beta vs the market",
    "risk_free_rate": "Risk-free rate (10-year Treasury)",
    "market_cap": "Market capitalisation (USD)",
    "net_debt": "Net debt (USD; negative means net cash)",
    "target_enterprise_value": "Enterprise value implied by the price (USD)",
    "price": "Share price, as traded (USD)",
}


class ValuationUnavailableError(AgentInvestError):
    """Required valuation inputs are missing. Raised (not returned) so the
    orchestrator's bulkhead records the agent as failed WITH the list of
    missing inputs: a visible gap in the run, never a silent empty result
    (C6)."""


def _format_headline(name: str, evidence: Evidence) -> str:
    loc = evidence.location
    value = loc["value"]
    if name in ("market_cap", "net_debt", "target_enterprise_value"):
        shown = f"{value:,.0f}"
    elif name == "price":
        shown = f"{value:,.2f}"
    elif name in ("incremental_investment_rate", "value_neutral_investment_rate", "beta"):
        shown = f"{value:.3f}"
    else:
        shown = f"{value:.2%}"
    line = f"- id={evidence.id} :: {_HEADLINE_LABELS[name]}: {shown}"
    if name == "implied_growth":
        if loc["ratio_name"] == "implied_growth_solutions":
            line = (
                f"- id={evidence.id} :: NO growth rate fits the market price. "
                f"Reason: {loc['reason']}"
            )
        elif not loc["growth_creates_value"]:
            line += " (on these drivers growth DESTROYS value: a higher price implies LOWER growth)"
        else:
            line += " (on these drivers growth creates value)"
    return line


class ValuationAgent:
    """Runs the reverse DCF and asks Claude what the result means."""

    def __init__(
        self,
        assembler: ValuationInputAssembler,
        claude: ClaudeClient,
        store: EvidenceStore,
        settings: Settings,
    ) -> None:
        self._assembler = assembler
        self._claude = claude
        self._store = store
        self._settings = settings

    @property
    def agent_name(self) -> str:
        return "valuation"

    async def run(self, ticker: str, as_of: date) -> list[Claim]:
        inputs = await self._assembler.assemble(ticker, as_of)
        if isinstance(inputs, ValuationUnavailable):
            raise ValuationUnavailableError(
                "Valuation inputs unavailable: " + "; ".join(inputs.missing)
            )
        result = compute_valuation(inputs, self._settings)
        evidence = build_valuation_evidence(result, self._store.run_id, self._settings)
        for row in evidence.rows:
            self._store.add_evidence(row)
        return await self._emit_claims(ticker, evidence, inputs.assumptions)

    async def _emit_claims(
        self, ticker: str, evidence: ValuationEvidence, assumptions: list[str]
    ) -> list[Claim]:
        lines = [
            _format_headline(name, evidence.headline[name])
            for name in _HEADLINE_LABELS
            if name in evidence.headline
        ]
        system_prompt = load_prompt("valuation_agent_v1").format(
            evidence_context="\n".join(lines),
            assumptions="\n".join(f"- {a}" for a in assumptions),
            horizon_years=self._settings.valuation_horizon_years,
        )
        try:
            # WHY asyncio.to_thread: see ADR-0019.
            batch = await asyncio.to_thread(
                self._claude.call_structured,
                agent=self.agent_name,
                model_cls=ClaimBatch,
                messages=[
                    {
                        "role": "user",
                        "content": f"Ticker: {ticker}. Emit claims for the valuation above.",
                    }
                ],
                system=system_prompt,
                model=self._settings.valuation_agent_model,
                max_tokens=self._settings.valuation_agent_max_tokens,
                tool_name="emit_claims",
                tool_description=(
                    "Emit a batch of Claim objects for the valuation shown, "
                    "citing only the given evidence_ids."
                ),
                extra_validation=self._store.validate_claim_batch,
            )
        except StructuredOutputError as exc:
            self._store.record_dropped_claim(
                agent=self.agent_name, raw_input=exc.raw_input, reason=str(exc.last_error)
            )
            logger.warning(
                "valuation_agent_claims_dropped",
                extra={"ticker": ticker, "reason": str(exc.last_error)},
            )
            return []

        for claim in batch.claims:
            self._store.add_claim(claim)
        return batch.claims
