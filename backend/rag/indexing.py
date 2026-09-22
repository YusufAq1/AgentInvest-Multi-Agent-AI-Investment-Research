"""Indexes one filing into Postgres: fetch -> chunk -> embed -> write.

WHY idempotent on re-run (skip-if-unchanged, delete-then-reinsert if the
chunk count differs) rather than a blind insert: `index_filing` is meant to
be safely re-run (a demo script, a retry after a partial failure, a future
scheduled re-index) without duplicating rows or silently accumulating
stale chunks from an older version of the chunking logic. A stale partial
set is worse than a clean rebuild, so a mismatch always wins over trying
to patch it.

WHY one batched `embed_texts` call per filing, not one per chunk: matches
the Financial Agent's one-batch-call design (backend/agents/financial.py)
— fewer round trips, and for a local model, fewer separate encode() calls
means better batching inside sentence-transformers itself.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.config import Settings
from backend.data.edgar import EdgarClient
from backend.data.models import DataUnavailable, Filing
from backend.db.models import Document, DocumentChunk
from backend.db.session import session_scope
from backend.rag.chunking import build_full_text, chunk_filing_sections
from backend.rag.embeddings import embed_texts


class IndexingResult(BaseModel):
    accession: str
    documents_written: int
    chunks_written: int
    chunks_skipped: int
    sections_dropped: int


async def index_filing(
    *,
    ticker: str,
    filing: Filing,
    edgar: EdgarClient,
    session_factory: async_sessionmaker[AsyncSession],
    as_of: date,
    settings: Settings,
) -> IndexingResult | DataUnavailable:
    """Fetches `filing`'s document, chunks it, embeds the chunks, and
    writes `Document`/`DocumentChunk` rows.

    Re-checks `filing.filing_date <= as_of` itself (C3, applied identically
    to every other data-touching call site in this codebase) even though a
    caller likely already filtered filings by `as_of` upstream.
    """
    if filing.filing_date > as_of:
        return DataUnavailable(
            source="edgar",
            identifier=filing.accession_number,
            as_of=as_of,
            reason=(
                f"Filing {filing.accession_number} was filed {filing.filing_date}, "
                f"after as_of {as_of}"
            ),
            attempted_at=datetime.now(UTC),
        )

    document_result = await edgar.get_filing_document(filing, as_of)
    if isinstance(document_result, DataUnavailable):
        return document_result

    fiscal_period = (
        document_result.period_of_report.isoformat() if document_result.period_of_report else None
    )
    full_text = build_full_text(document_result.sections)
    chunks = chunk_filing_sections(
        sections=document_result.sections,
        accession=filing.accession_number,
        filing_date=filing.filing_date,
        fiscal_period=fiscal_period,
        max_tokens=settings.rag_chunk_max_tokens,
        overlap_tokens=settings.rag_chunk_overlap_tokens,
        confidence_floor=settings.rag_section_confidence_floor,
    )
    chunked_section_names = {chunk.section_name for chunk in chunks}
    sections_dropped = len(
        [s for s in document_result.sections if s.name not in chunked_section_names]
    )

    async with session_scope(session_factory) as session:
        existing_count = await session.scalar(
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.accession == filing.accession_number)
        )
        existing_count = existing_count or 0

        if existing_count and existing_count == len(chunks):
            return IndexingResult(
                accession=filing.accession_number,
                documents_written=0,
                chunks_written=0,
                chunks_skipped=len(chunks),
                sections_dropped=sections_dropped,
            )

        if existing_count:
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.accession == filing.accession_number)
            )

        embeddings = await embed_texts([chunk.text for chunk in chunks]) if chunks else []

        insert_document = (
            pg_insert(Document)
            .values(
                id=uuid4(),
                accession=filing.accession_number,
                ticker=ticker,
                form=filing.form,
                filing_date=filing.filing_date,
                full_text=full_text,
                retrieved_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=["accession"])
        )
        insert_result = await session.execute(insert_document)
        # WHY the ignore: SQLAlchemy's async Result[Any] stubs don't
        # statically expose `.rowcount`, even though the runtime object for
        # an INSERT statement (a CursorResult) genuinely has it.
        documents_written = insert_result.rowcount or 0  # type: ignore[attr-defined]

        document_id = await session.scalar(
            select(Document.id).where(Document.accession == filing.accession_number)
        )

        chunk_rows = [
            DocumentChunk(
                id=uuid4(),
                document_id=document_id,
                chunk_index=chunk.chunk_index,
                text=chunk.text,
                accession=chunk.accession,
                item=chunk.item,
                filing_date=chunk.filing_date,
                fiscal_period=chunk.fiscal_period,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                embedding=embedding,
            )
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        ]
        session.add_all(chunk_rows)

    return IndexingResult(
        accession=filing.accession_number,
        documents_written=documents_written,
        chunks_written=len(chunk_rows),
        chunks_skipped=0,
        sections_dropped=sections_dropped,
    )
