"""Hand-worked golden values for backend.calc.dcf (the reverse DCF).

The reference drivers used throughout, chosen so every line works out by
hand:
    Rev_0 = 100, margin = 0.20, tax = 0.25  →  after-tax margin c = 0.15
    WACC = 0.10, horizon N = 2
    value-neutral IIR* = c·(1 + WACC)/WACC = 0.15 · 1.1 / 0.1 = 1.65
"""

import pytest
from backend.calc.dcf import (
    ImpliedGrowth,
    NoImpliedGrowth,
    ValueDrivers,
    average_operating_margin,
    enterprise_value,
    incremental_investment_rate,
    market_capitalisation,
    net_debt,
    sensitivity_grid,
    solve_implied_growth,
    target_enterprise_value,
    value_neutral_investment_rate,
)
from backend.calc.errors import ValuationInputError
from pydantic import ValidationError


def _drivers(**overrides: float) -> ValueDrivers:
    fields: dict[str, float] = {
        "base_revenue": 100,
        "operating_margin": 0.20,
        "tax_rate": 0.25,
        "incremental_investment_rate": 0.5,
        "horizon_years": 2,
        "wacc": 0.10,
    }
    fields.update(overrides)
    return ValueDrivers.model_validate(fields)


_SOLVER = {"search_min": -0.30, "search_max": 0.60, "step": 0.005, "tolerance": 1e-9}


def _solve(drivers: ValueDrivers, target: float) -> ImpliedGrowth | NoImpliedGrowth:
    return solve_implied_growth(
        drivers, target_enterprise_value=target, flat_tolerance=1e-6, **_SOLVER
    )


def test_enterprise_value_line_by_line() -> None:
    """g = 10%, IIR = 0.5:
    year 1: Rev 110.00, NOPAT 16.50, Inv 0.5·10 = 5.00, FCFF 11.50, PV 11.50/1.1  = 10.4545
    year 2: Rev 121.00, NOPAT 18.15, Inv 0.5·11 = 5.50, FCFF 12.65, PV 12.65/1.21 = 10.4545
    TV = NOPAT_2 / WACC = 18.15 / 0.10 = 181.50,  PV(TV) = 181.50 / 1.21 = 150.00
    EV = 10.4545 + 10.4545 + 150.00 = 170.9091"""
    result = enterprise_value(_drivers(), 0.10)

    y1, y2 = result.years
    assert (y1.revenue, y1.nopat, y1.investment, y1.fcff) == pytest.approx((110.0, 16.5, 5.0, 11.5))
    assert (y2.revenue, y2.nopat, y2.investment, y2.fcff) == pytest.approx(
        (121.0, 18.15, 5.5, 12.65)
    )
    assert y1.present_value == pytest.approx(11.5 / 1.1)
    assert y2.present_value == pytest.approx(12.65 / 1.21)
    assert result.terminal_value == pytest.approx(181.5)
    assert result.pv_terminal == pytest.approx(150.0)
    assert result.enterprise_value == pytest.approx(170.9090909)


@pytest.mark.parametrize("horizon", [1, 2, 5, 10, 20])
def test_zero_growth_ev_is_nopat_over_wacc_for_any_horizon(horizon: int) -> None:
    """No growth means no incremental investment, so EV is a perpetuity of
    NOPAT_0 = 15: 15 / 0.10 = 150, whatever the horizon."""
    assert enterprise_value(_drivers(horizon_years=horizon), 0.0).enterprise_value == (
        pytest.approx(150.0)
    )


def test_growth_destroys_value_when_reinvestment_is_above_break_even() -> None:
    """IIR = 3 > IIR* = 1.65, g = 10%:
    year 1: FCFF = 16.50 − 3·10 = −13.50,  PV = −12.2727
    year 2: FCFF = 18.15 − 3·11 = −14.85,  PV = −12.2727
    EV = −24.5455 + 150 = 125.4545, which is below the 150 of zero growth."""
    result = enterprise_value(_drivers(incremental_investment_rate=3.0), 0.10)
    assert result.enterprise_value == pytest.approx(125.4545454)


def test_at_break_even_reinvestment_growth_changes_nothing() -> None:
    """IIR = IIR* = 1.65: year 1 FCFF = 16.5 − 1.65·10 = 0, year 2 FCFF =
    18.15 − 1.65·11 = 0, PV(TV) = 150. EV = 150, the same as zero growth."""
    drivers = _drivers(incremental_investment_rate=1.65)
    assert value_neutral_investment_rate(drivers) == pytest.approx(1.65)
    for growth in (-0.2, 0.0, 0.1, 0.5):
        assert enterprise_value(drivers, growth).enterprise_value == pytest.approx(150.0)


def test_growth_at_or_below_minus_100_percent_is_rejected() -> None:
    with pytest.raises(ValuationInputError):
        enterprise_value(_drivers(), -1.0)


@pytest.mark.parametrize(
    "bad", [{"base_revenue": 0}, {"wacc": 0}, {"tax_rate": 1.0}, {"horizon_years": 0}]
)
def test_invalid_drivers_are_rejected(bad: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        _drivers(**bad)


@pytest.mark.parametrize("true_growth", [-0.20, -0.05, 0.0, 0.03, 0.12, 0.25, 0.55])
def test_solver_recovers_the_growth_that_produced_the_target(true_growth: float) -> None:
    """Property test: price the model at a known growth rate, hand that EV
    to the solver, and it must give the growth rate back."""
    drivers = _drivers(horizon_years=10)
    target = enterprise_value(drivers, true_growth).enterprise_value

    result = _solve(drivers, target)

    assert isinstance(result, ImpliedGrowth)
    assert result.growth == pytest.approx(true_growth, abs=1e-7)
    assert result.growth_creates_value is True


def test_hand_worked_target_solves_to_ten_percent() -> None:
    """170.9091 is the EV of the line-by-line case at g = 10%."""
    result = _solve(_drivers(), 170.9090909)
    assert isinstance(result, ImpliedGrowth)
    assert result.growth == pytest.approx(0.10, abs=1e-6)


def test_value_destroying_growth_inverts_the_story() -> None:
    """With IIR above break-even, a HIGHER target EV implies LOWER growth.
    The solver must still find it, and must flag that growth destroys value."""
    drivers = _drivers(incremental_investment_rate=3.0, horizon_years=10)
    low_price = enterprise_value(drivers, 0.20).enterprise_value
    high_price = enterprise_value(drivers, 0.05).enterprise_value
    assert high_price > low_price

    low = _solve(drivers, low_price)
    high = _solve(drivers, high_price)

    assert isinstance(low, ImpliedGrowth) and isinstance(high, ImpliedGrowth)
    assert low.growth == pytest.approx(0.20, abs=1e-7)
    assert high.growth == pytest.approx(0.05, abs=1e-7)
    assert low.growth_creates_value is False
    assert low.value_neutral_investment_rate == pytest.approx(1.65)


def test_value_neutral_drivers_have_no_implied_growth() -> None:
    result = _solve(_drivers(incremental_investment_rate=1.65), 150.0)
    assert isinstance(result, NoImpliedGrowth)
    assert result.growth_creates_value is None
    assert "value-neutral" in result.reason


def test_target_above_every_ev_in_range_has_no_implied_growth() -> None:
    drivers = _drivers(horizon_years=10)
    ceiling = enterprise_value(drivers, 0.60).enterprise_value

    result = _solve(drivers, ceiling * 2)

    assert isinstance(result, NoImpliedGrowth)
    assert "above the highest" in result.reason
    assert result.max_enterprise_value == pytest.approx(ceiling)
    assert result.growth_creates_value is True


def test_target_below_every_ev_in_range_has_no_implied_growth() -> None:
    result = _solve(_drivers(horizon_years=10), 1.0)
    assert isinstance(result, NoImpliedGrowth)
    assert "below the lowest" in result.reason


@pytest.mark.parametrize(
    "kwargs",
    [
        {"search_min": -1.0, "search_max": 0.5, "step": 0.01, "tolerance": 1e-6},
        {"search_min": 0.5, "search_max": 0.1, "step": 0.01, "tolerance": 1e-6},
        {"search_min": 0.0, "search_max": 0.5, "step": 0.0, "tolerance": 1e-6},
        {"search_min": 0.0, "search_max": 0.5, "step": 0.01, "tolerance": 0.0},
    ],
)
def test_invalid_search_settings_are_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValuationInputError):
        solve_implied_growth(
            _drivers(), target_enterprise_value=160.0, flat_tolerance=1e-6, **kwargs
        )


def test_sensitivity_grid_by_hand() -> None:
    """Value per share = (EV − net debt 20) / 10 shares:
    g=0,   WACC=10%: EV 150        → 13.0000
    g=10%, WACC=10%: EV 170.9091   → 15.0909
    g=0,   WACC=5%:  EV 15/0.05 = 300 → 28.0000"""
    grid = sensitivity_grid(
        _drivers(), growths=[0.0, 0.10], waccs=[0.10, 0.05], net_debt=20, shares_outstanding=10
    )
    assert grid.values[0][0] == pytest.approx(13.0)
    assert grid.values[1][0] == pytest.approx(15.0909091)
    assert grid.values[0][1] == pytest.approx(28.0)
    assert grid.growths == [0.0, 0.10] and grid.waccs == [0.10, 0.05]


def test_sensitivity_grid_rejects_non_positive_shares() -> None:
    with pytest.raises(ValuationInputError):
        sensitivity_grid(_drivers(), growths=[0.0], waccs=[0.1], net_debt=0, shares_outstanding=0)


def test_target_enterprise_value_by_hand() -> None:
    """1,000 market cap + 50 net debt = 1,050."""
    assert target_enterprise_value(market_cap=1000, net_debt=50).value == 1050
    # Net cash (negative net debt) lowers the EV the business must justify.
    assert target_enterprise_value(market_cap=1000, net_debt=-200).value == 800
    with pytest.raises(ValuationInputError):
        target_enterprise_value(market_cap=0, net_debt=50)


def test_incremental_investment_rate_by_hand() -> None:
    """(capex 120 − D&A 80 + ΔNWC 10) / ΔRevenue 100 = 50 / 100 = 0.5"""
    result = incremental_investment_rate(
        capex=120, depreciation=80, increase_in_working_capital=10, revenue_change=100
    )
    assert result.value == pytest.approx(0.5)


@pytest.mark.parametrize("revenue_change", [0.0, -50.0])
def test_incremental_investment_rate_needs_revenue_growth(revenue_change: float) -> None:
    with pytest.raises(ValuationInputError):
        incremental_investment_rate(
            capex=120,
            depreciation=80,
            increase_in_working_capital=10,
            revenue_change=revenue_change,
        )


@pytest.mark.parametrize("horizon", [1, 3, 10, 20])
@pytest.mark.parametrize("wacc", [0.04, 0.08, 0.15])
@pytest.mark.parametrize("multiple_of_break_even", [0.3, 0.9, 1.1, 3.0])
def test_ev_is_monotonic_in_growth_with_direction_set_by_break_even(
    horizon: int, wacc: float, multiple_of_break_even: float
) -> None:
    """The solver's correctness argument (module docstring): for constant
    drivers, EV moves in ONE direction across the whole growth range. It
    rises when IIR is below the break-even IIR*, and falls when above."""
    base = _drivers(horizon_years=horizon, wacc=wacc)
    iir = value_neutral_investment_rate(base) * multiple_of_break_even
    drivers = base.model_copy(update={"incremental_investment_rate": iir})
    growths = [-0.5 + i * 0.01 for i in range(201)]  # -50% .. +150%
    evs = [enterprise_value(drivers, g).enterprise_value for g in growths]
    steps = [b - a for a, b in zip(evs, evs[1:], strict=False)]

    if multiple_of_break_even < 1:
        assert all(step > 0 for step in steps)
    else:
        assert all(step < 0 for step in steps)


def test_average_operating_margin_by_hand() -> None:
    """Margins 20/100 = 0.20, 27/90 = 0.30, 8/80 = 0.10 → mean 0.20."""
    result = average_operating_margin(operating_incomes=[20, 27, 8], revenues=[100, 90, 80])
    assert result.value == pytest.approx(0.20)
    assert result.inputs["operating_income_1"] == 27 and result.inputs["revenue_2"] == 80
    with pytest.raises(ValuationInputError):
        average_operating_margin(operating_incomes=[20], revenues=[0])
    with pytest.raises(ValuationInputError):
        average_operating_margin(operating_incomes=[20, 30], revenues=[100])


def test_market_capitalisation_and_net_debt_by_hand() -> None:
    """1,096.33 × 2,464,000,000 shares = 2,701,357,120,000; 100 − 130 = −30."""
    cap = market_capitalisation(price=1096.33, shares_outstanding=2_464_000_000)
    assert cap.value == pytest.approx(2_701_357_120_000)
    assert net_debt(total_debt=100, cash=130).value == -30
    with pytest.raises(ValuationInputError):
        market_capitalisation(price=0, shares_outstanding=10)
    with pytest.raises(ValuationInputError):
        net_debt(total_debt=-1, cash=0)
