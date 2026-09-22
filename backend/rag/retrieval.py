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

from backend.db.models import Document, DocumentChunk
from backend.db.session import session_scope
from backend.rag.embeddings import embed_texts


class RetrievedChunk(BaseModel):
    """One hybrid-search result, with each leg's rank kept separate (not
    just a final blended score) — needed for eval transparency (the demo
    script and evaluation/retrieval_eval.py both show dense vs full-text
    contribution per result, not just the fused ranking)."""

    chunk_id: UUID
    text: str
    accession: str
    item: str | None
    filing_date: date
    dense_rank: int | None
    fulltext_rank: int | None
    rrf_score: float


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
