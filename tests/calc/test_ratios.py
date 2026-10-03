"""Hand-worked golden values for backend.calc.ratios (CLAUDE.md §12's
"calculation accuracy" test)."""

import pytest
from backend.calc.ratios import (
    RATIO_FUNCS,
    RatioInputError,
    current_ratio,
    gross_margin,
    net_margin,
    revenue_cagr,
    yoy_revenue_growth,
)


def test_gross_margin_golden_value() -> None:
    result = gross_margin(revenue=1_000_000, cogs=600_000)
    assert result.value == pytest.approx(0.4)
    assert result.inputs == {"revenue": 1_000_000, "cogs": 600_000}


def test_gross_margin_zero_revenue_raises() -> None:
    with pytest.raises(RatioInputError):
        gross_margin(revenue=0, cogs=600_000)


def test_net_margin_golden_value() -> None:
    result = net_margin(revenue=1_000_000, net_income=150_000)
    assert result.value == pytest.approx(0.15)


def test_net_margin_zero_revenue_raises() -> None:
    with pytest.raises(RatioInputError):
        net_margin(revenue=0, net_income=150_000)


def test_current_ratio_golden_value() -> None:
    result = current_ratio(current_assets=500_000, current_liabilities=250_000)
    assert result.value == pytest.approx(2.0)


def test_current_ratio_zero_liabilities_raises() -> None:
    with pytest.raises(RatioInputError):
        current_ratio(current_assets=500_000, current_liabilities=0)


def test_yoy_revenue_growth_golden_value() -> None:
    result = yoy_revenue_growth(revenue_current=1_100_000, revenue_prior=1_000_000)
    assert result.value == pytest.approx(0.1)


def test_yoy_revenue_growth_zero_prior_raises() -> None:
    with pytest.raises(RatioInputError):
        yoy_revenue_growth(revenue_current=1_100_000, revenue_prior=0)


def test_ratio_funcs_dispatch_table_round_trips() -> None:
    # This is exactly what backend/evidence/validation.py's computed-evidence
    # recompute check exercises — asserting it directly here too.
    result = gross_margin(revenue=1_000_000, cogs=600_000)
    recomputed = RATIO_FUNCS[result.name](**result.inputs)
    assert recomputed.value == pytest.approx(result.value)


def test_revenue_cagr_by_hand() -> None:
    """100 → 121 over 2 years: (121/100)^(1/2) − 1 = 1.1 − 1 = 0.10"""
    result = revenue_cagr(revenue_start=100, revenue_end=121, years=2)
    assert result.value == pytest.approx(0.10)
    assert RATIO_FUNCS["revenue_cagr"] is revenue_cagr


@pytest.mark.parametrize(
    "kwargs",
    [
        {"revenue_start": 0, "revenue_end": 121, "years": 2},
        {"revenue_start": 100, "revenue_end": -5, "years": 2},
        {"revenue_start": 100, "revenue_end": 121, "years": 0},
    ],
)
def test_revenue_cagr_rejects_invalid_inputs(kwargs: dict[str, float]) -> None:
    with pytest.raises(RatioInputError):
        revenue_cagr(**kwargs)
