"""Tests for backend.rag.retrieval.hybrid_search against a real Postgres+
pgvector (see tests/rag/conftest.py) — pure ranking math is covered
DB-free in test_retrieval_fusion.py instead.

`embed_texts` is monkeypatched to return controlled, hand-picked vectors
(not the real BGE-M3 model) so the dense leg's ordering is deterministic
and known ahead of time — these tests prove the SQL/fusion *mechanics*
work correctly, not that BGE-M3's real semantics are good (that's what
evaluation/retrieval_eval.py's recall@5 measures against real filings).
"""

from collections.abc import Callable
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from backend.db.models import Document, DocumentChunk
from backend.rag import retrieval as retrieval_module
from backend.rag.embeddings import EMBEDDING_DIM
from backend.rag.retrieval import hybrid_search
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TICKER = "AAPL"
AS_OF = date(2024, 6, 30)


def _unit_vector(index: int) -> list[float]:
    """A basis vector along dimension `index` — cosine distance to another
    basis vector along a different dimension is maximal (orthogonal),
    while distance to itself is zero. Gives fully deterministic, hand-
    reasoned dense-leg ordering without needing the real model.
    """
    vector = [0.0] * EMBEDDING_DIM
    vector[index] = 1.0
    return vector


def _near_vector(primary: int, secondary: int) -> list[float]:
    """A vector mostly aligned with `primary` but not identical to it —
    genuinely closer (lower cosine distance) to `_unit_vector(primary)`
    than any vector orthogonal to it, but not a perfect match either.
    WHY this exists alongside `_unit_vector`: with only a handful of rows
    seeded per test, two *orthogonal* vectors are equally (maximally)
    distant from a query and tie arbitrarily for dense rank — this gives
    a genuinely, deterministically closer alternative to rank against.
    """
    vector = [0.0] * EMBEDDING_DIM
    vector[primary] = 0.9
    vector[secondary] = 0.1
    return vector


async def _seed_chunk(
    db_session: AsyncSession,
    *,
    accession: str,
    text: str,
    embedding: list[float],
    filing_date: date = date(2024, 2, 1),
    ticker: str = TICKER,
) -> UUID:
    document = Document(
        id=uuid4(),
        accession=accession,
        ticker=ticker,
        form="10-K",
        filing_date=filing_date,
        full_text=text,
        retrieved_at=datetime.now(UTC),
    )
    chunk_id = uuid4()
    chunk = DocumentChunk(
        id=chunk_id,
        document_id=document.id,
        chunk_index=0,
        text=text,
        accession=accession,
        item="1A",
        filing_date=filing_date,
        fiscal_period=None,
        char_start=0,
        char_end=len(text),
        embedding=embedding,
    )
    db_session.add_all([document, chunk])
    # WHY commit(), not flush(): this writes directly via the shared
    # test session (bypassing session_scope), so it autobegins its own
    # transaction. hybrid_search() calls session_scope(), which calls
    # session.begin() explicitly — that raises "a transaction is already
    # begun" if this seed's transaction is still open. commit() ends it
    # cleanly (the outer, connection-level transaction from db_session's
    # own fixture still rolls everything back at test teardown).
    await db_session.commit()
    return chunk_id


_PatchQueryEmbedding = Callable[[list[float]], None]


@pytest.fixture
def _patched_query_embedding(monkeypatch: pytest.MonkeyPatch) -> _PatchQueryEmbedding:
    def _set(vector: list[float]) -> None:
        monkeypatch.setattr(retrieval_module, "embed_texts", AsyncMock(return_value=[vector]))

    return _set


@pytest.mark.usefixtures("db_engine")
async def test_hybrid_search_finds_exact_keyword_match_via_fulltext_leg(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    _patched_query_embedding: _PatchQueryEmbedding,
) -> None:
    # The keyword-matching chunk's embedding is orthogonal to the query
    # (the worst possible dense-leg position), but it contains the exact
    # phrase the query full-text-matches. The distractor's embedding is
    # deliberately made genuinely CLOSER to the query than the keyword
    # chunk's (see _near_vector) so the dense leg has a real, non-tied
    # reason to prefer it — proving the keyword chunk only wins overall
    # because of the full-text leg, not an accident of vector tie-breaking.
    keyword_chunk_id = await _seed_chunk(
        db_session,
        accession="ACC-KEYWORD",
        text="Customer concentration risk: one customer represents 20% of revenue.",
        embedding=_unit_vector(0),
    )
    distractor_chunk_id = await _seed_chunk(
        db_session,
        accession="ACC-UNRELATED",
        text="Unrelated discussion of manufacturing facilities and capacity.",
        embedding=_near_vector(2, 3),
    )
    _patched_query_embedding(_unit_vector(2))

    results = await hybrid_search(
        query="customer concentration risk",
        ticker=TICKER,
        as_of=AS_OF,
        session_factory=session_factory,
        top_k=5,
        candidate_k=20,
        rrf_k=60,
    )

    assert any(r.chunk_id == keyword_chunk_id for r in results)
    hit = next(r for r in results if r.chunk_id == keyword_chunk_id)
    assert hit.fulltext_rank == 1
    # Genuinely outranked on the dense leg by the distractor (rank 2, not
    # tied for 1st) — it still surfaces overall because of full-text.
    assert hit.dense_rank == 2

    distractor_hit = next(r for r in results if r.chunk_id == distractor_chunk_id)
    assert distractor_hit.dense_rank == 1
    # No shared vocabulary with the query at all — a real non-match on
    # the tsvector `@@` operator, not just a low rank.
    assert distractor_hit.fulltext_rank is None


@pytest.mark.usefixtures("db_engine")
async def test_hybrid_search_finds_semantic_match_via_dense_leg(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    _patched_query_embedding: _PatchQueryEmbedding,
) -> None:
    # No shared keywords with the query at all, but its embedding is
    # identical to the query's — only the dense leg can find it.
    semantic_chunk_id = await _seed_chunk(
        db_session,
        accession="ACC-SEMANTIC",
        text="Demand from a single large buyer accounts for a fifth of sales.",
        embedding=_unit_vector(5),
    )
    _patched_query_embedding(_unit_vector(5))

    results = await hybrid_search(
        query="words that share nothing lexically with the seeded chunk",
        ticker=TICKER,
        as_of=AS_OF,
        session_factory=session_factory,
        top_k=5,
        candidate_k=20,
        rrf_k=60,
    )

    assert any(r.chunk_id == semantic_chunk_id for r in results)
    hit = next(r for r in results if r.chunk_id == semantic_chunk_id)
    assert hit.dense_rank == 1
    assert hit.fulltext_rank is None


@pytest.mark.usefixtures("db_engine")
async def test_hybrid_search_never_returns_chunks_from_filings_after_as_of(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    _patched_query_embedding: _PatchQueryEmbedding,
) -> None:
    # Seeded with an embedding IDENTICAL to the query and text that
    # matches the query's own keywords exactly — the strongest possible
    # candidate on both legs — but its filing is dated after as_of. It
    # must never surface regardless of how well it scores (C3 / CLAUDE.md
    # §12's leakage test, applied to the RAG layer).
    await _seed_chunk(
        db_session,
        accession="ACC-POST-AS-OF",
        text="post as of filing content post as of filing content",
        embedding=_unit_vector(9),
        filing_date=date(2024, 8, 1),  # after AS_OF = 2024-06-30
    )
    _patched_query_embedding(_unit_vector(9))

    results = await hybrid_search(
        query="post as of filing content",
        ticker=TICKER,
        as_of=AS_OF,
        session_factory=session_factory,
        top_k=5,
        candidate_k=20,
        rrf_k=60,
    )

    assert all(r.accession != "ACC-POST-AS-OF" for r in results)
