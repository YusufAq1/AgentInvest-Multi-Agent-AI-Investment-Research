"""Phase 3 exit-criterion demo: index a real filing into a real Postgres
database, then run a real hybrid search query against it.

Run with:
    uv run alembic upgrade head   # once, before the first run
    uv run python scripts/rag_demo.py --ticker AAPL --as-of 2024-06-30 \\
        --query "customer concentration risk"

Needs your own EDGAR_IDENTITY and real POSTGRES_* connection values in
.env (a Supabase project's direct connection string, or a local
docker-compose Postgres — see .env.example and ADR-0013). Downloads the
real ~2GB BAAI/bge-m3 model on first run and writes to your real database
— same "I can't run this for you" situation as the earlier phases' demo
scripts (financial_agent_demo.py, data_layer_demo.py).
"""

import argparse
import asyncio
from datetime import date

from backend.core.config import Settings
from backend.core.logging import configure_logging
from backend.data.edgar import EdgarClient
from backend.data.models import DataUnavailable
from backend.db.session import make_engine, make_session_factory
from backend.rag.indexing import IndexingResult, index_filing
from backend.rag.retrieval import hybrid_search


async def main(ticker: str, as_of: date, query: str) -> None:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)

    edgar = EdgarClient(settings)
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    print(f"=== RAG demo: {ticker} as of {as_of.isoformat()} ===\n")

    filings = await edgar.get_filings(ticker, as_of, forms=("10-K",), limit=1)
    if isinstance(filings, DataUnavailable):
        print(f"No filings available: {filings.reason}")
        await engine.dispose()
        return
    filing = filings[0]
    print(f"Indexing {filing.form} {filing.accession_number} (filed {filing.filing_date}) ...")

    result = await index_filing(
        ticker=ticker,
        filing=filing,
        edgar=edgar,
        session_factory=session_factory,
        as_of=as_of,
        settings=settings,
    )
    if isinstance(result, DataUnavailable):
        print(f"Indexing failed: {result.reason}")
        await engine.dispose()
        return
    _print_indexing_result(result)

    print(f"\n--- Hybrid search: {query!r} ---")
    results = await hybrid_search(
        query=query,
        ticker=ticker,
        as_of=as_of,
        session_factory=session_factory,
        top_k=settings.rag_retrieval_top_k,
        candidate_k=settings.rag_retrieval_candidate_k,
        rrf_k=settings.rag_rrf_k,
    )
    for rank, chunk in enumerate(results, start=1):
        print(
            f"\n  #{rank} rrf_score={chunk.rrf_score:.5f} "
            f"dense_rank={chunk.dense_rank} fulltext_rank={chunk.fulltext_rank} "
            f"item={chunk.item} accession={chunk.accession}"
        )
        print(f"     {chunk.text[:200]}...")

    await engine.dispose()


def _print_indexing_result(result: IndexingResult) -> None:
    print(f"  documents_written={result.documents_written}")
    print(f"  chunks_written={result.chunks_written}")
    print(f"  chunks_skipped={result.chunks_skipped}")
    print(f"  sections_dropped={result.sections_dropped}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--as-of", default="2024-06-30", help="YYYY-MM-DD")
    parser.add_argument("--query", default="customer concentration risk")
    args = parser.parse_args()
    asyncio.run(main(args.ticker, date.fromisoformat(args.as_of), args.query))
