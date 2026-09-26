"""The Financial Agent: reads XBRL, computes ratios in pure Python, and
uses Claude only to decide which computed facts are claim-worthy and how
to phrase/classify them (CLAUDE.md §7's dividing line, applied literally).

Python creates every Evidence row deterministically BEFORE Claude is ever
called. Claude can only cite evidence_ids from the set it's shown — it
never creates evidence, never touches a number. This is what makes the
Financial Agent's claims impossible to fabricate: a hallucinated
evidence_id fails EvidenceStore.resolve and the whole claim batch is
rejected (see backend/core/llm.py's call_structured and
backend/evidence/store.py's docstring for where the two halves of that
enforcement live).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from json import dumps as json_dumps
from uuid import uuid4

from backend.agents.prompt_loader import load_prompt
from backend.agents.xbrl_facts import (
    ALL_ALIASES,
    CONCEPT_ALIASES,
    FactKey,
    fact_key,
    select_anchor,
    select_matching,
    select_prior_year,
)
from backend.calc.ratios import RATIO_FUNCS, RatioInputError
from backend.core.config import Settings
from backend.core.llm import ClaudeClient, StructuredOutputError
from backend.data.models import DataUnavailable, XBRLFact
from backend.data.xbrl import XBRLClient, find_raw_entry
from backend.evidence.models import Claim, ClaimBatch, Evidence
from backend.evidence.store import EvidenceStore

logger = logging.getLogger("agentinvest.agents.financial")


def _format_evidence_line(evidence: Evidence) -> str:
    if evidence.source_type == "xbrl_fact":
        loc = evidence.location
        return (
            f"- id={evidence.id} type=xbrl_fact :: {loc['concept']} = {loc['value']} "
            f"(period ending {loc['period_end']}, {loc['fiscal_period']} FY{loc['fiscal_year']})"
        )
    if evidence.source_type == "computed":
        loc = evidence.location
        return f"- id={evidence.id} type=computed :: {loc['ratio_name']} = {loc['value']:.4f}"
    return f"- id={evidence.id} type={evidence.source_type} :: {evidence.quote[:120]}"


class FinancialAgent:
    """Reads XBRL fundamentals for one ticker, computes ratios, and asks
    Claude which resulting facts are claim-worthy."""

    def __init__(
        self, xbrl: XBRLClient, claude: ClaudeClient, store: EvidenceStore, settings: Settings
    ) -> None:
        self._xbrl = xbrl
        self._claude = claude
        self._store = store
        self._settings = settings

    @property
    def agent_name(self) -> str:
        return "financial"

    async def run(self, ticker: str, as_of: date) -> list[Claim]:
        result = await self._xbrl.get_company_facts_with_raw(ticker, as_of, concepts=ALL_ALIASES)
        if isinstance(result, DataUnavailable):
            # C6: missing data means no claims, never a crash and never a
            # fabricated number.
            logger.info(
                "financial_agent_no_data", extra={"ticker": ticker, "reason": result.reason}
            )
            return []

        candidate_facts = [f for f in result.facts if f.unit == "USD"]

        # WHY evidence is built lazily, only for facts actually selected
        # below, rather than eagerly for every historical fact matching the
        # concept aliases: SEC XBRL companyfacts includes every filing that
        # ever reported a concept, including comparative/restated periods
        # from years of 10-Ks and 10-Qs. Eagerly turning all of that into
        # Evidence (and therefore into the prompt Claude sees) blew up a
        # real run to 78k+ input tokens for one ticker — expensive (C2) and
        # eventually truncates the response. Only the handful of facts
        # actually used for ratios (plus the bare anchor facts, so a
        # raw-fact-only claim is still possible when a ratio can't be
        # computed) become Evidence.
        fact_evidence: dict[FactKey, Evidence] = {}

        def ensure_fact_evidence(fact: XBRLFact) -> Evidence:
            key = fact_key(fact)
            if key not in fact_evidence:
                fact_evidence[key] = self._make_fact_evidence(fact, result.raw)
            return fact_evidence[key]

        ratio_evidence = self._build_ratio_evidence(ticker, candidate_facts, ensure_fact_evidence)

        for evidence in [*fact_evidence.values(), *ratio_evidence]:
            self._store.add_evidence(evidence)

        if not self._store.all_evidence():
            return []

        return await self._emit_claims(ticker)

    def _make_fact_evidence(self, fact: XBRLFact, raw: dict[str, object]) -> Evidence:
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

    def _build_ratio_evidence(
        self,
        ticker: str,
        facts: list[XBRLFact],
        ensure_fact_evidence: Callable[[XBRLFact], Evidence],
    ) -> list[Evidence]:
        ratio_evidence: list[Evidence] = []

        revenue_facts = [f for f in facts if f.concept in CONCEPT_ALIASES["revenue"]]
        anchor_revenue = select_anchor(revenue_facts, instant=False)
        if anchor_revenue is not None:
            # Ensure the bare revenue fact is citable even if no ratio
            # using it can be computed (e.g. cogs is missing).
            ensure_fact_evidence(anchor_revenue)
            cogs = select_matching(facts, CONCEPT_ALIASES["cogs"], anchor_revenue)
            if cogs is not None:
                self._try_add_ratio(
                    ratio_evidence,
                    ticker,
                    "gross_margin",
                    {"revenue": anchor_revenue, "cogs": cogs},
                    ensure_fact_evidence,
                )
            net_income = select_matching(facts, CONCEPT_ALIASES["net_income"], anchor_revenue)
            if net_income is not None:
                self._try_add_ratio(
                    ratio_evidence,
                    ticker,
                    "net_margin",
                    {"revenue": anchor_revenue, "net_income": net_income},
                    ensure_fact_evidence,
                )
            prior_revenue = select_prior_year(revenue_facts, anchor_revenue)
            if prior_revenue is not None:
                self._try_add_ratio(
                    ratio_evidence,
                    ticker,
                    "yoy_revenue_growth",
                    {"revenue_current": anchor_revenue, "revenue_prior": prior_revenue},
                    ensure_fact_evidence,
                )

        ca_facts = [f for f in facts if f.concept in CONCEPT_ALIASES["current_assets"]]
        anchor_ca = select_anchor(ca_facts, instant=True)
        if anchor_ca is not None:
            ensure_fact_evidence(anchor_ca)
            cl = select_matching(facts, CONCEPT_ALIASES["current_liabilities"], anchor_ca)
            if cl is not None:
                self._try_add_ratio(
                    ratio_evidence,
                    ticker,
                    "current_ratio",
                    {"current_assets": anchor_ca, "current_liabilities": cl},
                    ensure_fact_evidence,
                )

        return ratio_evidence

    def _try_add_ratio(
        self,
        ratio_evidence: list[Evidence],
        ticker: str,
        ratio_name: str,
        fact_by_param: dict[str, XBRLFact],
        ensure_fact_evidence: Callable[[XBRLFact], Evidence],
    ) -> None:
        inputs = {param: fact.value for param, fact in fact_by_param.items()}
        try:
            result = RATIO_FUNCS[ratio_name](**inputs)
        except RatioInputError as exc:
            # One bad input (e.g. a zero denominator) skips this ratio,
            # not the whole run.
            logger.info(
                "financial_agent_ratio_skipped",
                extra={"ticker": ticker, "ratio": ratio_name, "reason": str(exc)},
            )
            return

        input_evidence = {
            param: ensure_fact_evidence(fact) for param, fact in fact_by_param.items()
        }
        anchor_fact = next(iter(fact_by_param.values()))
        ratio_evidence.append(
            Evidence(
                id=uuid4(),
                run_id=self._store.run_id,
                source_type="computed",
                source_ref=anchor_fact.accession_number,
                # A derived number can't have been published before the
                # last of its inputs existed.
                published_at=max(ev.published_at for ev in input_evidence.values()),
                retrieved_at=datetime.now(UTC),
                quote=f"{result.name} = {result.formula} = {result.value:.6f}",
                location={
                    "ratio_name": result.name,
                    "input_evidence_ids": {
                        param: str(ev.id) for param, ev in input_evidence.items()
                    },
                    "value": result.value,
                },
            )
        )

    async def _emit_claims(self, ticker: str) -> list[Claim]:
        evidence_context = "\n".join(_format_evidence_line(ev) for ev in self._store.all_evidence())
        system_prompt = load_prompt("financial_agent_v1").format(evidence_context=evidence_context)

        try:
            # WHY asyncio.to_thread: ClaudeClient.call_structured wraps the
            # synchronous anthropic.Anthropic client, not AsyncAnthropic.
            # Called directly, it would block the event loop for its full
            # network latency — invisible with one agent, but under
            # asyncio.gather (Phase 4's multiple concurrent agents) it
            # would silently serialize every other agent's Claude call
            # behind this one. See ADR-0019.
            batch = await asyncio.to_thread(
                self._claude.call_structured,
                agent=self.agent_name,
                model_cls=ClaimBatch,
                messages=[
                    {
                        "role": "user",
                        "content": f"Ticker: {ticker}. Emit claims for the evidence above.",
                    }
                ],
                system=system_prompt,
                model=self._settings.financial_agent_model,
                max_tokens=self._settings.financial_agent_max_tokens,
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
                "financial_agent_claims_dropped",
                extra={"ticker": ticker, "reason": str(exc.last_error)},
            )
            return []

        for claim in batch.claims:
            self._store.add_claim(claim)
        return batch.claims
