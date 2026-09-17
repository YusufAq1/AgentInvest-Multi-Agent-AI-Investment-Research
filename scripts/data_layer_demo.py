"""Phase 1 exit criterion: given (ticker, as_of), retrieve filings,
fundamentals, prices, and macro data through backend/data, and prove
nothing returned postdates as_of.

Run with: uv run python scripts/data_layer_demo.py [--ticker AAPL] [--as-of 2024-06-30]

Mirrors hello_world.py's role for Phase 0: a real, end-to-end proof that
the phase's exit criterion actually works, not just that its unit tests
pass. Needs your own EDGAR_IDENTITY and FRED_API_KEY in .env — same
"I can't run this for you" situation as Phase 0's real Claude call.
"""

import argparse
import asyncio
from datetime import date

from backend.core.config import Settings
from backend.core.logging import configure_logging
from backend.data.cache import Cache
from backend.data.cik import CikResolver
from backend.data.edgar import EdgarClient
from backend.data.http import SecHttpClient
from backend.data.macro import MacroClient
from backend.data.models import DataUnavailable
from backend.data.news import NewsClient
from backend.data.prices import PricesClient
from backend.data.xbrl import XBRLClient

# Collects every date-bearing field returned across all sources, so the
# final self-check can assert none of them postdates as_of in one place —
# the data-layer equivalent of CLAUDE.md §12's look-ahead-leakage test,
# run here against real data instead of fixtures.
_seen_dates: list[tuple[str, date]] = []


def _report(label: str, result: object, as_of: date) -> None:
    print(f"--- {label} ---")
    if isinstance(result, DataUnavailable):
        print(f"  DataUnavailable: {result.reason}")
        return
    assert isinstance(result, list)
    print(f"  {len(result)} item(s) retrieved")
    for item in result[:3]:
        print(f"    {item}")
    if len(result) > 3:
        print(f"    ... and {len(result) - 3} more")

    for item in result:
        for field_name in ("filed", "filing_date", "date"):
            value = getattr(item, field_name, None)
            if isinstance(value, date):
                _seen_dates.append((f"{label}.{field_name}", value))


async def main(ticker: str, as_of: date) -> None:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)

    cache = Cache(settings.data_cache_path)
    http = SecHttpClient(settings)
    cik = CikResolver(http, cache)
    edgar = EdgarClient(settings)
    xbrl = XBRLClient(http, cik, cache)
    prices = PricesClient(settings)
    macro = MacroClient(settings, cache)
    news = NewsClient(http, cik, cache)

    print(f"=== AgentInvest data layer demo: {ticker} as of {as_of.isoformat()} ===\n")

    _report("Filings (10-K/10-Q)", await edgar.get_filings(ticker, as_of), as_of)
    _report("XBRL fundamentals", await xbrl.get_company_facts(ticker, as_of), as_of)
    _report("Price history", await prices.get_price_history(ticker, as_of), as_of)
    _report("Risk-free rate (FRED)", await macro.get_series("risk_free_rate", as_of), as_of)
    _report("8-K events (bonus)", await news.get_8k_events(ticker, as_of), as_of)

    await http.aclose()
    await macro.aclose()

    print(f"\n=== Self-check: nothing postdates {as_of.isoformat()} ===")
    violations = [(label, d) for label, d in _seen_dates if d > as_of]
    if violations:
        for label, d in violations:
            print(f"  LEAKAGE: {label} = {d.isoformat()}")
        raise SystemExit(1)
    print(f"  OK — checked {len(_seen_dates)} date field(s), none after as_of.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--as-of", default="2024-06-30", help="YYYY-MM-DD")
    args = parser.parse_args()
    asyncio.run(main(args.ticker, date.fromisoformat(args.as_of)))
