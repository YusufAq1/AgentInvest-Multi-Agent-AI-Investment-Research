"""Tests for the valuation input assembler and the pure valuation step.

A synthetic company, TICK, with every input chosen to be checkable by hand
(all money in USD; numbers small for readability):

  Fiscal years end Dec 31. Revenue FY2018..FY2023: 900, 950, 1000, 1100, 1210, 1331
  Operating income FY2021..2023: 220, 242, 266.2      → margin exactly 0.20 each year
  Capex 100 and D&A 60 in each of FY2021..2023        → Σ(capex − D&A) = 120
  Operating working capital (CA − cash − (CL)):
      FY2023: 500 − 100 − 300 = 100;  FY2020: 400 − 100 − 270 = 30  → ΔNWC = 70
  Revenue change FY2020 → FY2023 = 1331 − 1000 = 331
  → incremental investment rate = (120 + 70) / 331 = 190 / 331 = 0.574018...
  Shares 100 (2024-04-15), as-traded close 50 on 2024-06-28 → market cap 5,000
  Debt 400 LongTermDebt + 50 CommercialPaper (2024-03-31) = 450
  Cash 150 + 50 current marketable securities = 200     → net debt 250
  Interest expense FY2023 = 18 → cost of debt 18 / 450 = 0.04
  DGS10 4.50% → R_f 0.045.  Stock monthly returns = 1.5 × market → beta 1.5
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from backend.agents.valuation_compute import compute_valuation
from backend.agents.valuation_inputs import (
    ValuationInputAssembler,
    ValuationInputs,
    ValuationUnavailable,
    annual_series,
    latest_instant,
    prior_fiscal_year_ends,
)
from backend.calc.dcf import ImpliedGrowth, enterprise_value
from backend.data.macro import MacroClient
from backend.data.models import (
    MacroObservation,
    PriceBar,
    StockSplit,
    XBRLCompanyFacts,
    XBRLFact,
)
from backend.data.prices import PricesClient
from backend.data.xbrl import XBRLClient

from tests.conftest import make_settings

AS_OF = date(2024, 6, 30)
TICKER = "TICK"


def _annual(concept: str, fy: int, value: float, *, filed: date | None = None) -> XBRLFact:
    return XBRLFact(
        concept=concept,
        taxonomy="us-gaap",
        unit="USD",
        value=value,
        period_start=date(fy, 1, 1),
        period_end=date(fy, 12, 31),
        fiscal_year=fy,
        fiscal_period="FY",
        form="10-K",
        filed=filed or date(fy + 1, 2, 15),
        accession_number=f"acc-10k-{fy}",
    )


def _instant(
    concept: str,
    day: date,
    value: float,
    *,
    taxonomy: str = "us-gaap",
    unit: str = "USD",
    filed: date | None = None,
) -> XBRLFact:
    return XBRLFact(
        concept=concept,
        taxonomy=taxonomy,  # type: ignore[arg-type]
        unit=unit,
        value=value,
        period_start=None,
        period_end=day,
        fiscal_year=day.year,
        fiscal_period="Q",
        form="10-Q",
        filed=filed or day + timedelta(days=30),
        accession_number=f"acc-{concept}-{day.isoformat()}",
    )


def company_facts(
    *,
    revenues: dict[int, float] | None = None,
    include_debt: bool = True,
    include_interest: bool = True,
    shares_day: date = date(2024, 4, 15),
) -> list[XBRLFact]:
    revenues = revenues or {2018: 900, 2019: 950, 2020: 1000, 2021: 1100, 2022: 1210, 2023: 1331}
    facts = [
        _annual("RevenueFromContractWithCustomerExcludingAssessedTax", fy, v)
        for fy, v in revenues.items()
    ]
    for fy in (2021, 2022, 2023):
        facts.append(_annual("OperatingIncomeLoss", fy, revenues[fy] * 0.2))
        facts.append(_annual("PaymentsToAcquirePropertyPlantAndEquipment", fy, 100))
        facts.append(_annual("DepreciationDepletionAndAmortization", fy, 60))
    fy23, fy20 = date(2023, 12, 31), date(2020, 12, 31)
    facts += [
        _instant("AssetsCurrent", fy23, 500),
        _instant("CashAndCashEquivalentsAtCarryingValue", fy23, 100),
        _instant("LiabilitiesCurrent", fy23, 300),
        _instant("AssetsCurrent", fy20, 400),
        _instant("CashAndCashEquivalentsAtCarryingValue", fy20, 100),
        _instant("LiabilitiesCurrent", fy20, 270),
        _instant(
            "EntityCommonStockSharesOutstanding", shares_day, 100, taxonomy="dei", unit="shares"
        ),
        _instant("CashAndCashEquivalentsAtCarryingValue", date(2024, 3, 31), 150),
        _instant("MarketableSecuritiesCurrent", date(2024, 3, 31), 50),
    ]
    if include_debt:
        facts += [
            _instant("LongTermDebt", date(2024, 3, 31), 400),
            _instant("CommercialPaper", date(2024, 3, 31), 50),
        ]
    if include_interest:
        facts.append(_annual("InterestExpense", 2023, 18))
    return facts


def raw_from_facts(facts: list[XBRLFact]) -> dict[str, Any]:
    """A companyfacts-shaped raw payload containing exactly these facts, so
    xbrl_fact evidence can quote real raw entries (find_raw_entry)."""
    raw: dict[str, Any] = {"facts": {}}
    for fact in facts:
        entry: dict[str, Any] = {
            "end": fact.period_end.isoformat(),
            "val": fact.value,
            "accn": fact.accession_number,
            "fy": fact.fiscal_year,
            "fp": fact.fiscal_period,
            "form": fact.form,
            "filed": fact.filed.isoformat(),
        }
        if fact.period_start is not None:
            entry["start"] = fact.period_start.isoformat()
        units = (
            raw["facts"]
            .setdefault(fact.taxonomy, {})
            .setdefault(fact.concept, {"units": {}})["units"]
        )
        units.setdefault(fact.unit, []).append(entry)
    return raw


def monthly_bars(returns: list[float], start_price: float = 100.0) -> list[PriceBar]:
    """One bar per month-end, from 2019-05 through 2024-06."""
    bars, price, year, month = [], start_price, 2019, 5
    for r in [0.0, *returns]:
        price *= 1 + r
        day = date(year, month, 28)
        bars.append(PriceBar(date=day, open=price, high=price, low=price, close=price, volume=1))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return bars


_MARKET_RETURNS = [0.02, -0.01, 0.03, 0.0, 0.015, -0.02] * 10 + [0.01]  # 61 months


def assembler_for(
    facts: list[XBRLFact], *, splits: list[StockSplit] | None = None
) -> ValuationInputAssembler:
    xbrl = MagicMock(spec=XBRLClient)
    xbrl.get_company_facts_with_raw = AsyncMock(
        return_value=XBRLCompanyFacts(facts=facts, raw=raw_from_facts(facts))
    )

    async def history(symbol: str, as_of: date, **kwargs: Any) -> list[PriceBar]:
        if kwargs.get("adjustment") == "as_traded":
            return [PriceBar(date=date(2024, 6, 28), open=50, high=50, low=50, close=50, volume=1)]
        if symbol == "SPY":
            return monthly_bars(_MARKET_RETURNS)
        return monthly_bars([1.5 * r for r in _MARKET_RETURNS])

    prices = MagicMock(spec=PricesClient)
    prices.get_price_history = AsyncMock(side_effect=history)
    prices.get_splits = AsyncMock(return_value=splits or [])
    macro = MagicMock(spec=MacroClient)
    macro.get_series = AsyncMock(
        return_value=[MacroObservation(series_id="DGS10", date=date(2024, 6, 27), value=4.5)]
    )
    return ValuationInputAssembler(xbrl, prices, macro, make_settings())


async def assemble(facts: list[XBRLFact], **kwargs: Any) -> ValuationInputs:
    result = await assembler_for(facts, **kwargs).assemble(TICKER, AS_OF)
    assert isinstance(result, ValuationInputs), result
    return result


async def test_every_input_matches_its_hand_worked_value() -> None:
    inputs = await assemble(company_facts())

    assert inputs.fiscal_year_end == date(2023, 12, 31)
    assert inputs.base_revenue.value == 1331
    assert inputs.operating_margin.value == pytest.approx(0.20)
    assert inputs.incremental_investment_rate.value == pytest.approx(190 / 331)
    assert inputs.price.value == 50
    assert inputs.shares_outstanding.value == 100
    assert inputs.market_cap.value == 5000
    assert inputs.total_debt.value == 450
    assert inputs.cash.value == 200
    assert inputs.net_debt.value == 250
    assert inputs.cost_of_debt.value == pytest.approx(0.04)
    assert inputs.cost_of_debt.source == "computed"
    assert inputs.risk_free_rate.value == pytest.approx(0.045)
    assert inputs.beta.value == pytest.approx(1.5)
    assert not any(a.startswith("ASSUMED") for a in inputs.assumptions)


async def test_nothing_filed_after_as_of_is_used() -> None:
    """Leakage test: a restated FY2023 revenue filed AFTER as_of must be
    ignored, even though the (mocked) XBRL client handed it over."""
    facts = company_facts() + [
        _annual(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            2023,
            9999,
            filed=date(2024, 8, 1),
        )
    ]
    inputs = await assemble(facts)

    assert inputs.base_revenue.value == 1331
    cited = [
        fact
        for item in inputs.model_dump().values()
        if isinstance(item, dict) and "facts" in item
        for fact in item["facts"]
    ]
    assert cited and all(fact["filed"] <= AS_OF for fact in cited)
    assert inputs.price_bar.date <= AS_OF
    assert inputs.risk_free_observation.date <= AS_OF


async def test_missing_inputs_are_all_reported_together() -> None:
    """No debt tags (Ford's real situation) and a stale share count: both
    are listed in one result, and nothing is fabricated."""
    result = await assembler_for(
        company_facts(include_debt=False, shares_day=date(2011, 2, 14))
    ).assemble(TICKER, AS_OF)

    assert isinstance(result, ValuationUnavailable)
    assert any("long-term debt" in m for m in result.missing)
    assert any("shares outstanding" in m for m in result.missing)


async def test_shrinking_revenue_uses_a_labelled_assumed_investment_rate() -> None:
    shrinking: dict[int, float] = {
        2018: 1500,
        2019: 1450,
        2020: 1400,
        2021: 1300,
        2022: 1250,
        2023: 1200,
    }
    inputs = await assemble(company_facts(revenues=shrinking))

    assert inputs.incremental_investment_rate.source == "assumed"
    assert inputs.incremental_investment_rate.value == pytest.approx(0.25)
    assert any(a.startswith("ASSUMED incremental investment rate") for a in inputs.assumptions)


async def test_missing_interest_expense_uses_a_labelled_assumed_cost_of_debt() -> None:
    inputs = await assemble(company_facts(include_interest=False))

    assert inputs.cost_of_debt.source == "assumed"
    assert inputs.cost_of_debt.value == pytest.approx(0.045 + 0.015)
    assert any(a.startswith("ASSUMED cost of debt") for a in inputs.assumptions)


async def test_share_count_is_restated_for_a_split_before_the_price_date() -> None:
    """Shares reported 2024-04-15 (100), 2:1 split 2024-05-10, price date
    2024-06-28 → 200 shares on the price's basis (ADR-0021)."""
    inputs = await assemble(company_facts(), splits=[StockSplit(date=date(2024, 5, 10), ratio=2.0)])
    assert inputs.shares_outstanding.value == 200
    assert inputs.market_cap.value == 10_000
    assert inputs.shares_outstanding.note is not None
    assert "Multiplied by 2" in inputs.shares_outstanding.note


def test_annual_series_prefers_alias_priority_then_latest_filing() -> None:
    aliases = ("DepreciationDepletionAndAmortization", "Depreciation")
    facts = [
        _annual("Depreciation", 2023, 50),
        _annual("DepreciationDepletionAndAmortization", 2023, 60),
        _annual("DepreciationDepletionAndAmortization", 2022, 55),
        _annual("DepreciationDepletionAndAmortization", 2022, 57, filed=date(2024, 2, 15)),
    ]
    series = annual_series(facts, aliases)
    assert series[date(2023, 12, 31)].value == 60
    assert series[date(2022, 12, 31)].value == 57  # restatement filed later wins


def test_latest_instant_refuses_stale_facts() -> None:
    old = _instant("LongTermDebt", date(2022, 12, 31), 400)
    assert latest_instant([old], ("LongTermDebt",), as_of=AS_OF, max_age_days=400) is None
    assert latest_instant([old], ("LongTermDebt",), as_of=AS_OF, max_age_days=600) == old


def test_prior_fiscal_year_ends_leaves_gaps_as_none() -> None:
    series = annual_series([_annual("Revenues", fy, 1) for fy in (2023, 2022, 2020)], ("Revenues",))
    ends = prior_fiscal_year_ends(series, date(2023, 12, 31), 4)
    assert ends == [date(2023, 12, 31), date(2022, 12, 31), None, date(2020, 12, 31)]


async def test_compute_valuation_by_hand() -> None:
    """R_e = 0.045 + 1.5·0.05 = 0.12
    WACC = 5000/5450·0.12 + 450/5450·0.04·0.79 = 0.110092 + 0.002609 = 0.112701
    Target EV = 5,000 + 250 = 5,250
    3-year CAGR = (1331/1000)^(1/3) − 1 = 0.10 exactly (1.1³ = 1.331)."""
    inputs = await assemble(company_facts())
    result = compute_valuation(inputs, make_settings())

    assert result.cost_of_equity.value == pytest.approx(0.12)
    assert result.wacc.value == pytest.approx(5000 / 5450 * 0.12 + 450 / 5450 * 0.04 * 0.79)
    assert result.target_enterprise_value.value == 5250
    assert result.historical_revenue_cagr[3].value == pytest.approx(0.10)
    assert result.historical_revenue_cagr[5].value == pytest.approx((1331 / 900) ** 0.2 - 1)
    assert isinstance(result.implied_growth, ImpliedGrowth)
    assert enterprise_value(result.drivers, result.implied_growth.growth).enterprise_value == (
        pytest.approx(5250, rel=1e-6)
    )
    assert result.drivers.horizon_years == 10
    assert len(result.sensitivity.values) == 5
