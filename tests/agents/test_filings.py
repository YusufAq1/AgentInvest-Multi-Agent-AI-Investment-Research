"""Tests for backend.agents.filings.FilingsAgent.

No real network/DB calls: EdgarClient is mocked entirely, and
index_filing/hybrid_search/resolve_chunk_citation (backend.rag.*) are
patched as module-level AsyncMocks in backend.agents.filings' own
namespace — this agent orchestrates fetch->index->search->cite itself, so
its tests stay DB-free the same way tests/agents/test_financial.py stays
network-free, by mocking at the seam this agent calls through rather than
hitting a real Postgres (that seam is already covered directly by
tests/rag/test_retrieval.py and tests/rag/test_indexing.py).

ClaudeClient wraps a mocked Anthropic SDK client (matching
tests/test_llm.py's discipline) so the real forced-tool-use + retry
mechanics run for real against fake SDK responses.
"""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

from backend.agents.filings import FilingsAgent
from backend.core.llm import ClaudeClient
from backend.data.edgar import EdgarClient
from backend.data.models import DataUnavailable, Filing
from backend.evidence.store import EvidenceStore
from backend.rag.indexing import IndexingResult
from backend.rag.retrieval import ChunkCitation, RetrievedChunk

from tests.conftest import make_settings

AS_OF = date(2024, 6, 30)
TICKER = "AAPL"

_FILING = Filing(
    accession_number="0000320193-23-000106",
    form="10-K",
    filing_date=date(2023, 11, 3),
    cik="0000320193",
)


def _unavailable(source: str = "edgar") -> DataUnavailable:
    return DataUnavailable(
        source=source,  # type: ignore[arg-type]
        identifier=TICKER,
        as_of=AS_OF,
        reason="unavailable in test",
        attempted_at=datetime.now(UTC),
    )


def _indexing_result() -> IndexingResult:
    return IndexingResult(
        accession=_FILING.accession_number,
        documents_written=1,
        chunks_written=10,
        chunks_skipped=0,
        sections_dropped=0,
    )


def _retrieved_chunk(chunk_id: UUID, item: str = "1A") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        text="Risk factor excerpt text.",
        accession=_FILING.accession_number,
        item=item,
        filing_date=_FILING.filing_date,
        dense_rank=1,
        fulltext_rank=None,
        rrf_score=0.5,
    )


def _citation(chunk_id: UUID) -> ChunkCitation:
    return ChunkCitation(
        chunk_id=chunk_id,
        document_id=uuid4(),
        accession=_FILING.accession_number,
        item="1A",
        filing_date=_FILING.filing_date,
        fiscal_period=None,
        char_start=0,
        char_end=len("Risk factor excerpt text."),
        text="Risk factor excerpt text.",
    )


def _claim_batch_response(
    evidence_ids: list[str], *, tool_use_id: str = "toolu_1", claim_type: str = "evidence"
) -> SimpleNamespace:
    tool_input = {
        "claims": [
            {
                "id": str(uuid4()),
                "agent": "filings",
                "statement": "The company discloses a material risk factor.",
                "evidence_ids": evidence_ids,
                "claim_type": claim_type,
                "materiality": "high",
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


def _build_agent(mock_sdk_client: MagicMock) -> tuple[FilingsAgent, EvidenceStore, MagicMock]:
    mock_edgar = MagicMock(spec=EdgarClient)
    claude = ClaudeClient(make_settings(), client=mock_sdk_client)
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)
    agent = FilingsAgent(mock_edgar, MagicMock(), claude, store, make_settings())
    return agent, store, mock_edgar


async def test_run_returns_empty_when_no_filings() -> None:
    mock_sdk_client = MagicMock()
    agent, store, mock_edgar = _build_agent(mock_sdk_client)
    mock_edgar.get_filings = AsyncMock(return_value=_unavailable())

    claims = await agent.run(TICKER, AS_OF)

    assert claims == []
    mock_sdk_client.messages.create.assert_not_called()


@patch("backend.agents.filings.hybrid_search")
@patch("backend.agents.filings.index_filing")
async def test_run_returns_empty_when_indexing_fails_for_every_filing(
    mock_index_filing: AsyncMock, mock_hybrid_search: AsyncMock
) -> None:
    mock_sdk_client = MagicMock()
    agent, store, mock_edgar = _build_agent(mock_sdk_client)
    mock_edgar.get_filings = AsyncMock(return_value=[_FILING])
    mock_index_filing.return_value = _unavailable()

    claims = await agent.run(TICKER, AS_OF)

    assert claims == []
    mock_hybrid_search.assert_not_called()
    mock_sdk_client.messages.create.assert_not_called()


@patch("backend.agents.filings.resolve_chunk_citation")
@patch("backend.agents.filings.hybrid_search")
@patch("backend.agents.filings.index_filing")
@patch("backend.agents.filings.uuid4")
async def test_run_produces_claims_referencing_real_evidence(
    mock_uuid4: MagicMock,
    mock_index_filing: AsyncMock,
    mock_hybrid_search: AsyncMock,
    mock_resolve_chunk_citation: AsyncMock,
) -> None:
    fixed_evidence_id = uuid4()
    mock_uuid4.return_value = fixed_evidence_id
    chunk_id = uuid4()

    mock_sdk_client = MagicMock()
    agent, store, mock_edgar = _build_agent(mock_sdk_client)
    mock_edgar.get_filings = AsyncMock(return_value=[_FILING])
    mock_index_filing.return_value = _indexing_result()
    mock_hybrid_search.return_value = [_retrieved_chunk(chunk_id)]
    mock_resolve_chunk_citation.return_value = _citation(chunk_id)
    mock_sdk_client.messages.create.return_value = _claim_batch_response([str(fixed_evidence_id)])

    claims = await agent.run(TICKER, AS_OF)

    assert len(claims) == 1
    assert claims[0].evidence_ids == [fixed_evidence_id]
    assert store.claims() == claims
    # 5 default research questions, all resolving to the SAME chunk_id ->
    # exactly one resolve_chunk_citation call, not five (dedup regression).
    assert mock_resolve_chunk_citation.await_count == 1
    assert mock_hybrid_search.await_count == 5


@patch("backend.agents.filings.resolve_chunk_citation")
@patch("backend.agents.filings.hybrid_search")
@patch("backend.agents.filings.index_filing")
@patch("backend.agents.filings.uuid4")
async def test_run_dedupes_the_same_chunk_across_multiple_questions(
    mock_uuid4: MagicMock,
    mock_index_filing: AsyncMock,
    mock_hybrid_search: AsyncMock,
    mock_resolve_chunk_citation: AsyncMock,
) -> None:
    fixed_evidence_id = uuid4()
    mock_uuid4.return_value = fixed_evidence_id
    shared_chunk_id = uuid4()

    mock_sdk_client = MagicMock()
    agent, store, mock_edgar = _build_agent(mock_sdk_client)
    mock_edgar.get_filings = AsyncMock(return_value=[_FILING])
    mock_index_filing.return_value = _indexing_result()
    # Every question's hybrid_search call happens to return the SAME chunk —
    # a real scenario (one chunk answering several research questions).
    mock_hybrid_search.return_value = [_retrieved_chunk(shared_chunk_id)]
    mock_resolve_chunk_citation.return_value = _citation(shared_chunk_id)
    mock_sdk_client.messages.create.return_value = _claim_batch_response([str(fixed_evidence_id)])

    await agent.run(TICKER, AS_OF)

    assert len(store.all_evidence(source_type="sec_filing")) == 1


@patch("backend.agents.filings.resolve_chunk_citation")
@patch("backend.agents.filings.hybrid_search")
@patch("backend.agents.filings.index_filing")
@patch("backend.agents.filings.uuid4")
async def test_run_retries_once_when_claim_cites_unknown_evidence(
    mock_uuid4: MagicMock,
    mock_index_filing: AsyncMock,
    mock_hybrid_search: AsyncMock,
    mock_resolve_chunk_citation: AsyncMock,
) -> None:
    fixed_evidence_id = uuid4()
    mock_uuid4.return_value = fixed_evidence_id
    chunk_id = uuid4()
    hallucinated_id = str(uuid4())

    mock_sdk_client = MagicMock()
    agent, store, mock_edgar = _build_agent(mock_sdk_client)
    mock_edgar.get_filings = AsyncMock(return_value=[_FILING])
    mock_index_filing.return_value = _indexing_result()
    mock_hybrid_search.return_value = [_retrieved_chunk(chunk_id)]
    mock_resolve_chunk_citation.return_value = _citation(chunk_id)
    bad_response = _claim_batch_response([hallucinated_id], tool_use_id="toolu_bad")
    good_response = _claim_batch_response([str(fixed_evidence_id)], tool_use_id="toolu_good")
    mock_sdk_client.messages.create.side_effect = [bad_response, good_response]

    claims = await agent.run(TICKER, AS_OF)

    assert len(claims) == 1
    assert mock_sdk_client.messages.create.call_count == 2
    assert store.dropped_claims() == []


@patch("backend.agents.filings.resolve_chunk_citation")
@patch("backend.agents.filings.hybrid_search")
@patch("backend.agents.filings.index_filing")
@patch("backend.agents.filings.uuid4")
async def test_run_drops_claims_when_both_attempts_cite_unknown_evidence(
    mock_uuid4: MagicMock,
    mock_index_filing: AsyncMock,
    mock_hybrid_search: AsyncMock,
    mock_resolve_chunk_citation: AsyncMock,
) -> None:
    fixed_evidence_id = uuid4()
    mock_uuid4.return_value = fixed_evidence_id
    chunk_id = uuid4()

    mock_sdk_client = MagicMock()
    agent, store, mock_edgar = _build_agent(mock_sdk_client)
    mock_edgar.get_filings = AsyncMock(return_value=[_FILING])
    mock_index_filing.return_value = _indexing_result()
    mock_hybrid_search.return_value = [_retrieved_chunk(chunk_id)]
    mock_resolve_chunk_citation.return_value = _citation(chunk_id)
    mock_sdk_client.messages.create.side_effect = [
        _claim_batch_response([str(uuid4())], tool_use_id="toolu_bad1"),
        _claim_batch_response([str(uuid4())], tool_use_id="toolu_bad2"),
    ]

    claims = await agent.run(TICKER, AS_OF)

    assert claims == []
    dropped = store.dropped_claims()
    assert len(dropped) == 1
    assert dropped[0].agent == "filings"
