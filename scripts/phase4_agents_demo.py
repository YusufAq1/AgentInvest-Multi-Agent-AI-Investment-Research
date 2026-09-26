"""Phase 4 exit-criterion demo: run all four research agents (Financial,
Filings, News, Competitive) concurrently against one shared EvidenceStore,
for a real ticker, with real API calls.

Run with:
    uv run alembic upgrade head   # once, before the first run
    uv run python scripts/phase4_agents_demo.py --ticker AAPL --as-of 2024-06-30

Needs your own ANTHROPIC_API_KEY, EDGAR_IDENTITY, FRED_API_KEY, and
POSTGRES_* connection values in .env — same "I can't run this for you"
situation as every prior phase's demo script, since this makes real
Claude calls, real SEC/EDGAR requests, and real Postgres writes (Filings
Agent indexes a real filing). Also downloads BAAI/bge-m3 (~2GB, one-time)
if it isn't already cached from a Phase 3 run.

Prints wall-clock duration for the concurrent run — see ADR-0019 for why
that's the actual, visible proof "parallel execution works" beyond just
"didn't crash": before the asyncio.to_thread fix, four agents' Claude
calls would have executed one after another despite asyncio.gather,
making this number roughly the SUM of each agent's latency instead of
roughly the MAXIMUM.
"""

import argparse
import asyncio
import logging
import time
from datetime import date
from decimal import Decimal
from uuid import uuid4

from backend.agents.competitive import CompetitiveAgent
from backend.agents.filings import FilingsAgent
from backend.agents.financial import FinancialAgent
from backend.agents.news import NewsAgent
from backend.core.config import Settings
from backend.core.llm import ClaudeClient
from backend.core.logging import configure_logging
from backend.data.cache import Cache
from backend.data.cik import CikResolver
from backend.data.edgar import EdgarClient
from backend.data.http import SecHttpClient
from backend.data.news import NewsClient
from backend.data.xbrl import XBRLClient
from backend.db.session import make_engine, make_session_factory
from backend.evidence.models import Claim
from backend.evidence.store import EvidenceStore


class _CostCollector(logging.Handler):
    """Sums cost_usd off every 'llm_call' log line during this run — see
    financial_agent_demo.py's identical helper for why this reads logs
    rather than a return value (call_structured is deliberately generic
    and doesn't return cost as data)."""

    def __init__(self) -> None:
        super().__init__()
        self.total = Decimal("0")

    def emit(self, record: logging.LogRecord) -> None:
        cost = getattr(record, "cost_usd", None)
        if cost is not None:
            self.total += Decimal(cost)


def _print_claims(agent_name: str, claims: list[Claim]) -> None:
    print(f"\n--- {agent_name}: {len(claims)} claim(s) ---")
    for claim in claims:
        print(f"  [{claim.claim_type}/{claim.materiality}] {claim.statement}")


async def main(ticker: str, as_of: date) -> None:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)
    cost_collector = _CostCollector()
    logging.getLogger("agentinvest.llm").addHandler(cost_collector)

    cache = Cache(settings.data_cache_path)
    http = SecHttpClient(settings)
    cik = CikResolver(http, cache)
    xbrl = XBRLClient(http, cik, cache)
    news = NewsClient(http, cik, cache)
    edgar = EdgarClient(settings)
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    store = EvidenceStore(run_id=uuid4(), as_of=as_of)

    financial = FinancialAgent(xbrl, ClaudeClient(settings), store, settings)
    filings = FilingsAgent(edgar, session_factory, ClaudeClient(settings), store, settings)
    news_agent = NewsAgent(news, ClaudeClient(settings), store, settings)
    competitive = CompetitiveAgent(xbrl, ClaudeClient(settings), store, settings)

    print(f"=== Phase 4 agents demo: {ticker} as of {as_of.isoformat()} ===")
    print("Running Financial, Filings, News, and Competitive agents concurrently...\n")

    start = time.monotonic()
    financial_claims, filings_claims, news_claims, competitive_claims = await asyncio.gather(
        financial.run(ticker, as_of),
        filings.run(ticker, as_of),
        news_agent.run(ticker, as_of),
        competitive.run(ticker, as_of),
    )
    elapsed = time.monotonic() - start

    _print_claims("Financial", financial_claims)
    _print_claims("Filings", filings_claims)
    _print_claims("News", news_claims)
    _print_claims("Competitive", competitive_claims)

    dropped = store.dropped_claims()
    if dropped:
        print(f"\n--- Dropped claim batches: {len(dropped)} ---")
        for drop in dropped:
            print(f"  agent={drop.agent} reason={drop.reason}")

    await http.aclose()
    await engine.dispose()

    print("\n=== Run total ===")
    print(f"Wall-clock time for the concurrent run: {elapsed:.2f}s")
    print(f"Total Claude cost this run: ${cost_collector.total:.6f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--as-of", default="2024-06-30", help="YYYY-MM-DD")
    args = parser.parse_args()
    asyncio.run(main(args.ticker, date.fromisoformat(args.as_of)))
