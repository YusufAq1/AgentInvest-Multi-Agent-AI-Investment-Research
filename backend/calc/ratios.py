"""Financial ratios computed from XBRL facts. Pure functions, no LLM.

Four ratios, one from each category CLAUDE.md §7 assigns to Python:
profitability (gross_margin, net_margin), liquidity (current_ratio), and
growth (yoy_revenue_growth). Each takes plain floats and is agnostic to
XBRL tagging conventions — resolving "which XBRL concept means revenue for
this filer" is the Financial Agent's job (concept names vary across
filers), not this module's.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from pydantic import BaseModel

from backend.core.llm import AgentInvestError


class RatioInputError(AgentInvestError):
    """A required input was invalid for the formula (e.g. a zero
    denominator) — never silently returns inf/nan; C6's "never fabricate
    on failure" extends to calculations, not just data retrieval."""


class RatioResult(BaseModel):
    """WHY a model, not a bare float: the Financial Agent needs the formula
    text and the exact keyword inputs used to build both the deterministic
    citation `quote` and the `location` dict of the resulting `computed`
    Evidence row. `validation.py`'s recompute check also needs `inputs` to
    call the same function again — pushing this bookkeeping into the one
    function that already has it avoids duplicating it at every call site.
    """

    name: str
    formula: str
    inputs: dict[str, float]
    value: float


def gross_margin(*, revenue: float, cogs: float) -> RatioResult:
    """(revenue - cogs) / revenue."""
    if revenue == 0:
        raise RatioInputError("gross_margin: revenue is zero")
    value = (revenue - cogs) / revenue
    return RatioResult(
        name="gross_margin",
        formula="(revenue - cogs) / revenue",
        inputs={"revenue": revenue, "cogs": cogs},
        value=value,
    )


def net_margin(*, revenue: float, net_income: float) -> RatioResult:
    """net_income / revenue."""
    if revenue == 0:
        raise RatioInputError("net_margin: revenue is zero")
    value = net_income / revenue
    return RatioResult(
        name="net_margin",
        formula="net_income / revenue",
        inputs={"revenue": revenue, "net_income": net_income},
        value=value,
    )


def current_ratio(*, current_assets: float, current_liabilities: float) -> RatioResult:
    """current_assets / current_liabilities."""
    if current_liabilities == 0:
        raise RatioInputError("current_ratio: current_liabilities is zero")
    value = current_assets / current_liabilities
    return RatioResult(
        name="current_ratio",
        formula="current_assets / current_liabilities",
        inputs={"current_assets": current_assets, "current_liabilities": current_liabilities},
        value=value,
    )


def yoy_revenue_growth(*, revenue_current: float, revenue_prior: float) -> RatioResult:
    """(revenue_current - revenue_prior) / revenue_prior."""
    if revenue_prior == 0:
        raise RatioInputError("yoy_revenue_growth: revenue_prior is zero")
    value = (revenue_current - revenue_prior) / revenue_prior
    return RatioResult(
        name="yoy_revenue_growth",
        formula="(revenue_current - revenue_prior) / revenue_prior",
        inputs={"revenue_current": revenue_current, "revenue_prior": revenue_prior},
        value=value,
    )


# WHY one dispatch table, not scattered `if ratio_name == ...` chains: both
# the Financial Agent (to compute) and validation.py (to recompute during
# citation-validity checks) need "look up a ratio function by name" — one
# source of truth, no risk of the two call sites drifting apart.
RATIO_FUNCS: Final[dict[str, Callable[..., RatioResult]]] = {
    "gross_margin": gross_margin,
    "net_margin": net_margin,
    "current_ratio": current_ratio,
    "yoy_revenue_growth": yoy_revenue_growth,
}
