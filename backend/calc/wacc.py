"""Cost of capital: CAPM cost of equity, after-tax cost of debt, and WACC.

Pure functions, no I/O, no LLM (CLAUDE.md §7). Each returns a RatioResult
(formula + inputs + value) so the value can become `computed` evidence that
CI recomputes from its cited inputs.

    Rₑ   = R_f + β·ERP                                (CAPM)
    WACC = (E/V)·Rₑ + (D/V)·R_d·(1 − t),   V = E + D

The simplifications behind each INPUT (10-year Treasury as R_f, a
configured ERP, statutory tax, book debt) are decided and printed where
the inputs are assembled (Phase 6b), not here. This module only does the
arithmetic, and ADR-0006 lists the assumptions.
"""

from __future__ import annotations

from backend.calc.errors import ValuationInputError
from backend.calc.ratios import RatioResult


def cost_of_equity(
    *, risk_free_rate: float, beta: float, equity_risk_premium: float
) -> RatioResult:
    """CAPM: risk_free_rate + beta * equity_risk_premium. All rates are
    decimals (0.042, not 4.2)."""
    value = risk_free_rate + beta * equity_risk_premium
    return RatioResult(
        name="cost_of_equity",
        formula="risk_free_rate + beta * equity_risk_premium",
        inputs={
            "risk_free_rate": risk_free_rate,
            "beta": beta,
            "equity_risk_premium": equity_risk_premium,
        },
        value=value,
    )


def after_tax_cost_of_debt(*, cost_of_debt: float, tax_rate: float) -> RatioResult:
    """cost_of_debt * (1 - tax_rate). WHY after tax: interest is tax
    deductible, so each dollar of interest costs the firm (1 - t) dollars."""
    if not 0 <= tax_rate < 1:
        raise ValuationInputError(f"after_tax_cost_of_debt: tax_rate {tax_rate} not in [0, 1)")
    return RatioResult(
        name="after_tax_cost_of_debt",
        formula="cost_of_debt * (1 - tax_rate)",
        inputs={"cost_of_debt": cost_of_debt, "tax_rate": tax_rate},
        value=cost_of_debt * (1 - tax_rate),
    )


def wacc(
    *,
    equity_value: float,
    debt_value: float,
    cost_of_equity: float,
    cost_of_debt: float,
    tax_rate: float,
) -> RatioResult:
    """(E/V)*cost_of_equity + (D/V)*cost_of_debt*(1 - tax_rate), V = E + D.

    `equity_value` is market capitalisation. `debt_value` is book debt
    (market value of debt isn't available from free data; book is the
    standard approximation, and it's printed as an assumption).
    """
    if equity_value <= 0:
        raise ValuationInputError(f"wacc: equity_value must be positive, got {equity_value}")
    if debt_value < 0:
        raise ValuationInputError(f"wacc: debt_value must be non-negative, got {debt_value}")
    if not 0 <= tax_rate < 1:
        raise ValuationInputError(f"wacc: tax_rate {tax_rate} not in [0, 1)")
    total = equity_value + debt_value
    value = (equity_value / total) * cost_of_equity + (debt_value / total) * cost_of_debt * (
        1 - tax_rate
    )
    return RatioResult(
        name="wacc",
        formula=(
            "(equity_value / (equity_value + debt_value)) * cost_of_equity"
            " + (debt_value / (equity_value + debt_value)) * cost_of_debt * (1 - tax_rate)"
        ),
        inputs={
            "equity_value": equity_value,
            "debt_value": debt_value,
            "cost_of_equity": cost_of_equity,
            "cost_of_debt": cost_of_debt,
            "tax_rate": tax_rate,
        },
        value=value,
    )


def implied_cost_of_debt(*, interest_expense: float, total_debt: float) -> RatioResult:
    """interest_expense / total_debt: the average rate the company pays on
    its book debt. A free-data stand-in for the market yield on its bonds
    (CLAUDE.md §8). It's backward-looking: a company that borrowed cheaply
    years ago shows a low rate even if it would pay more today. That's
    printed as an assumption wherever it's used."""
    if total_debt <= 0:
        raise ValuationInputError(f"implied_cost_of_debt: total_debt {total_debt} must be > 0")
    if interest_expense < 0:
        raise ValuationInputError(
            f"implied_cost_of_debt: interest_expense {interest_expense} must be >= 0"
        )
    return RatioResult(
        name="implied_cost_of_debt",
        formula="interest_expense / total_debt",
        inputs={"interest_expense": interest_expense, "total_debt": total_debt},
        value=interest_expense / total_debt,
    )
