"""Every calculation whose result can be stored as `computed` evidence, and
the rules for recomputing it from cited evidence.

WHY a registry: backend/evidence/validation.py recomputes each `computed`
evidence row from the rows it cites, and fails CI if the stored value
doesn't match (CLAUDE.md §6, §7). To do that it must know, per calculation
name:
  - which function to call
  - what kind of evidence its inputs may cite. Ratios (Phase 2) may cite
    only raw XBRL facts. Valuation numbers (Phase 6) may also cite market
    prices, FRED data and other computed rows, because WACC is built from
    cost of equity, which is built from beta.
  - which inputs may be CONSTANTS stored in the row itself rather than
    cited, which is only configured assumptions (ERP, tax rate, horizon,
    solver settings). Any other self-reported input is rejected, so a row
    can't fabricate its own inputs.

Every function here takes keyword floats and returns a RatioResult, so the
validator can call any of them the same way.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from backend.calc import dcf, wacc
from backend.calc.dcf import ImpliedGrowth, ValueDrivers
from backend.calc.errors import ValuationInputError
from backend.calc.ratios import RATIO_FUNCS, RatioResult

_XBRL_ONLY: Final = frozenset({"xbrl_fact"})
_VALUATION_INPUTS: Final = frozenset({"xbrl_fact", "price_series", "macro", "computed"})


@dataclass(frozen=True)
class ComputedSpec:
    func: Callable[..., RatioResult]
    allowed_input_types: frozenset[str]
    allowed_constants: frozenset[str] = frozenset()


# ---- Adapters: registry functions must take flat keyword floats ----------


def _average_operating_margin(**inputs: float) -> RatioResult:
    count = len([k for k in inputs if k.startswith("revenue_")])
    return dcf.average_operating_margin(
        operating_incomes=[inputs[f"operating_income_{i}"] for i in range(count)],
        revenues=[inputs[f"revenue_{i}"] for i in range(count)],
    )


def _drivers(inputs: dict[str, float]) -> ValueDrivers:
    return ValueDrivers(
        base_revenue=inputs["base_revenue"],
        operating_margin=inputs["operating_margin"],
        tax_rate=inputs["tax_rate"],
        incremental_investment_rate=inputs["incremental_investment_rate"],
        horizon_years=round(inputs["horizon_years"]),
        wacc=inputs["wacc"],
    )


_SOLVER_CONSTANTS: Final = frozenset(
    {"tax_rate", "horizon_years", "search_min", "search_max", "step", "tolerance", "flat_tolerance"}
)


def implied_revenue_growth(**inputs: float) -> RatioResult:
    """The reverse DCF's answer, as a recomputable number. Raises if no
    growth rate fits: a row claiming a growth rate the solver can't
    reproduce is invalid."""
    result = dcf.solve_implied_growth(
        _drivers(inputs),
        target_enterprise_value=inputs["target_enterprise_value"],
        search_min=inputs["search_min"],
        search_max=inputs["search_max"],
        step=inputs["step"],
        tolerance=inputs["tolerance"],
        flat_tolerance=inputs["flat_tolerance"],
    )
    if not isinstance(result, ImpliedGrowth):
        raise ValuationInputError(f"implied_revenue_growth: no solution ({result.reason})")
    return RatioResult(
        name="implied_revenue_growth",
        formula="g such that EV(value drivers, g) = target_enterprise_value",
        inputs=dict(inputs),
        value=result.growth,
    )


def implied_growth_solutions(**inputs: float) -> RatioResult:
    """1.0 if the solver finds an implied growth rate, 0.0 if it doesn't.
    WHY this exists: "the market price implies NO growth rate in range" is
    itself a finding the agent must be able to cite, and it has to be
    recomputable like any other."""
    result = dcf.solve_implied_growth(
        _drivers(inputs),
        target_enterprise_value=inputs["target_enterprise_value"],
        search_min=inputs["search_min"],
        search_max=inputs["search_max"],
        step=inputs["step"],
        tolerance=inputs["tolerance"],
        flat_tolerance=inputs["flat_tolerance"],
    )
    return RatioResult(
        name="implied_growth_solutions",
        formula="number of growth rates g in [search_min, search_max] with EV(g) = target",
        inputs=dict(inputs),
        value=1.0 if isinstance(result, ImpliedGrowth) else 0.0,
    )


def value_neutral_investment_rate(
    *, operating_margin: float, tax_rate: float, wacc: float
) -> RatioResult:
    """operating_margin * (1 - tax_rate) * (1 + wacc) / wacc. Below this,
    growth creates value; above it, growth destroys value (dcf.py)."""
    if wacc <= 0:
        raise ValuationInputError(f"value_neutral_investment_rate: wacc {wacc} must be > 0")
    return RatioResult(
        name="value_neutral_investment_rate",
        formula="operating_margin * (1 - tax_rate) * (1 + wacc) / wacc",
        inputs={"operating_margin": operating_margin, "tax_rate": tax_rate, "wacc": wacc},
        value=operating_margin * (1 - tax_rate) * (1 + wacc) / wacc,
    )


def assumed_cost_of_debt(*, risk_free_rate: float, credit_spread: float) -> RatioResult:
    """risk_free_rate + credit_spread: the labelled fallback when interest
    expense or debt is missing (6b)."""
    return RatioResult(
        name="assumed_cost_of_debt",
        formula="risk_free_rate + credit_spread",
        inputs={"risk_free_rate": risk_free_rate, "credit_spread": credit_spread},
        value=risk_free_rate + credit_spread,
    )


def assumed_incremental_investment_rate(*, value: float) -> RatioResult:
    """A configured placeholder, used when revenue didn't grow. It exists
    as a row so the implied growth that depends on it can cite it, and so
    the assumption is visible as evidence rather than hidden in a number."""
    return RatioResult(
        name="assumed_incremental_investment_rate",
        formula="configured placeholder (ASSUMED)",
        inputs={"value": value},
        value=value,
    )


COMPUTED_FUNCS: Final[dict[str, ComputedSpec]] = {
    **{name: ComputedSpec(func, _XBRL_ONLY) for name, func in RATIO_FUNCS.items()},
    # revenue_cagr is used by valuation with a configured year count.
    "revenue_cagr": ComputedSpec(RATIO_FUNCS["revenue_cagr"], _XBRL_ONLY, frozenset({"years"})),
    "average_operating_margin": ComputedSpec(_average_operating_margin, _XBRL_ONLY),
    "incremental_investment_rate": ComputedSpec(dcf.incremental_investment_rate, _XBRL_ONLY),
    "assumed_incremental_investment_rate": ComputedSpec(
        assumed_incremental_investment_rate, _VALUATION_INPUTS, frozenset({"value"})
    ),
    "market_capitalisation": ComputedSpec(dcf.market_capitalisation, _VALUATION_INPUTS),
    "net_debt": ComputedSpec(dcf.net_debt, _XBRL_ONLY),
    "target_enterprise_value": ComputedSpec(dcf.target_enterprise_value, _VALUATION_INPUTS),
    "implied_cost_of_debt": ComputedSpec(wacc.implied_cost_of_debt, _XBRL_ONLY),
    "assumed_cost_of_debt": ComputedSpec(
        assumed_cost_of_debt, _VALUATION_INPUTS, frozenset({"credit_spread"})
    ),
    "cost_of_equity": ComputedSpec(
        wacc.cost_of_equity, _VALUATION_INPUTS, frozenset({"equity_risk_premium"})
    ),
    "wacc": ComputedSpec(wacc.wacc, _VALUATION_INPUTS, frozenset({"tax_rate"})),
    "value_neutral_investment_rate": ComputedSpec(
        value_neutral_investment_rate, _VALUATION_INPUTS, frozenset({"tax_rate"})
    ),
    "implied_revenue_growth": ComputedSpec(
        implied_revenue_growth, _VALUATION_INPUTS, _SOLVER_CONSTANTS
    ),
    "implied_growth_solutions": ComputedSpec(
        implied_growth_solutions, _VALUATION_INPUTS, _SOLVER_CONSTANTS
    ),
}
