"""The Competitive Agent: compares a company's XBRL fundamentals against a
curated set of peers. CLAUDE.md §15: "peers from a curated map, and an
explicit rule that market-share claims require a citation or are not
made."

Reuses backend/agents/xbrl_facts.py's fact-selection logic (see ADR-0016)
rather than the Financial Agent's Evidence construction directly — this
agent's evidence shape genuinely differs (revenue + net margin only, per
company, across multiple companies, tagged with which company each row
belongs to).

Market-share enforcement needs zero new validation code: no
backend/data/ module can produce market-share/TAM data, so Python never
creates that Evidence, so Claude can never cite one, so
EvidenceStore.validate_claim_batch structurally rejects any
claim_type="evidence" attempt at one. The one gap this doesn't
structurally close — an "inference"/"assumption" claim citing real
revenue evidence while asserting an unrelated market-share number — is
closed by an explicit prompt instruction instead (inference/assumption
claims aren't checked for whether their cited evidence actually supports
the specific assertion, per CLAUDE.md §6 rule 5's "judgement, not fact"
category). See ADR-0018.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from json import dumps as json_dumps
from uuid import uuid4

from backend.agents.peer_map import PEER_MAP
from backend.agents.prompt_loader import load_prompt
from backend.agents.xbrl_facts import ALL_ALIASES, CONCEPT_ALIASES, select_anchor, select_matching
from backend.calc.ratios import RATIO_FUNCS, RatioInputError
from backend.core.config import Settings
from backend.core.llm import ClaudeClient, StructuredOutputError
from backend.data.models import DataUnavailable, XBRLFact
from backend.data.xbrl import XBRLClient, find_raw_entry
from backend.evidence.models import Claim, ClaimBatch, Evidence
from backend.evidence.store import EvidenceStore

logger = logging.getLogger("agentinvest.agents.competitive")


def _format_evidence_line(evidence: Evidence) -> str:
    loc = evidence.location
    company = loc.get("company", "?")
    if evidence.source_type == "xbrl_fact":
        return (
            f"- id={evidence.id} type=xbrl_fact company={company} :: "
            f"{loc['concept']} = {loc['value']} (period ending {loc['period_end']})"
        )
    if evidence.source_type == "computed":
        return (
            f"- id={evidence.id} type=computed company={company} :: "
            f"{loc['ratio_name']} = {loc['value']:.4f}"
        )
    return (
        f"- id={evidence.id} type={evidence.source_type} company={company} :: "
        f"{evidence.quote[:120]}"
    )


class CompetitiveAgent:
    """Compares a ticker's XBRL fundamentals (revenue, net margin) against
    a curated set of peers, and asks Claude which comparisons are
    claim-worthy."""

    def __init__(
        self,
        xbrl: XBRLClient,
        claude: ClaudeClient,
        store: EvidenceStore,
        settings: Settings,
        peer_map: dict[str, tuple[str, ...]] = PEER_MAP,
    ) -> None:
        self._xbrl = xbrl
        self._claude = claude
        self._store = store
        self._settings = settings
        self._peer_map = peer_map

    @property
    def agent_name(self) -> str:
        return "competitive"

    async def run(self, ticker: str, as_of: date) -> list[Claim]:
        peers = self._peer_map.get(ticker, ())
        if not peers:
            # An uncovered ticker produces no fabricated comparison, not a
            # crash — C6 applied to a curation gap, not just a data-fetch
            # failure.
            logger.info("competitive_agent_no_peers", extra={"ticker": ticker})
            return []

        for company in (ticker, *peers):
            result = await self._xbrl.get_company_facts_with_raw(
                company, as_of, concepts=ALL_ALIASES
            )
            if isinstance(result, DataUnavailable):
                # One peer's XBRL being unavailable shouldn't erase claims
                # comparing the others.
                logger.info(
                    "competitive_agent_company_unavailable",
                    extra={"ticker": ticker, "company": company, "reason": result.reason},
                )
                continue

            facts = [f for f in result.facts if f.unit == "USD"]
            self._build_company_evidence(company, facts, result.raw)

        if not self._store.all_evidence(source_type="xbrl_fact") and not self._store.all_evidence(
            source_type="computed"
        ):
            return []

        return await self._emit_claims(ticker, peers)

    def _build_company_evidence(
        self, company: str, facts: list[XBRLFact], raw: dict[str, object]
    ) -> None:
        revenue_facts = [f for f in facts if f.concept in CONCEPT_ALIASES["revenue"]]
        anchor_revenue = select_anchor(revenue_facts, instant=False)
        if anchor_revenue is None:
            return

        revenue_evidence = self._make_fact_evidence(company, anchor_revenue, raw)
        self._store.add_evidence(revenue_evidence)

        net_income = select_matching(facts, CONCEPT_ALIASES["net_income"], anchor_revenue)
        if net_income is None:
            return
        net_income_evidence = self._make_fact_evidence(company, net_income, raw)

        inputs = {"revenue": anchor_revenue.value, "net_income": net_income.value}
        try:
            result = RATIO_FUNCS["net_margin"](**inputs)
        except RatioInputError as exc:
            logger.info(
                "competitive_agent_ratio_skipped",
                extra={"company": company, "ratio": "net_margin", "reason": str(exc)},
            )
            return

        self._store.add_evidence(net_income_evidence)
        self._store.add_evidence(
            Evidence(
                id=uuid4(),
                run_id=self._store.run_id,
                source_type="computed",
                source_ref=anchor_revenue.accession_number,
                published_at=max(revenue_evidence.published_at, net_income_evidence.published_at),
                retrieved_at=datetime.now(UTC),
                quote=f"{result.name} = {result.formula} = {result.value:.6f}",
                location={
                    "company": company,
                    "ratio_name": result.name,
                    "input_evidence_ids": {
                        "revenue": str(revenue_evidence.id),
                        "net_income": str(net_income_evidence.id),
                    },
                    "value": result.value,
                },
            )
        )

    def _make_fact_evidence(self, company: str, fact: XBRLFact, raw: dict[str, object]) -> Evidence:
        entry = find_raw_entry(raw, fact)
        return Evidence(
            id=uuid4(),
            run_id=self._store.run_id,
            source_type="xbrl_fact",
            source_ref=fact.accession_number,
            published_at=fact.filed,
            retrieved_at=datetime.now(UTC),
            quote=json_dumps(entry, sort_keys=True, separators=(",", ":")),
            location={
                "company": company,
                "concept": fact.concept,
                "taxonomy": fact.taxonomy,
                "unit": fact.unit,
                "value": fact.value,
                "accession_number": fact.accession_number,
                "period_end": fact.period_end.isoformat(),
                "fiscal_year": fact.fiscal_year,
                "fiscal_period": fact.fiscal_period,
            },
        )

    async def _emit_claims(self, ticker: str, peers: tuple[str, ...]) -> list[Claim]:
        all_evidence = [
            *self._store.all_evidence(source_type="xbrl_fact"),
            *self._store.all_evidence(source_type="computed"),
        ]
        evidence_context = "\n".join(_format_evidence_line(ev) for ev in all_evidence)
        system_prompt = load_prompt("competitive_agent_v1").format(
            evidence_context=evidence_context
        )

        try:
            # WHY asyncio.to_thread: see ADR-0019 — ClaudeClient.call_structured
            # is synchronous; called directly it would block the event loop
            # for every other concurrently-running agent.
            batch = await asyncio.to_thread(
                self._claude.call_structured,
                agent=self.agent_name,
                model_cls=ClaimBatch,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Ticker: {ticker}. Peers: {', '.join(peers)}. "
                            "Emit claims for the evidence above."
                        ),
                    }
                ],
                system=system_prompt,
                model=self._settings.competitive_agent_model,
                max_tokens=self._settings.competitive_agent_max_tokens,
                tool_name="emit_claims",
                tool_description=(
                    "Emit a batch of Claim objects for the evidence shown, "
                    "citing only the given evidence_ids."
                ),
                extra_validation=self._store.validate_claim_batch,
            )
        except StructuredOutputError as exc:
            self._store.record_dropped_claim(
                agent=self.agent_name, raw_input=exc.raw_input, reason=str(exc.last_error)
            )
            logger.warning(
                "competitive_agent_claims_dropped",
                extra={"ticker": ticker, "reason": str(exc.last_error)},
            )
            return []

        for claim in batch.claims:
            self._store.add_claim(claim)
        return batch.claims
