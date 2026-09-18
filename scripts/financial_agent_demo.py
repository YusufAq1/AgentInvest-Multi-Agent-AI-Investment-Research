"""Phase 2 exit criterion: the Financial Agent produces claims that all
pass validation, end to end, against real XBRL data and a real Claude call.

Run with: uv run python scripts/financial_agent_demo.py [--ticker AAPL] [--as-of 2024-06-30]

Mirrors hello_world.py / data_layer_demo.py's role for this phase. Needs
your own ANTHROPIC_API_KEY, EDGAR_IDENTITY, and FRED_API_KEY in .env — same
"I can't run this for you" situation as the earlier phases' demo scripts,
since this makes real (tiny) Claude calls.
"""

import argparse
import asyncio
import logging
from datetime import date
from decimal import Decimal
from uuid import uuid4

from backend.agents.financial import FinancialAgent
from backend.core.config import Settings
from backend.core.llm import ClaudeClient
from backend.core.logging import configure_logging
from backend.data.cache import Cache
from backend.data.cik import CikResolver
from backend.data.http import SecHttpClient
from backend.data.xbrl import XBRLClient
from backend.evidence.store import EvidenceStore


class _CostCollector(logging.Handler):
    """Sums cost_usd off every 'llm_call' log line during this run.

    WHY this exists instead of call_structured returning cost as data:
    that method is deliberately generic over any Pydantic model and
    returns just the parsed result (see backend/core/llm.py's docstring
    and ADR-0012's neighboring design notes) — cost is logged, not
    returned, for both the initial and retry call automatically. This
    demo script listens for those log lines to print a real total,
    without changing call_structured's contract for its actual callers.
    """

    def __init__(self) -> None:
        super().__init__()
        self.total = Decimal("0")

    def emit(self, record: logging.LogRecord) -> None:
        cost = getattr(record, "cost_usd", None)
        if cost is not None:
            self.total += Decimal(cost)


async def main(ticker: str, as_of: date) -> None:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)
    cost_collector = _CostCollector()
    logging.getLogger("agentinvest.llm").addHandler(cost_collector)

    cache = Cache(settings.data_cache_path)
    http = SecHttpClient(settings)
    cik = CikResolver(http, cache)
    xbrl = XBRLClient(http, cik, cache)
    claude = ClaudeClient(settings)
    store = EvidenceStore(run_id=uuid4(), as_of=as_of)
    agent = FinancialAgent(xbrl, claude, store, settings)

    print(f"=== Financial Agent demo: {ticker} as of {as_of.isoformat()} ===\n")

    claims = await agent.run(ticker, as_of)

    print(f"--- Evidence stored: {len(store.all_evidence())} ---")
    for evidence in store.all_evidence():
        print(f"  [{evidence.source_type}] id={evidence.id} source_ref={evidence.source_ref}")

    print(f"\n--- Claims produced: {len(claims)} ---")
    for claim in claims:
        print(f"  [{claim.claim_type}/{claim.materiality}] {claim.statement}")
        print(f"    evidence_ids: {[str(e) for e in claim.evidence_ids]}")

    dropped = store.dropped_claims()
    if dropped:
        print(f"\n--- Dropped claim batches: {len(dropped)} ---")
        for drop in dropped:
            print(f"  agent={drop.agent} reason={drop.reason}")

    await http.aclose()

    print("\n=== Run total ===")
    print(f"Total Claude cost this run: ${cost_collector.total:.6f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--as-of", default="2024-06-30", help="YYYY-MM-DD")
    args = parser.parse_args()
    asyncio.run(main(args.ticker, date.fromisoformat(args.as_of)))
