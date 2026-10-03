"""Reverse DCF on Rappaport & Mauboussin's value drivers.

New-concept note: a normal ("forward") DCF guesses a growth rate and
outputs a fair value. A REVERSE DCF runs the other way. It takes today's
market price as given and solves for the growth rate that would make the
DCF value equal that price. The output is not "what the stock is worth"
but "what the market is assuming", and that is a falsifiable claim the
rest of the system can test against the company's history (CLAUDE.md §8;
Rappaport & Mauboussin, *Expectations Investing*). See ADR-0006.

The model, for constant revenue growth g over an explicit horizon of N
years (every input except g is a fixed "value driver"):

    Rev_t   = Rev_0 · (1 + g)^t
    NOPAT_t = Rev_t · operating_margin · (1 − tax_rate)
    Inv_t   = incremental_investment_rate · (Rev_t − Rev_{t−1})
    FCFF_t  = NOPAT_t − Inv_t
    PV      = Σ_{t=1..N} FCFF_t / (1 + WACC)^t
    TV      = NOPAT_N / WACC                  (perpetuity method)
    EV      = PV + TV / (1 + WACC)^N

WHY reinvestment is tied to growth (Inv_t): growth isn't free. To sell
more, a company must invest in capacity and working capital. Without this
term, any growth rate is costless and the implied growth is biased low.

WHY the perpetuity terminal value (NOPAT_N / WACC, no growth after N):
it assumes that growth after the horizon earns exactly its cost of capital,
so it adds no value. That's Mauboussin's standard assumption. It removes
the "terminal growth rate" input, which in a Gordon-growth DCF often
supplies most of the value from a single guessed number. After N, revenue
stops growing, so no further incremental investment is needed.

Growth does NOT always add value. Each extra $1 of revenue costs IIR
dollars of investment in the year it arrives and earns
margin·(1 − tax) of NOPAT in that year and every year after. Under this
model's end-of-year timing, that perpetuity is worth margin·(1 − tax)·
(1 + WACC)/WACC at that date. So growth is value-neutral at

    IIR* = margin · (1 − tax) · (1 + WACC) / WACC     (value_neutral_investment_rate)

and EV(g) is monotonic in g, with its direction set by IIR vs IIR*:
  - IIR < IIR*: growth creates value. EV rises with g, and a higher price
    implies higher growth.
  - IIR > IIR*: growth destroys value. EV FALLS with g, so a higher price
    implies LOWER growth. That is a real finding to report, not an error.
  - IIR = IIR*: EV is flat at NOPAT_0 / WACC for every g, and the price
    says nothing about growth.
(Verified numerically across horizons 1–20 and WACC 3–15% while building
this module. An earlier design assumed EV could rise then fall, giving
several roots; for constant drivers it can't.)

WHY the solver still scans before it bisects, given monotonicity: the scan
is what detects the flat case and a target outside the range, and it keeps
the solver correct if the model ever gains non-constant drivers (a margin
or growth fade). If a scan ever finds more than one crossing, the solver
raises instead of picking one, because that would mean a bug or an
unvalidated model change, never a market signal.

Pure functions, no I/O, no LLM, mypy --strict.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.calc.errors import ValuationInputError
from backend.calc.ratios import RatioResult

DCF_FORMULA = (
    "EV = sum_{t=1..N} [Rev_0*(1+g)^t*margin*(1-tax) - IIR*(Rev_t - Rev_{t-1})] / (1+WACC)^t"
    " + [Rev_N*margin*(1-tax) / WACC] / (1+WACC)^N"
)


class ValueDrivers(BaseModel):
    """Every DCF input except the growth rate being solved for.

    `incremental_investment_rate` (IIR) is net investment (capex − D&A +
    increase in operating working capital) per $1 of INCREMENTAL revenue.
    It may legitimately be negative for a company that releases working
    capital as it grows. It isn't bounded here; the assembler (6b) prints
    how it was estimated.
    """

    model_config = ConfigDict(frozen=True)

    base_revenue: float = Field(gt=0)
    operating_margin: float
    tax_rate: float = Field(ge=0, lt=1)
    incremental_investment_rate: float
    horizon_years: int = Field(ge=1)
    wacc: float = Field(gt=0)


class DCFYear(BaseModel):
    year: int
    revenue: float
    nopat: float
    investment: float
    fcff: float
    discount_factor: float
    present_value: float


class DCFResult(BaseModel):
    """One evaluation of the model at one growth rate, with the per-year
    breakdown kept so a reader can check every line by hand."""

    growth: float
    years: list[DCFYear]
    pv_explicit: float
    terminal_value: float
    pv_terminal: float
    enterprise_value: float


def enterprise_value(drivers: ValueDrivers, growth: float) -> DCFResult:
    """Evaluate the model at revenue growth `growth` (a decimal, 0.08 = 8%)."""
    if growth <= -1:
        raise ValuationInputError(f"enterprise_value: growth {growth} must be > -1 (-100%)")
    margin_after_tax = drivers.operating_margin * (1 - drivers.tax_rate)
    years: list[DCFYear] = []
    previous_revenue = drivers.base_revenue
    for t in range(1, drivers.horizon_years + 1):
        revenue = drivers.base_revenue * (1 + growth) ** t
        nopat = revenue * margin_after_tax
        investment = drivers.incremental_investment_rate * (revenue - previous_revenue)
        fcff = nopat - investment
        discount_factor = 1 / (1 + drivers.wacc) ** t
        years.append(
            DCFYear(
                year=t,
                revenue=revenue,
                nopat=nopat,
                investment=investment,
                fcff=fcff,
                discount_factor=discount_factor,
                present_value=fcff * discount_factor,
            )
        )
        previous_revenue = revenue

    pv_explicit = sum(y.present_value for y in years)
    terminal_value = years[-1].nopat / drivers.wacc
    pv_terminal = terminal_value * years[-1].discount_factor
    return DCFResult(
        growth=growth,
        years=years,
        pv_explicit=pv_explicit,
        terminal_value=terminal_value,
        pv_terminal=pv_terminal,
        enterprise_value=pv_explicit + pv_terminal,
    )


def value_neutral_investment_rate(drivers: ValueDrivers) -> float:
    """IIR* = margin · (1 − tax) · (1 + WACC) / WACC: the incremental
    investment rate at which growth neither creates nor destroys value
    under this model's timing. Below it, growth creates value; above it,
    growth destroys value. See the module docstring for the derivation."""
    return drivers.operating_margin * (1 - drivers.tax_rate) * (1 + drivers.wacc) / drivers.wacc


class ImpliedGrowth(BaseModel):
    """The growth rate at which the model's EV equals the target.

    `growth_creates_value` must travel with the number. If it's False,
    the market price is pricing in growth that destroys value, and a
    higher price would have implied LOWER growth. Reading the number
    without that flag gets the direction of the story wrong.
    """

    kind: Literal["implied_growth"] = "implied_growth"
    growth: float
    growth_creates_value: bool
    value_neutral_investment_rate: float
    target_enterprise_value: float


class NoImpliedGrowth(BaseModel):
    """No growth rate in the searched range produces the target EV.

    Returned, never raised, and never replaced with a nearest-guess number
    (C6). `reason` says why: the target is above or below every EV in the
    range, or growth is value-neutral so EV doesn't depend on it at all.
    """

    kind: Literal["no_implied_growth"] = "no_implied_growth"
    reason: str
    growth_creates_value: bool | None
    target_enterprise_value: float
    min_enterprise_value: float
    max_enterprise_value: float
    search_min: float
    search_max: float


def solve_implied_growth(
    drivers: ValueDrivers,
    *,
    target_enterprise_value: float,
    search_min: float,
    search_max: float,
    step: float,
    tolerance: float,
    flat_tolerance: float,
) -> ImpliedGrowth | NoImpliedGrowth:
    """Find the growth rate in [search_min, search_max] where
    EV(growth) = target_enterprise_value.

    1. Scan: evaluate EV on an even grid (`step` apart).
    2. If EV barely varies across the range (its spread is within
       `flat_tolerance` of its size), growth is value-neutral and can't be
       identified from the price, so return NoImpliedGrowth saying so.
    3. Otherwise find the adjacent grid pair where EV − target changes
       sign, and bisect it until it's narrower than `tolerance`.

    Raises ValuationInputError if the scan finds more than one crossing.
    EV is monotonic in growth for constant drivers (module docstring), so
    several crossings would mean a bug, and guessing one would hide it.
    """
    if search_min <= -1 or search_min >= search_max:
        raise ValuationInputError(
            f"solve_implied_growth: need -1 < search_min < search_max, "
            f"got [{search_min}, {search_max}]"
        )
    if step <= 0 or tolerance <= 0 or flat_tolerance <= 0:
        raise ValuationInputError(
            "solve_implied_growth: step, tolerance and flat_tolerance must be positive"
        )

    # WHY an integer count, not `g += step`: repeated float addition drifts,
    # and the grid's last point must be exactly search_max.
    count = max(1, round((search_max - search_min) / step))
    grid = [search_min + i * (search_max - search_min) / count for i in range(count + 1)]
    evs = [enterprise_value(drivers, g).enterprise_value for g in grid]
    min_ev, max_ev = min(evs), max(evs)
    neutral_iir = value_neutral_investment_rate(drivers)

    def no_growth(reason: str, creates_value: bool | None) -> NoImpliedGrowth:
        return NoImpliedGrowth(
            reason=reason,
            growth_creates_value=creates_value,
            target_enterprise_value=target_enterprise_value,
            min_enterprise_value=min_ev,
            max_enterprise_value=max_ev,
            search_min=search_min,
            search_max=search_max,
        )

    scale = max(abs(min_ev), abs(max_ev), 1.0)
    if max_ev - min_ev <= flat_tolerance * scale:
        return no_growth(
            f"Growth is value-neutral on these drivers: the incremental investment rate "
            f"({drivers.incremental_investment_rate:.3f}) equals the break-even rate "
            f"({neutral_iir:.3f}), so EV is about {min_ev:,.0f} at every growth rate and "
            "the market price says nothing about growth.",
            None,
        )

    creates_value = evs[-1] > evs[0]
    direction = "rises" if creates_value else "falls"

    def gap(growth: float) -> float:
        return enterprise_value(drivers, growth).enterprise_value - target_enterprise_value

    gaps = [ev - target_enterprise_value for ev in evs]
    brackets = [
        (grid[i], grid[i + 1], gaps[i])
        for i in range(count)
        if gaps[i] == 0 or gaps[i] * gaps[i + 1] < 0
    ]
    if gaps[-1] == 0:
        brackets.append((grid[-1], grid[-1], 0.0))

    if len(brackets) > 1:
        raise ValuationInputError(
            f"solve_implied_growth: {len(brackets)} crossings found, but EV should be "
            "monotonic in growth for constant value drivers. Refusing to pick one."
        )
    if len(brackets) == 1:
        lo, hi, f_lo = brackets[0]
        growth = lo if f_lo == 0 else _bisect(gap, lo, hi, f_lo, tolerance)
        return ImpliedGrowth(
            growth=growth,
            growth_creates_value=creates_value,
            value_neutral_investment_rate=neutral_iir,
            target_enterprise_value=target_enterprise_value,
        )

    side = "above the highest" if target_enterprise_value > max_ev else "below the lowest"
    bound = max_ev if target_enterprise_value > max_ev else min_ev
    return no_growth(
        f"The target EV {target_enterprise_value:,.0f} is {side} EV ({bound:,.0f}) that any "
        f"growth rate in [{search_min:.1%}, {search_max:.1%}] produces. EV {direction} with "
        "growth on these value drivers.",
        creates_value,
    )


def _bisect(
    gap: Callable[[float], float], lo: float, hi: float, f_lo: float, tolerance: float
) -> float:
    """Standard bisection on a bracket [lo, hi] known to contain a sign
    change. Converges in about log2((hi - lo) / tolerance) steps."""
    while hi - lo > tolerance:
        mid = (lo + hi) / 2
        f_mid = gap(mid)
        if f_mid == 0:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


class SensitivityGrid(BaseModel):
    """Equity value per share at each (growth, WACC) pair.

    `values[i][j]` is the value at `growths[i]` and `waccs[j]`. WHY this
    exists alongside the reverse DCF (CLAUDE.md §8, secondary output): one
    implied growth rate hides how sensitive the answer is to WACC, and the
    grid shows that sensitivity honestly instead of one precise-looking
    number.
    """

    growths: list[float]
    waccs: list[float]
    values: list[list[float]]


def sensitivity_grid(
    drivers: ValueDrivers,
    *,
    growths: Sequence[float],
    waccs: Sequence[float],
    net_debt: float,
    shares_outstanding: float,
) -> SensitivityGrid:
    """Value per share = (EV(growth, wacc) − net_debt) / shares_outstanding,
    for every combination. Every input except growth and WACC is held at
    `drivers`."""
    if shares_outstanding <= 0:
        raise ValuationInputError(
            f"sensitivity_grid: shares_outstanding must be positive, got {shares_outstanding}"
        )
    values = [
        [
            (
                enterprise_value(drivers.model_copy(update={"wacc": w}), g).enterprise_value
                - net_debt
            )
            / shares_outstanding
            for w in waccs
        ]
        for g in growths
    ]
    return SensitivityGrid(growths=list(growths), waccs=list(waccs), values=values)


def target_enterprise_value(*, market_cap: float, net_debt: float) -> RatioResult:
    """market_cap + net_debt: the enterprise value the market price
    implies, i.e. what the reverse DCF solves to match."""
    if market_cap <= 0:
        raise ValuationInputError(f"target_enterprise_value: market_cap {market_cap} <= 0")
    return RatioResult(
        name="target_enterprise_value",
        formula="market_cap + net_debt",
        inputs={"market_cap": market_cap, "net_debt": net_debt},
        value=market_cap + net_debt,
    )


def incremental_investment_rate(
    *, capex: float, depreciation: float, increase_in_working_capital: float, revenue_change: float
) -> RatioResult:
    """(capex − depreciation + increase_in_working_capital) / revenue_change.

    Net investment per $1 of incremental revenue over the same period.
    Callers sum several years of each input first, because a single year
    is too noisy. Raises when revenue didn't grow (revenue_change <= 0):
    the ratio is then undefined or has the wrong sign, and the caller must
    decide what to do (6b uses a configured, clearly-labelled assumption).
    """
    if revenue_change <= 0:
        raise ValuationInputError(
            f"incremental_investment_rate: revenue_change {revenue_change} must be positive"
        )
    value = (capex - depreciation + increase_in_working_capital) / revenue_change
    return RatioResult(
        name="incremental_investment_rate",
        formula="(capex - depreciation + increase_in_working_capital) / revenue_change",
        inputs={
            "capex": capex,
            "depreciation": depreciation,
            "increase_in_working_capital": increase_in_working_capital,
            "revenue_change": revenue_change,
        },
        value=value,
    )


def average_operating_margin(
    *, operating_incomes: Sequence[float], revenues: Sequence[float]
) -> RatioResult:
    """Mean of operating_income_i / revenue_i over the same fiscal years.

    WHY an average of yearly margins, not one year: a single year's margin
    carries one-offs (a restructuring charge, a windfall). Averaging three
    years is the usual smoothing for a constant-margin value driver. The
    inputs are flattened to operating_income_0.., revenue_0.. because
    RatioResult inputs are scalars.
    """
    if not revenues or len(operating_incomes) != len(revenues):
        raise ValuationInputError(
            "average_operating_margin: need matching, non-empty operating income and revenue"
        )
    if any(r <= 0 for r in revenues):
        raise ValuationInputError("average_operating_margin: every revenue must be positive")
    margins = [oi / r for oi, r in zip(operating_incomes, revenues, strict=True)]
    inputs = {f"operating_income_{i}": oi for i, oi in enumerate(operating_incomes)}
    inputs.update({f"revenue_{i}": r for i, r in enumerate(revenues)})
    return RatioResult(
        name="average_operating_margin",
        formula="mean(operating_income_i / revenue_i)",
        inputs=inputs,
        value=sum(margins) / len(margins),
    )


def market_capitalisation(*, price: float, shares_outstanding: float) -> RatioResult:
    """price × shares_outstanding. Both must describe the SAME date's share
    basis: an as-traded price with a split-aligned share count (ADR-0021)."""
    if price <= 0 or shares_outstanding <= 0:
        raise ValuationInputError(
            f"market_capitalisation: price {price} and shares {shares_outstanding} must be > 0"
        )
    return RatioResult(
        name="market_capitalisation",
        formula="price * shares_outstanding",
        inputs={"price": price, "shares_outstanding": shares_outstanding},
        value=price * shares_outstanding,
    )


def net_debt(*, total_debt: float, cash: float) -> RatioResult:
    """total_debt − cash. Negative means net cash. Leases and non-current
    investments are excluded, and that's printed with every valuation."""
    if total_debt < 0 or cash < 0:
        raise ValuationInputError(f"net_debt: debt {total_debt} and cash {cash} must be >= 0")
    return RatioResult(
        name="net_debt",
        formula="total_debt - cash",
        inputs={"total_debt": total_debt, "cash": cash},
        value=total_debt - cash,
    )
