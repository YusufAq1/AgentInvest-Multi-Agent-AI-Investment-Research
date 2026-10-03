"""From assembled inputs to a valuation: pure, no I/O, no LLM.

    cost_of_equity = R_f + β·ERP
    WACC           = (E/V)·Rₑ + (D/V)·R_d·(1 − t)     E = market cap, D = book debt
    target EV      = market cap + net debt
    implied growth = the g at which EV(g) on the value drivers = target EV
    + a growth × WACC sensitivity grid (value per share)
    + historical revenue CAGRs to judge the implied growth against

Separate from valuation_inputs.py so the arithmetic can be tested with
hand-built inputs, and so the Valuation Agent (6c) and the demo script run
exactly the same computation.
"""

from __future__ import annotations

from pydantic import BaseModel

from backend.agents.valuation_inputs import ValuationInputs
from backend.calc.dcf import (
    ImpliedGrowth,
    NoImpliedGrowth,
    SensitivityGrid,
    ValueDrivers,
    sensitivity_grid,
    solve_implied_growth,
    target_enterprise_value,
)
from backend.calc.ratios import RatioResult, revenue_cagr
from backend.calc.wacc import cost_of_equity, wacc
from backend.core.config import Settings


class ValuationResult(BaseModel):
    inputs: ValuationInputs
    cost_of_equity: RatioResult
    wacc: RatioResult
    target_enterprise_value: RatioResult
    drivers: ValueDrivers
    implied_growth: ImpliedGrowth | NoImpliedGrowth
    sensitivity: SensitivityGrid
    # Keyed by number of years (3, 5). Absent when that much history
    # isn't available.
    historical_revenue_cagr: dict[int, RatioResult]


def compute_valuation(inputs: ValuationInputs, settings: Settings) -> ValuationResult:
    re = cost_of_equity(
        risk_free_rate=inputs.risk_free_rate.value,
        beta=inputs.beta.value,
        equity_risk_premium=inputs.equity_risk_premium.value,
    )
    w = wacc(
        equity_value=inputs.market_cap.value,
        debt_value=inputs.total_debt.value,
        cost_of_equity=re.value,
        cost_of_debt=inputs.cost_of_debt.value,
        tax_rate=inputs.tax_rate.value,
    )
    target = target_enterprise_value(
        market_cap=inputs.market_cap.value, net_debt=inputs.net_debt.value
    )
    drivers = ValueDrivers(
        base_revenue=inputs.base_revenue.value,
        operating_margin=inputs.operating_margin.value,
        tax_rate=inputs.tax_rate.value,
        incremental_investment_rate=inputs.incremental_investment_rate.value,
        horizon_years=settings.valuation_horizon_years,
        wacc=w.value,
    )
    implied = solve_implied_growth(
        drivers,
        target_enterprise_value=target.value,
        search_min=settings.valuation_growth_search_min,
        search_max=settings.valuation_growth_search_max,
        step=settings.valuation_growth_search_step,
        tolerance=settings.valuation_growth_tolerance,
        flat_tolerance=settings.valuation_flat_tolerance,
    )
    # WHY skip non-positive WACC offsets: a WACC at or below zero has no
    # meaning, so those columns are dropped rather than shown as nonsense.
    grid_waccs = [
        w.value + offset for offset in settings.valuation_grid_wacc_offsets if w.value + offset > 0
    ]
    grid = sensitivity_grid(
        drivers,
        growths=settings.valuation_grid_growths,
        waccs=grid_waccs,
        net_debt=inputs.net_debt.value,
        shares_outstanding=inputs.shares_outstanding.value,
    )
    history = inputs.revenue_history
    cagrs: dict[int, RatioResult] = {}
    for years in (3, 5):
        if len(history) > years and history[years].name == f"revenue_fy-{years}":
            cagrs[years] = revenue_cagr(
                revenue_start=history[years].value,
                revenue_end=history[0].value,
                years=years,
            )
    return ValuationResult(
        inputs=inputs,
        cost_of_equity=re,
        wacc=w,
        target_enterprise_value=target,
        drivers=drivers,
        implied_growth=implied,
        sensitivity=grid,
        historical_revenue_cagr=cagrs,
    )
