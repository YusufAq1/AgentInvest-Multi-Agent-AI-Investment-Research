"""Hybrid retrieval: pgvector HNSW (dense) + Postgres full-text (sparse
keyword), fused with Reciprocal Rank Fusion.

WHY two separate queries fused in Python, not one SQL-level UNION/CTE:
keeps the fusion math (`fuse_rankings`) directly unit-testable in
isolation, without a database — CLAUDE.md §7's Python-computes principle
extended from "the LLM never does arithmetic" to "test the ranking math
without needing a live index." See ADR-0014 for why hybrid search (rather
than pure embeddings) is the design at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.llm import AgentInvestError
from backend.db.models import Document, DocumentChunk
from backend.db.session import session_scope
from backend.rag.embeddings import embed_texts


class RetrievedChunk(BaseModel):
    """One hybrid-search result, with each leg's rank kept separate (not
    just a final blended score) — needed for eval transparency (the demo
    script and evaluation/retrieval_eval.py both show dense vs full-text
    contribution per result, not just the fused ranking).

    WHY this deliberately carries no `char_start`/`char_end`/`document_id`:
    it's a ranking/display projection, not a citation-grade one — a caller
    that needs to build a verbatim, offset-locatable Evidence row (Phase
    4's Filings Agent) calls `resolve_chunk_citation` for that, keeping
    this type's shape stable for the (cheaper, more common) case of just
    showing/ranking results.
    """

    chunk_id: UUID
    text: str
    accession: str
    item: str | None
    filing_date: date
    dense_rank: int | None
    fulltext_rank: int | None
    rrf_score: float


class ChunkCitation(BaseModel):
    """Everything needed to build a verbatim, offset-locatable `Evidence`
    row from a `hybrid_search` hit — the citation-grade projection
    `RetrievedChunk` deliberately isn't (see its docstring).
    """

    chunk_id: UUID
    document_id: UUID
    accession: str
    item: str | None
    filing_date: date
    fiscal_period: str | None
    char_start: int
    char_end: int
    text: str  # == chunk.text; a substring of Document.full_text by
    # chunking.py's construction (see its module docstring) — never
    # re-derived by slicing full_text here.


class ChunkNotFoundError(AgentInvestError):
    """`resolve_chunk_citation` was given a `chunk_id` that doesn't resolve
    to a real `document_chunks` row — should never happen for a chunk_id
    `hybrid_search` just returned in the same run; not silently swallowed
    if it somehow does (C6's spirit applied to a bug, not just missing
    external data)."""


async def hybrid_search(
    *,
    query: str,
    ticker: str,
    as_of: date,
    session_factory: async_sessionmaker[AsyncSession],
    top_k: int,
    candidate_k: int,
    rrf_k: int,
) -> list[RetrievedChunk]:
    """Runs a dense (pgvector cosine) leg and a full-text (`tsvector`) leg,
    each filtered to `ticker` and `filing_date <= as_of` — retrieval's own
    as_of enforcement (C3): a chunk from a filing dated after `as_of` must
    never surface here regardless of how similar it scores, even if it was
    indexed as part of a different, later `as_of` run. Fuses the two
    ranked id lists via Reciprocal Rank Fusion and returns the top `top_k`
    chunks with both legs' scoring context attached.
    """
    query_vector = (await embed_texts([query]))[0]

    async with session_scope(session_factory) as session:
        dense_stmt = _dense_query(query_vector, ticker, as_of, candidate_k)
        fulltext_stmt = _fulltext_query(query, ticker, as_of, candidate_k)

        dense_rows = (await session.execute(dense_stmt)).all()
        fulltext_rows = (await session.execute(fulltext_stmt)).all()

    dense_ids = [row.id for row in dense_rows]
    fulltext_ids = [row.id for row in fulltext_rows]
    fused = fuse_rankings(dense_ids, fulltext_ids, rrf_k)[:top_k]

    rows_by_id = {row.id: row for row in (*dense_rows, *fulltext_rows)}
    dense_rank_by_id = {chunk_id: rank for rank, chunk_id in enumerate(dense_ids, start=1)}
    fulltext_rank_by_id = {chunk_id: rank for rank, chunk_id in enumerate(fulltext_ids, start=1)}

    return [
        RetrievedChunk(
            chunk_id=chunk_id,
            text=rows_by_id[chunk_id].text,
            accession=rows_by_id[chunk_id].accession,
            item=rows_by_id[chunk_id].item,
            filing_date=rows_by_id[chunk_id].filing_date,
            dense_rank=dense_rank_by_id.get(chunk_id),
            fulltext_rank=fulltext_rank_by_id.get(chunk_id),
            rrf_score=score,
        )
        for chunk_id, score in fused
    ]


async def resolve_chunk_citation(
    chunk_id: UUID, *, session_factory: async_sessionmaker[AsyncSession]
) -> ChunkCitation:
    """Fetches the one `document_chunks` row `hybrid_search`'s
    `RetrievedChunk` didn't carry enough of to cite from directly (no
    `char_start`/`char_end`/`document_id`/`fiscal_period`). Raises
    `ChunkNotFoundError` if `chunk_id` doesn't resolve.
    """
    async with session_scope(session_factory) as session:
        row = await session.get(DocumentChunk, chunk_id)
    if row is None:
        raise ChunkNotFoundError(f"No document_chunks row for chunk_id={chunk_id}")
    return ChunkCitation(
        chunk_id=row.id,
        document_id=row.document_id,
        accession=row.accession,
        item=row.item,
        filing_date=row.filing_date,
        fiscal_period=row.fiscal_period,
        char_start=row.char_start,
        char_end=row.char_end,
        text=row.text,
    )


async def get_full_text_by_accession(
    accession: str, *, session_factory: async_sessionmaker[AsyncSession]
) -> str | None:
    """Fetches `Document.full_text` by accession — the one piece of I/O
    plumbing needed to build the `documents={accession: full_text}`
    mapping `backend.evidence.validation.validate_all_evidence` needs for
    its `sec_filing` containment check (`validation.py` itself stays
    DB-free by design — see its module docstring). Returns `None`, not an
    exception, if no `Document` row exists for this accession.
    """
    async with session_scope(session_factory) as session:
        full_text = await session.scalar(
            select(Document.full_text).where(Document.accession == accession)
        )
    return full_text


def _dense_query(
    query_vector: Sequence[float], ticker: str, as_of: date, candidate_k: int
) -> Select[tuple[UUID, str, str, str | None, date]]:
    return (
        select(
            DocumentChunk.id,
            DocumentChunk.text,
            DocumentChunk.accession,
            DocumentChunk.item,
            DocumentChunk.filing_date,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.ticker == ticker, Document.filing_date <= as_of)
        .order_by(DocumentChunk.embedding.cosine_distance(list(query_vector)))
        .limit(candidate_k)
    )


def _fulltext_query(
    query: str, ticker: str, as_of: date, candidate_k: int
) -> Select[tuple[UUID, str, str, str | None, date]]:
    tsquery = func.plainto_tsquery("english", query)
    return (
        select(
            DocumentChunk.id,
            DocumentChunk.text,
            DocumentChunk.accession,
            DocumentChunk.item,
            DocumentChunk.filing_date,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            Document.ticker == ticker,
            Document.filing_date <= as_of,
            DocumentChunk.tsv.op("@@")(tsquery),
        )
        .order_by(func.ts_rank(DocumentChunk.tsv, tsquery).desc())
        .limit(candidate_k)
    )


def fuse_rankings(
    dense_ids: Sequence[UUID], fulltext_ids: Sequence[UUID], rrf_k: int
) -> list[tuple[UUID, float]]:
    """Reciprocal Rank Fusion: score(d) = sum over each ranked list
    containing `d` of `1 / (rrf_k + rank)`, rank 1-indexed. A chunk
    appearing in only one list still gets a nonzero score from that list
    alone. Factored out as a standalone, DB-free function specifically so
    the fusion math is unit-testable against hand-computed expected
    orderings — see ADR-0014 for why RRF (rank-based) rather than a
    weighted blend of cosine distance and ts_rank (incompatible scales
    that would need ad hoc calibration).
    """
    scores: dict[UUID, float] = {}
    for ranked_list in (dense_ids, fulltext_ids):
        for rank, chunk_id in enumerate(ranked_list, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (rrf_k + rank)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)
