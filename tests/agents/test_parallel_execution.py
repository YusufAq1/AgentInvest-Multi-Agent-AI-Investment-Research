"""Proves Phase 4's second exit-criterion clause: parallel execution works.

Builds all four agents (Financial, Filings, News, Competitive) against ONE
shared EvidenceStore, with fully mocked dependencies (no real network/DB —
matching every other agent test's discipline), and runs them concurrently
via asyncio.gather. Asserts every agent's claims/evidence land in the
shared store correctly, with nothing lost, duplicated, or cross
-attributed to the wrong agent.

WHY this only demonstrates genuine event-loop overlap AFTER the
asyncio.to_thread retrofit (ADR-0019): before that fix,
ClaudeClient.call_structured was called synchronously inside each agent's
`async def run()`, which blocks the event loop for that call's full
duration — asyncio.gather would still run each agent's calls one after
another, not concurrently. Python's cooperative scheduling means the
CORRECTNESS assertions below (nothing lost/duplicated/cross-attributed)
would pass either way, since EvidenceStore's methods are all synchronous
between await points and never leave a partial mutation exposed across an
await boundary — but only the asyncio.to_thread fix makes the concurrency
genuinely overlap wall-clock time, which is what
scripts/phase4_agents_demo.py's timing print demonstrates for a real run.
"""

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

from backend.agents.competitive import CompetitiveAgent
from backend.agents.filings import FilingsAgent
from backend.agents.financial import FinancialAgent
from backend.agents.news import NewsAgent
from backend.core.llm import ClaudeClient
from backend.data.edgar import EdgarClient
from backend.data.models import EightKEvent, Filing, XBRLCompanyFacts, XBRLFact
from backend.data.news import NewsClient
from backend.data.xbrl import XBRLClient
from backend.evidence.store import EvidenceStore
from backend.rag.indexing import IndexingResult
from backend.rag.retrieval import ChunkCitation, RetrievedChunk

from tests.conftest import make_settings

AS_OF = date(2024, 6, 30)
TICKER = "TICK"
PEER = "PEER1"


def _xbrl_fact(concept: str, value: float, accession: str) -> XBRLFact:
    return XBRLFact(
        concept=concept,
        taxonomy="us-gaap",
        unit="USD",
        value=value,
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        fiscal_year=2023,
        fiscal_period="FY",
        form="10-K",
        filed=date(2024, 2, 1),
        accession_number=accession,
    )


def _company_facts(value: float, accession: str) -> XBRLCompanyFacts:
    entry = {
        "start": "2023-01-01",
        "end": "2023-12-31",
        "val": value,
        "accn": accession,
        "fy": 2023,
        "fp": "FY",
        "form": "10-K",
        "filed": "2024-02-01",
    }
    net_income_entry = {**entry, "val": value * 0.2}
    facts = [
        _xbrl_fact("Revenues", value, accession),
        _xbrl_fact("NetIncomeLoss", value * 0.2, accession),
    ]
    raw = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [entry]}},
                "NetIncomeLoss": {"units": {"USD": [net_income_entry]}},
            }
        }
    }
    return XBRLCompanyFacts(facts=facts, raw=raw)


def _make_claude_response(
    agent_name: str, evidence_id: UUID, *, tool_use_id: str
) -> SimpleNamespace:
    tool_input = {
        "claims": [
            {
                "id": str(uuid4()),
                "agent": agent_name,
                "statement": f"A claim from the {agent_name} agent.",
                "evidence_ids": [str(evidence_id)],
                "claim_type": "evidence",
                "materiality": "medium",
            }
        ]
    }
    content = [
        SimpleNamespace(type="tool_use", id=tool_use_id, name="emit_claims", input=tool_input)
    ]
    usage = SimpleNamespace(
        input_tokens=100, output_tokens=50, cache_creation_input_tokens=0, cache_read_input_tokens=0
    )
    return SimpleNamespace(content=content, usage=usage, stop_reason="tool_use")


def _claude_for(agent_name: str, store: EvidenceStore, source_ref_prefix: str) -> ClaudeClient:
    """Builds a ClaudeClient whose mocked SDK response dynamically cites
    whichever real evidence_id this agent actually generated in the
    shared store, identified by a distinct `source_ref` prefix each
    agent's mocked data is given below. WHY not a globally-pinned uuid4:
    that silently collides evidence ids across agents/companies sharing
    one EvidenceStore dict-by-id (the exact bug found and fixed in
    tests/agents/test_competitive.py's multi-company test).
    """
    mock_sdk_client = MagicMock()

    def _respond(*args: object, **kwargs: object) -> SimpleNamespace:
        matching = [
            ev for ev in store.all_evidence() if ev.source_ref.startswith(source_ref_prefix)
        ]
        real_evidence_id = matching[0].id
        return _make_claude_response(
            agent_name, real_evidence_id, tool_use_id=f"toolu-{agent_name}"
        )

    mock_sdk_client.messages.create.side_effect = _respond
    return ClaudeClient(make_settings(), client=mock_sdk_client)


@patch("backend.agents.filings.resolve_chunk_citation")
@patch("backend.agents.filings.hybrid_search")
@patch("backend.agents.filings.index_filing")
async def test_four_agents_run_concurrently_against_one_shared_store(
    mock_index_filing: AsyncMock,
    mock_hybrid_search: AsyncMock,
    mock_resolve_chunk_citation: AsyncMock,
) -> None:
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)
    settings = make_settings()

    # --- Financial Agent (evidence source_ref prefix: "acc-financial") ---
    mock_xbrl_financial = MagicMock(spec=XBRLClient)
    mock_xbrl_financial.get_company_facts_with_raw = AsyncMock(
        return_value=_company_facts(1_000_000, "acc-financial")
    )
    financial = FinancialAgent(
        mock_xbrl_financial, _claude_for("financial", store, "acc-financial"), store, settings
    )

    # --- Filings Agent (evidence source_ref prefix: "acc-filings") ---
    mock_edgar = MagicMock(spec=EdgarClient)
    mock_edgar.get_filings = AsyncMock(
        return_value=[
            Filing(
                accession_number="acc-filings",
                form="10-K",
                filing_date=date(2023, 11, 3),
                cik="0000000001",
            )
        ]
    )
    mock_index_filing.return_value = IndexingResult(
        accession="acc-filings",
        documents_written=1,
        chunks_written=1,
        chunks_skipped=0,
        sections_dropped=0,
    )
    chunk_id = uuid4()
    mock_hybrid_search.return_value = [
        RetrievedChunk(
            chunk_id=chunk_id,
            text="Risk factor excerpt.",
            accession="acc-filings",
            item="1A",
            filing_date=date(2023, 11, 3),
            dense_rank=1,
            fulltext_rank=None,
            rrf_score=0.5,
        )
    ]
    mock_resolve_chunk_citation.return_value = ChunkCitation(
        chunk_id=chunk_id,
        document_id=uuid4(),
        accession="acc-filings",
        item="1A",
        filing_date=date(2023, 11, 3),
        fiscal_period=None,
        char_start=0,
        char_end=len("Risk factor excerpt."),
        text="Risk factor excerpt.",
    )
    filings = FilingsAgent(
        mock_edgar, MagicMock(), _claude_for("filings", store, "acc-filings"), store, settings
    )

    # --- News Agent (evidence source_ref prefix: "acc-news") ---
    mock_news = MagicMock(spec=NewsClient)
    mock_news.get_8k_events = AsyncMock(
        return_value=[
            EightKEvent(
                accession_number="acc-news",
                filing_date=date(2024, 5, 2),
                items=["8.01"],
                item_labels=["Other Events"],
            )
        ]
    )
    news = NewsAgent(mock_news, _claude_for("news", store, "acc-news"), store, settings)

    # --- Competitive Agent (evidence source_ref prefix: "acc-competitive") ---
    mock_xbrl_competitive = MagicMock(spec=XBRLClient)

    async def _competitive_facts(
        company: str, as_of: date, *, concepts: object = None
    ) -> XBRLCompanyFacts:
        values = {TICKER: 2_000_000, PEER: 900_000}
        return _company_facts(values[company], f"acc-competitive-{company}")

    mock_xbrl_competitive.get_company_facts_with_raw = AsyncMock(side_effect=_competitive_facts)
    competitive = CompetitiveAgent(
        mock_xbrl_competitive,
        _claude_for("competitive", store, "acc-competitive"),
        store,
        settings,
        peer_map={TICKER: (PEER,)},
    )

    financial_claims, filings_claims, news_claims, competitive_claims = await asyncio.gather(
        financial.run(TICKER, AS_OF),
        filings.run(TICKER, AS_OF),
        news.run(TICKER, AS_OF),
        competitive.run(TICKER, AS_OF),
    )

    assert financial_claims and filings_claims and news_claims and competitive_claims

    all_claims = store.claims()
    assert len(all_claims) == (
        len(financial_claims) + len(filings_claims) + len(news_claims) + len(competitive_claims)
    )

    claims_by_agent = {c.agent: c for c in all_claims}
    assert set(claims_by_agent) == {"financial", "filings", "news", "competitive"}
    assert claims_by_agent["financial"] in financial_claims
    assert claims_by_agent["filings"] in filings_claims
    assert claims_by_agent["news"] in news_claims
    assert claims_by_agent["competitive"] in competitive_claims

    # Nothing cross-attributed: each claim's cited evidence resolves to a
    # real evidence row with the source_ref prefix that agent's own data
    # actually used — never another agent's or another company's.
    expected_prefix = {
        "financial": "acc-financial",
        "filings": "acc-filings",
        "news": "acc-news",
        "competitive": "acc-competitive",
    }
    for claim in all_claims:
        evidence = store.resolve(claim.evidence_ids)
        prefix = expected_prefix[claim.agent]
        assert all(ev.source_ref.startswith(prefix) for ev in evidence)
