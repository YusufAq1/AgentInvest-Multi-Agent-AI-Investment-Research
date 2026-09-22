"""Tests for backend.rag.indexing.

Two tiers, matching this project's established split:

- as_of rejection / DataUnavailable propagation: fully mocked, no DB, fast.
- writes-real-rows / idempotency / uniqueness: need a real Postgres+
  pgvector (see tests/rag/conftest.py's `db_session`/`session_factory`
  fixtures) — these run against CI's service container, or a local
  Supabase/docker-compose Postgres if you point POSTGRES_* at one
  yourself. They are expected to fail with a connection error if no such
  database is reachable, which is informative, not a bug to silence.

`embed_texts` is always monkeypatched to a fixed-size fake vector — no
test in this file downloads the real ~2GB BAAI/bge-m3 model.
"""

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from backend.data.edgar import EdgarClient
from backend.data.models import DataUnavailable, Filing, FilingDocument, SectionMeta
from backend.db.models import Document, DocumentChunk
from backend.rag import indexing as indexing_module
from backend.rag.embeddings import EMBEDDING_DIM
from backend.rag.indexing import IndexingResult, index_filing
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import make_settings

TICKER = "AAPL"
AS_OF = date(2024, 6, 30)


def _filing(accession: str = "ACC-1", filing_date: date = date(2024, 2, 1)) -> Filing:
    return Filing(
        accession_number=accession, form="10-K", filing_date=filing_date, cik="0000320193"
    )


def _fake_edgar(document: FilingDocument | DataUnavailable) -> MagicMock:
    mock = MagicMock(spec=EdgarClient)
    mock.get_filing_document = AsyncMock(return_value=document)
    return mock


def _two_section_document() -> FilingDocument:
    return FilingDocument(
        sections=[
            SectionMeta(
                name="item_1",
                title="Item 1 - Business",
                item="1",
                part=None,
                confidence=0.95,
                detection_method="toc",
                text="Business overview body text describing operations.",
            ),
            SectionMeta(
                name="item_1a",
                title="Item 1A - Risk Factors",
                item="1A",
                part=None,
                confidence=0.95,
                detection_method="toc",
                text="Risk factors body text describing key risks.",
            ),
        ],
        period_of_report=date(2023, 12, 31),
    )


async def test_index_filing_rejects_stale_filing_against_new_as_of() -> None:
    filing = _filing(filing_date=date(2024, 8, 1))
    mock_edgar = _fake_edgar(
        DataUnavailable(
            source="edgar",
            identifier=filing.accession_number,
            as_of=AS_OF,
            reason="should not be reached",
            attempted_at=datetime.now(UTC),
        )
    )

    result = await index_filing(
        ticker=TICKER,
        filing=filing,
        edgar=mock_edgar,
        session_factory=MagicMock(),
        as_of=AS_OF,
        settings=make_settings(),
    )

    assert isinstance(result, DataUnavailable)
    mock_edgar.get_filing_document.assert_not_called()


async def test_index_filing_propagates_data_unavailable_from_edgar() -> None:
    filing = _filing()
    unavailable = DataUnavailable(
        source="edgar",
        identifier=filing.accession_number,
        as_of=AS_OF,
        reason="no parseable document",
        attempted_at=datetime.now(UTC),
    )
    mock_edgar = _fake_edgar(unavailable)

    result = await index_filing(
        ticker=TICKER,
        filing=filing,
        edgar=mock_edgar,
        session_factory=MagicMock(),
        as_of=AS_OF,
        settings=make_settings(),
    )

    assert result is unavailable


@pytest.mark.usefixtures("db_engine")
async def test_index_filing_writes_document_and_chunks(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    monkeypatch.setattr(
        indexing_module,
        "embed_texts",
        AsyncMock(return_value=[[0.0] * EMBEDDING_DIM, [0.0] * EMBEDDING_DIM]),
    )
    filing = _filing(accession="ACC-IDX-WRITE")
    mock_edgar = _fake_edgar(_two_section_document())

    result = await index_filing(
        ticker=TICKER,
        filing=filing,
        edgar=mock_edgar,
        session_factory=session_factory,
        as_of=AS_OF,
        settings=make_settings(),
    )

    assert isinstance(result, IndexingResult)
    assert result.documents_written == 1
    assert result.chunks_written == 2
    assert result.chunks_skipped == 0
    assert result.sections_dropped == 0

    stored_chunk_count = await db_session.scalar(
        select(func.count())
        .select_from(DocumentChunk)
        .where(DocumentChunk.accession == "ACC-IDX-WRITE")
    )
    assert stored_chunk_count == 2
    stored_document = await db_session.scalar(
        select(Document).where(Document.accession == "ACC-IDX-WRITE")
    )
    assert stored_document is not None
    assert stored_document.ticker == TICKER


@pytest.mark.usefixtures("db_engine")
async def test_index_filing_is_idempotent_on_rerun(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    fake_embed = AsyncMock(return_value=[[0.0] * EMBEDDING_DIM, [0.0] * EMBEDDING_DIM])
    monkeypatch.setattr(indexing_module, "embed_texts", fake_embed)
    filing = _filing(accession="ACC-IDX-IDEMPOTENT")
    mock_edgar = _fake_edgar(_two_section_document())

    first = await index_filing(
        ticker=TICKER,
        filing=filing,
        edgar=mock_edgar,
        session_factory=session_factory,
        as_of=AS_OF,
        settings=make_settings(),
    )
    second = await index_filing(
        ticker=TICKER,
        filing=filing,
        edgar=mock_edgar,
        session_factory=session_factory,
        as_of=AS_OF,
        settings=make_settings(),
    )

    assert isinstance(first, IndexingResult)
    assert first.chunks_written == 2
    assert isinstance(second, IndexingResult)
    assert second.chunks_written == 0
    assert second.chunks_skipped == 2
    # embed_texts must only have been called once (the first run) — the
    # whole point of the idempotency check is skipping the model on a
    # no-op re-run.
    assert fake_embed.await_count == 1

    stored_chunk_count = await db_session.scalar(
        select(func.count())
        .select_from(DocumentChunk)
        .where(DocumentChunk.accession == "ACC-IDX-IDEMPOTENT")
    )
    assert stored_chunk_count == 2
