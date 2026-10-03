"""Hand-worked golden values for backend.calc.wacc."""

import pytest
from backend.calc.errors import ValuationInputError
from backend.calc.ratios import RatioInputError
from backend.calc.wacc import (
    after_tax_cost_of_debt,
    cost_of_equity,
    implied_cost_of_debt,
    wacc,
)


def test_cost_of_equity_capm_by_hand() -> None:
    """0.04 + 1.2 * 0.05 = 0.04 + 0.06 = 0.10"""
    result = cost_of_equity(risk_free_rate=0.04, beta=1.2, equity_risk_premium=0.05)
    assert result.value == pytest.approx(0.10)
    assert result.name == "cost_of_equity"


def test_after_tax_cost_of_debt_by_hand() -> None:
    """0.06 * (1 - 0.21) = 0.06 * 0.79 = 0.0474"""
    result = after_tax_cost_of_debt(cost_of_debt=0.06, tax_rate=0.21)
    assert result.value == pytest.approx(0.0474)


def test_wacc_by_hand() -> None:
    """E=800, D=200, so V=1000, E/V=0.8, D/V=0.2.
    0.8 * 0.10 + 0.2 * 0.05 * (1 - 0.25) = 0.08 + 0.0075 = 0.0875"""
    result = wacc(
        equity_value=800, debt_value=200, cost_of_equity=0.10, cost_of_debt=0.05, tax_rate=0.25
    )
    assert result.value == pytest.approx(0.0875)
    assert result.inputs["equity_value"] == 800


def test_wacc_with_no_debt_is_the_cost_of_equity() -> None:
    result = wacc(
        equity_value=500, debt_value=0, cost_of_equity=0.09, cost_of_debt=0.05, tax_rate=0.21
    )
    assert result.value == pytest.approx(0.09)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"equity_value": 0, "debt_value": 100},
        {"equity_value": -1, "debt_value": 100},
        {"equity_value": 100, "debt_value": -1},
    ],
)
def test_wacc_rejects_an_invalid_capital_base(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValuationInputError):
        wacc(cost_of_equity=0.1, cost_of_debt=0.05, tax_rate=0.21, **kwargs)


@pytest.mark.parametrize("tax_rate", [-0.1, 1.0, 1.5])
def test_tax_rate_outside_zero_to_one_is_rejected(tax_rate: float) -> None:
    with pytest.raises(ValuationInputError):
        after_tax_cost_of_debt(cost_of_debt=0.05, tax_rate=tax_rate)


def test_valuation_errors_are_ratio_input_errors() -> None:
    """Callers that already handle 'formula can't run on these inputs' for
    ratios handle valuation formulas too."""
    assert issubclass(ValuationInputError, RatioInputError)


def test_implied_cost_of_debt_by_hand() -> None:
    """3.9bn interest / 111bn debt = 0.035135..."""
    result = implied_cost_of_debt(interest_expense=3.9e9, total_debt=111e9)
    assert result.value == pytest.approx(3.9 / 111)
    with pytest.raises(ValuationInputError):
        implied_cost_of_debt(interest_expense=1.0, total_debt=0)
