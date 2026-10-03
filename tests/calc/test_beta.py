"""Hand-worked golden values for backend.calc.beta."""

from datetime import date

import pytest
from backend.calc.beta import monthly_returns, ols_beta
from backend.calc.errors import ValuationInputError


def test_monthly_returns_use_the_last_close_of_each_month() -> None:
    """Jan's last close is 110 (Jan 31), not 100 (Jan 2).
    Feb: 121 / 110 - 1 = 0.10.  Mar: 108.9 / 121 - 1 = -0.10."""
    closes = [
        (date(2024, 1, 2), 100.0),
        (date(2024, 1, 31), 110.0),
        (date(2024, 2, 15), 999.0),
        (date(2024, 2, 29), 121.0),
        (date(2024, 3, 28), 108.9),
    ]
    returns = monthly_returns(closes)
    assert set(returns) == {(2024, 2), (2024, 3)}
    assert returns[(2024, 2)] == pytest.approx(0.10)
    assert returns[(2024, 3)] == pytest.approx(-0.10)


def test_input_order_does_not_matter() -> None:
    closes = [(date(2024, 2, 29), 121.0), (date(2024, 1, 31), 110.0)]
    assert monthly_returns(closes) == monthly_returns(sorted(closes))


def test_a_missing_month_never_becomes_a_two_month_return() -> None:
    """No February data: there's no return for March (it would span two
    months), and none for January (nothing before it)."""
    closes = [(date(2024, 1, 31), 100.0), (date(2024, 3, 28), 130.0), (date(2024, 4, 30), 143.0)]
    returns = monthly_returns(closes)
    assert set(returns) == {(2024, 4)}
    assert returns[(2024, 4)] == pytest.approx(0.10)


def test_returns_cross_a_year_boundary() -> None:
    returns = monthly_returns([(date(2023, 12, 29), 100.0), (date(2024, 1, 31), 105.0)])
    assert returns == {(2024, 1): pytest.approx(0.05)}


def test_non_positive_close_is_rejected() -> None:
    with pytest.raises(ValuationInputError):
        monthly_returns([(date(2024, 1, 31), 0.0)])


def test_ols_beta_by_hand() -> None:
    """market x = [0.02, -0.01, 0.03, 0.00], mean 0.01
    stock   y = [0.03, -0.02, 0.05, 0.02], mean 0.02
    dx = [0.01, -0.02, 0.02, -0.01],  dy = [0.01, -0.04, 0.03, 0.00]
    sum(dx*dy) = 0.0001 + 0.0008 + 0.0006 + 0 = 0.0015
    sum(dx^2)  = 0.0001 + 0.0004 + 0.0004 + 0.0001 = 0.0010
    beta = 0.0015 / 0.0010 = 1.5   (covariance 0.0005, variance 0.001/3)"""
    months = [(2024, m) for m in range(1, 5)]
    market = dict(zip(months, [0.02, -0.01, 0.03, 0.00], strict=True))
    stock = dict(zip(months, [0.03, -0.02, 0.05, 0.02], strict=True))

    result = ols_beta(stock_returns=stock, market_returns=market, min_observations=4)

    assert result.value == pytest.approx(1.5)
    assert result.inputs["covariance"] == pytest.approx(0.0005)
    assert result.inputs["market_variance"] == pytest.approx(0.001 / 3)
    assert result.inputs["observations"] == 4


def test_ols_beta_only_uses_months_both_series_have() -> None:
    """The (2024, 5) stock return has no market partner and is ignored, so
    the answer is the same 1.5 as above."""
    months = [(2024, m) for m in range(1, 5)]
    market = dict(zip(months, [0.02, -0.01, 0.03, 0.00], strict=True))
    stock = {**dict(zip(months, [0.03, -0.02, 0.05, 0.02], strict=True)), (2024, 5): 0.5}
    assert ols_beta(stock_returns=stock, market_returns=market, min_observations=4).value == (
        pytest.approx(1.5)
    )


def test_too_few_observations_is_rejected() -> None:
    months = [(2024, m) for m in range(1, 4)]
    series = dict(zip(months, [0.01, 0.02, 0.03], strict=True))
    with pytest.raises(ValuationInputError, match="need at least 36"):
        ols_beta(stock_returns=series, market_returns=series, min_observations=36)


def test_flat_market_is_rejected() -> None:
    months = [(2024, m) for m in range(1, 5)]
    flat = dict.fromkeys(months, 0.01)
    with pytest.raises(ValuationInputError, match="zero variance"):
        ols_beta(stock_returns=flat, market_returns=flat, min_observations=4)
