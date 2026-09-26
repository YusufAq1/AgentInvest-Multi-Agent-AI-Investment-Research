"""The Filings Agent: the first real consumer of Phase 3's RAG pipeline
(backend/rag/). Orchestrates fetch -> index -> hybrid_search itself. Since Phase 5 the
research questions it searches for are supplied by the orchestration
layer's plan (see backend/orchestration/planning.py); indexing still
happens here, per run.

Same anti-fabrication design as every other agent: Python builds every
Evidence row (a verbatim chunk of stored filing text, at a real char
offset) before Claude is ever called. Claude can only cite evidence_ids
from the set it's shown — see backend/evidence/store.py's
`validate_claim_batch` for the enforcement.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.agents.prompt_loader import load_prompt
from backend.core.config import Settings
from backend.core.llm import ClaudeClient, StructuredOutputError
from backend.data.edgar import EdgarClient
from backend.data.models import DataUnavailable
from backend.evidence.models import Claim, ClaimBatch, Evidence
from backend.evidence.store import EvidenceStore
from backend.rag.indexing import index_filing
from backend.rag.retrieval import RetrievedChunk, hybrid_search, resolve_chunk_citation

logger = logging.getLogger("agentinvest.agents.filings")

# WHY a fixed, curated default: it's what the agent uses when nobody hands
# it ticker-specific questions. Since Phase 5 the orchestration layer passes
# questions in via the constructor (the deterministic plan passes exactly
# these; Increment 5b's LLM Research Manager writes its own). Public, not
# `_`-prefixed, because backend/orchestration/planning.py reads it too —
# same category of module-level, curated constant as
# backend/agents/xbrl_facts.py's CONCEPT_ALIASES.
DEFAULT_RESEARCH_QUESTIONS: Final[tuple[str, ...]] = (
    "What are the company's most significant risk factors?",
    "How does the company describe competition in its industry?",
    "What legal proceedings is the company currently involved in?",
    "What customer or supplier concentration risks does the company disclose?",
    "What does management say about recent business trends or outlook?",
)

# WHY only 10-Ks, only the single most recent one: matches
# scripts/rag_demo.py's exact, already-verified pattern. A documented
# scope-narrowing (no 10-Qs yet), not a silent one — see docs/adr and
# Settings.filings_agent_max_filings for the tunable count.
_FILING_FORMS: Final[tuple[str, ...]] = ("10-K",)


def _format_evidence_line(evidence: Evidence) -> str:
    loc = evidence.location
    return f"- id={evidence.id} type=sec_filing :: [Item {loc['item']}] {evidence.quote[:300]}"


class FilingsAgent:
    """Reads a company's most recent 10-K via hybrid search over its
    indexed chunks, and asks Claude which retrieved excerpts are
    claim-worthy."""

    def __init__(
        self,
        edgar: EdgarClient,
        session_factory: async_sessionmaker[AsyncSession],
        claude: ClaudeClient,
        store: EvidenceStore,
        settings: Settings,
        research_questions: Sequence[str] | None = None,
    ) -> None:
        """`research_questions`: the hybrid-search queries to run against
        the indexed filing. None means DEFAULT_RESEARCH_QUESTIONS. WHY a
        constructor argument rather than a new `run()` parameter: every
        agent satisfies the shared `ResearchAgent` Protocol's
        `run(ticker, as_of)`, and the orchestrator builds a fresh agent per
        run anyway, so per-run configuration belongs at construction.
        """
        self._edgar = edgar
        self._session_factory = session_factory
        self._claude = claude
        self._store = store
        self._settings = settings
        self._research_questions: tuple[str, ...] = tuple(
            research_questions if research_questions is not None else DEFAULT_RESEARCH_QUESTIONS
        )

    @property
    def agent_name(self) -> str:
        return "filings"

    async def run(self, ticker: str, as_of: date) -> list[Claim]:
        filings = await self._edgar.get_filings(
            ticker,
            as_of,
            forms=_FILING_FORMS,
            limit=self._settings.filings_agent_max_filings,
        )
        if isinstance(filings, DataUnavailable):
            logger.info(
                "filings_agent_no_filings", extra={"ticker": ticker, "reason": filings.reason}
            )
            return []

        indexed_any = False
        for filing in filings:
            result = await index_filing(
                ticker=ticker,
                filing=filing,
                edgar=self._edgar,
                session_factory=self._session_factory,
                as_of=as_of,
                settings=self._settings,
            )
            if isinstance(result, DataUnavailable):
                logger.info(
                    "filings_agent_index_unavailable",
                    extra={
                        "ticker": ticker,
                        "accession": filing.accession_number,
                        "reason": result.reason,
                    },
                )
            else:
                indexed_any = True

        if not indexed_any:
            # Nothing to search — every filing failed to index. Skip the
            # hybrid_search round trips entirely rather than running five
            # queries against an unchanged (or empty) index.
            return []

        # WHY deduplicated by chunk_id across questions, not one Evidence
        # row per (question, chunk) pair: the same chunk legitimately
        # answers more than one research question (e.g. a chunk about
        # customer concentration is also relevant to "risk factors").
        # Without dedup, one real chunk would become N near-duplicate
        # Evidence rows and inflate the prompt for no benefit — the same
        # "don't blow up evidence count" discipline as the Financial
        # Agent's lazy evidence construction, applied to a different
        # failure shape.
        unique_chunks: dict[UUID, RetrievedChunk] = {}
        for question in self._research_questions:
            hits = await hybrid_search(
                query=question,
                ticker=ticker,
                as_of=as_of,
                session_factory=self._session_factory,
                top_k=self._settings.rag_retrieval_top_k,
                candidate_k=self._settings.rag_retrieval_candidate_k,
                rrf_k=self._settings.rag_rrf_k,
            )
            for hit in hits:
                unique_chunks.setdefault(hit.chunk_id, hit)

        for chunk_id in unique_chunks:
            citation = await resolve_chunk_citation(chunk_id, session_factory=self._session_factory)
            self._store.add_evidence(
                Evidence(
                    id=uuid4(),
                    run_id=self._store.run_id,
                    source_type="sec_filing",
                    source_ref=citation.accession,
                    published_at=citation.filing_date,
                    retrieved_at=datetime.now(UTC),
                    quote=citation.text,
                    location={
                        "accession": citation.accession,
                        "item": citation.item,
                        "char_start": citation.char_start,
                        "char_end": citation.char_end,
                        "fiscal_period": citation.fiscal_period,
                    },
                )
            )

        if not self._store.all_evidence(source_type="sec_filing"):
            return []

        return await self._emit_claims(ticker)

    async def _emit_claims(self, ticker: str) -> list[Claim]:
        evidence_context = "\n".join(
            _format_evidence_line(ev) for ev in self._store.all_evidence(source_type="sec_filing")
        )
        system_prompt = load_prompt("filings_agent_v1").format(evidence_context=evidence_context)

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
                model=self._settings.filings_agent_model,
                max_tokens=self._settings.filings_agent_max_tokens,
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
                "filings_agent_claims_dropped",
                extra={"ticker": ticker, "reason": str(exc.last_error)},
            )
            return []

        for claim in batch.claims:
            self._store.add_claim(claim)
        return batch.claims
