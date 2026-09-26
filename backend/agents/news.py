"""The News Agent: classifies the materiality of SEC 8-K material-event
filings. CLAUDE.md §7 lists "materiality judgement on events" as an LLM
task, contrasted with deterministic date filtering/as_of enforcement.

WHY this agent's citation is a deterministic, Python-constructed string —
never the 8-K's actual narrative text: NewsClient (backend/data/news.py)
deliberately scoped itself in Phase 1 to structured item-code metadata
(accession, filing_date, item codes, item labels), not narrative body
text — RSS/GDELT (real narrative sourcing) are its own explicitly deferred
future work, not something this agent should reach around it to fetch via
EdgarClient instead. Extending EdgarClient's filing-document pipeline
(built and validated specifically against TenK/TenQ — see ADR-0015's whole
offset-trust saga) to CurrentReport (8-K) is real, unverified surface
area, not a one-line change. Item code + label + filing date is a real,
judgeable materiality signal on its own (a bankruptcy or cybersecurity
item code is inherently higher-materiality than a routine exhibit filing)
without needing narrative substance. See ADR-0017 for the full reasoning,
including the honest limitation this creates.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from uuid import uuid4

from backend.agents.prompt_loader import load_prompt
from backend.core.config import Settings
from backend.core.llm import ClaudeClient, StructuredOutputError
from backend.data.models import DataUnavailable, EightKEvent
from backend.data.news import NewsClient
from backend.evidence.models import Claim, ClaimBatch, Evidence
from backend.evidence.store import EvidenceStore

logger = logging.getLogger("agentinvest.agents.news")


def _event_quote(event: EightKEvent) -> str:
    """A deterministic, canonical string built from EightKEvent's own
    real fields — never freeform/paraphrased prose. This is BOTH the
    Evidence.quote AND (see _emit_claims) the "document" the citation
    -validity containment check runs against, since there's no separately
    -fetched narrative to check it against — see this module's docstring
    and ADR-0017 for why that makes the containment check tautological
    for this source_type, and why that's an accepted, documented tradeoff
    rather than a hidden one.
    """
    items = "; ".join(
        f"{code} ({label})" for code, label in zip(event.items, event.item_labels, strict=True)
    )
    return (
        f"8-K filed {event.filing_date.isoformat()} (accession {event.accession_number}): {items}"
    )


def _format_evidence_line(evidence: Evidence) -> str:
    return f"- id={evidence.id} type=news :: {evidence.quote}"


class NewsAgent:
    """Reads a ticker's recent 8-K filings and asks Claude to classify
    each material event's materiality."""

    def __init__(
        self, news: NewsClient, claude: ClaudeClient, store: EvidenceStore, settings: Settings
    ) -> None:
        self._news = news
        self._claude = claude
        self._store = store
        self._settings = settings

    @property
    def agent_name(self) -> str:
        return "news"

    async def run(self, ticker: str, as_of: date) -> list[Claim]:
        result = await self._news.get_8k_events(
            ticker, as_of, lookback_days=self._settings.news_agent_lookback_days
        )
        if isinstance(result, DataUnavailable):
            logger.info("news_agent_no_events", extra={"ticker": ticker, "reason": result.reason})
            return []

        # WHY no lazy-construction concern here (unlike the Financial
        # Agent's XBRL history): get_8k_events already returns a bounded,
        # as_of-filtered window — every event becomes exactly one Evidence
        # row, 1:1, with no analogous risk of turning years of history
        # into thousands of rows.
        for event in result:
            self._store.add_evidence(
                Evidence(
                    id=uuid4(),
                    run_id=self._store.run_id,
                    source_type="news",
                    source_ref=event.accession_number,
                    published_at=event.filing_date,
                    retrieved_at=datetime.now(UTC),
                    quote=_event_quote(event),
                    location={
                        "accession": event.accession_number,
                        "items": event.items,
                        "item_labels": event.item_labels,
                        "filing_date": event.filing_date.isoformat(),
                    },
                )
            )

        if not self._store.all_evidence(source_type="news"):
            return []

        return await self._emit_claims(ticker)

    async def _emit_claims(self, ticker: str) -> list[Claim]:
        evidence_context = "\n".join(
            _format_evidence_line(ev) for ev in self._store.all_evidence(source_type="news")
        )
        system_prompt = load_prompt("news_agent_v1").format(evidence_context=evidence_context)

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
                        "content": f"Ticker: {ticker}. Emit claims for the evidence above.",
                    }
                ],
                system=system_prompt,
                model=self._settings.news_agent_model,
                max_tokens=self._settings.news_agent_max_tokens,
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
                "news_agent_claims_dropped",
                extra={"ticker": ticker, "reason": str(exc.last_error)},
            )
            return []

        for claim in batch.claims:
            self._store.add_claim(claim)
        return batch.claims
