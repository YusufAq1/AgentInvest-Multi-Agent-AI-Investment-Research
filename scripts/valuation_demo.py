"""Phase 6 exit criterion: compute the market-implied growth rate for a real
ticker as of a date, printing every input and assumption so the result can
be checked by hand (docs/valuation_worked_example.md does exactly that).

Run with:
    uv run python scripts/valuation_demo.py --ticker AAPL --as-of 2024-06-30

No Claude calls, so it costs nothing. It needs EDGAR_IDENTITY and
FRED_API_KEY in .env (SEC, FRED and yfinance are all free).
"""

import argparse
import asyncio
from datetime import date

from backend.agents.valuation_compute import compute_valuation
from backend.agents.valuation_inputs import (
    SourcedInput,
    ValuationInputAssembler,
    ValuationUnavailable,
)
from backend.calc.dcf import ImpliedGrowth, enterprise_value
from backend.core.config import Settings
from backend.core.disclaimer import DISCLAIMER
from backend.data.cache import Cache
from backend.data.cik import CikResolver
from backend.data.http import SecHttpClient
from backend.data.macro import MacroClient
from backend.data.prices import PricesClient
from backend.data.xbrl import XBRLClient


def _fmt(value: float) -> str:
    if abs(value) >= 1e6:
        return f"{value:,.0f}"
    if abs(value) < 1:
        return f"{value:.6f}"
    return f"{value:,.4f}"


def _print_input(item: SourcedInput) -> None:
    print(f"  {item.name:<28} {_fmt(item.value):>24}  [{item.source}]")
    for fact in item.facts:
        print(
            f"      {fact.taxonomy}:{fact.concept} = {fact.value:,.0f} "
            f"({fact.period_start or ''} to {fact.period_end}, {fact.form} "
            f"{fact.accession_number}, filed {fact.filed})"
        )
    if item.calculation is not None:
        print(f"      = {item.calculation.formula}")
    if item.note:
        print(f"      note: {item.note}")


async def main(ticker: str, as_of: date) -> None:
    settings = Settings()  # type: ignore[call-arg]
    cache = Cache(settings.data_cache_path)
    http = SecHttpClient(settings)
    macro = MacroClient(settings, cache)
    assembler = ValuationInputAssembler(
        XBRLClient(http, CikResolver(http, cache), cache), PricesClient(settings), macro, settings
    )
    try:
        inputs = await assembler.assemble(ticker, as_of)
    finally:
        await http.aclose()
        await macro.aclose()

    print(f"=== Reverse DCF: {ticker} as of {as_of.isoformat()} ===\n")
    if isinstance(inputs, ValuationUnavailable):
        print("Valuation UNAVAILABLE. Missing inputs:")
        for reason in inputs.missing:
            print(f"  - {reason}")
        print(f"\n{DISCLAIMER}")
        return

    result = compute_valuation(inputs, settings)
    print(f"Latest fiscal year end: {inputs.fiscal_year_end}\n")
    print("--- Inputs ---")
    for item in [
        *inputs.revenue_history,
        inputs.operating_margin,
        inputs.incremental_investment_rate,
        inputs.price,
        inputs.shares_outstanding,
        inputs.market_cap,
        inputs.total_debt,
        inputs.cash,
        inputs.net_debt,
        inputs.risk_free_rate,
        inputs.beta,
        inputs.cost_of_debt,
        inputs.tax_rate,
        inputs.equity_risk_premium,
    ]:
        _print_input(item)

    print("\n--- Cost of capital ---")
    for r in (result.cost_of_equity, result.wacc, result.target_enterprise_value):
        print(f"  {r.name:<26} {_fmt(r.value):>24}   = {r.formula}")

    d = result.drivers
    print("\n--- Value drivers ---")
    print(
        f"  revenue {d.base_revenue:,.0f} | margin {d.operating_margin:.4f} | tax {d.tax_rate} | "
        f"IIR {d.incremental_investment_rate:.4f} | horizon {d.horizon_years}y | "
        f"WACC {d.wacc:.4f}"
    )

    print("\n--- Implied growth ---")
    implied = result.implied_growth
    if isinstance(implied, ImpliedGrowth):
        print(
            f"  The market price implies {implied.growth:.2%} revenue growth a year for "
            f"{d.horizon_years} years."
        )
        print(
            f"  Growth {'creates' if implied.growth_creates_value else 'DESTROYS'} value on these "
            f"drivers (break-even investment rate {implied.value_neutral_investment_rate:.4f})."
        )
        check = enterprise_value(d, implied.growth)
        print(
            f"  Check: EV at that growth = {check.enterprise_value:,.0f} "
            f"vs target {implied.target_enterprise_value:,.0f}"
        )
    else:
        print(f"  NO implied growth: {implied.reason}")
    for years, cagr in sorted(result.historical_revenue_cagr.items()):
        print(f"  Historical {years}-year revenue CAGR: {cagr.value:.2%}")

    grid = result.sensitivity
    print("\n--- Sensitivity: value per share (rows: growth, columns: WACC) ---")
    print("  growth \\ WACC " + "".join(f"{w:>10.2%}" for w in grid.waccs))
    for g, row in zip(grid.growths, grid.values, strict=True):
        print(f"  {g:>13.0%} " + "".join(f"{v:>10.2f}" for v in row))
    print(f"  Market price: {inputs.price.value:.2f}")

    print("\n--- Assumptions ---")
    for assumption in inputs.assumptions:
        print(f"  - {assumption}")
    print(f"\n{DISCLAIMER}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--as-of", default="2024-06-30", help="YYYY-MM-DD")
    args = parser.parse_args()
    asyncio.run(main(args.ticker, date.fromisoformat(args.as_of)))
