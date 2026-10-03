"""Assemble the reverse DCF's inputs from free data, with provenance.

Sources (all free, CLAUDE.md C1), all as of `as_of` (C3):
  - SEC XBRL companyfacts: revenue, operating income, capex, D&A, working
    capital, debt, cash, interest expense, shares outstanding
  - yfinance: the as-traded close on the last trading day on or before
    as_of (ADR-0021), splits up to as_of, and total-return history for beta
  - FRED: the 10-year Treasury yield (DGS10) as the risk-free rate
  - config: ERP, tax rate, horizon, and the two labelled fallbacks

Every input is a `SourcedInput` carrying where it came from, so the
Valuation Agent (6c) can turn each one into cited Evidence, and the demo
script can print it. Anything not measured is listed in `assumptions`.

Missing data never becomes a made-up number (C6). If a REQUIRED input
can't be found, assembly returns `ValuationUnavailable` listing every
missing input at once. Two inputs have a labelled fallback instead,
because otherwise weak or shrinking companies (CLAUDE.md §17.3's
deliberate test case) could never be valued:
  - cost of debt → R_f + default spread, when interest or debt is missing
  - incremental investment rate → a configured placeholder, when revenue
    didn't grow over the lookback (the historical ratio is undefined)

WHY a separate alias table rather than extending xbrl_facts.CONCEPT_ALIASES:
the Financial Agent fetches exactly that table, and widening it would
change Phase 2-4 behaviour as a side effect.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any, Final, Literal

from pydantic import BaseModel, Field

from backend.agents.xbrl_facts import CONCEPT_ALIASES, PRIOR_YEAR_TOLERANCE_DAYS
from backend.calc.beta import monthly_returns, ols_beta
from backend.calc.dcf import (
    average_operating_margin,
    incremental_investment_rate,
    market_capitalisation,
    net_debt,
)
from backend.calc.errors import ValuationInputError
from backend.calc.ratios import RatioResult
from backend.calc.wacc import implied_cost_of_debt
from backend.core.config import Settings
from backend.data.macro import MacroClient
from backend.data.models import (
    DataUnavailable,
    MacroObservation,
    PriceBar,
    StockSplit,
    XBRLFact,
)
from backend.data.prices import PricesClient, split_factor_between
from backend.data.xbrl import XBRLClient

# Priority order matters: when two aliases report the same period, the
# earlier one wins. Verified against real companyfacts for AAPL, MSFT,
# NVDA, F, KO and AMZN (2026-10-02).
VALUATION_CONCEPTS: Final[dict[str, tuple[str, ...]]] = {
    "revenue": CONCEPT_ALIASES["revenue"],
    "operating_income": ("OperatingIncomeLoss",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"),
    # WHY this order: DDA includes amortization. Plain `Depreciation` is a
    # last resort (MSFT tags only that) and slightly understates D&A, which
    # slightly overstates the investment rate. Provenance shows which was used.
    "depreciation": (
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "Depreciation",
    ),
    # WHY not InterestPaidNet: that's cash paid, not the expense.
    "interest_expense": ("InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"),
    "current_assets": ("AssetsCurrent",),
    "current_liabilities": ("LiabilitiesCurrent",),
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "marketable_securities_current": (
        "MarketableSecuritiesCurrent",
        "ShortTermInvestments",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
    ),
    # Per the US-GAAP taxonomy, LongTermDebt includes its current portion.
    "long_term_debt_total": ("LongTermDebt",),
    "long_term_debt_noncurrent": ("LongTermDebtNoncurrent",),
    "long_term_debt_current": ("LongTermDebtCurrent",),
    # WHY one concept (first found), not a sum: ShortTermBorrowings often
    # already includes commercial paper, so summing could double count.
    # Taking one may undercount; that's the safer error for net debt.
    "short_term_debt": ("ShortTermBorrowings", "CommercialPaper", "OtherShortTermBorrowings"),
    "shares": ("EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"),
}
ALL_VALUATION_CONCEPTS: Final[tuple[str, ...]] = tuple(
    sorted({a for aliases in VALUATION_CONCEPTS.values() for a in aliases})
)

_USD = "USD"
_SHARES = "shares"
# A fiscal-year duration fact spans about a year. 52/53-week fiscal years
# (Apple, Nvidia) land between 357 and 371 days.
_ANNUAL_MIN_DAYS = 330
_ANNUAL_MAX_DAYS = 400

InputSource = Literal["xbrl", "market", "fred", "config", "computed", "assumed"]


Term = tuple[float, XBRLFact]


class SourcedInput(BaseModel):
    """One valuation input and exactly where it came from.

    `terms`: for an input that is a signed combination of XBRL facts
    (debt = LongTermDebt + CommercialPaper; shares = count × split factor),
    value = Σ coefficient × fact.value.
    `param_terms`: for a computed input, the same thing per parameter of
    its calculation (e.g. the investment rate's `revenue_change` is
    +FY0 − FY3). The Valuation Agent turns both into citations that
    backend/evidence/validation.py recomputes.
    """

    name: str
    value: float
    source: InputSource
    facts: list[XBRLFact] = []
    terms: list[Term] = []
    param_terms: dict[str, list[Term]] = {}
    calculation: RatioResult | None = None
    note: str | None = None


class ValuationInputs(BaseModel):
    """Everything the reverse DCF needs, each input traceable."""

    ticker: str
    as_of: date
    fiscal_year_end: date
    revenue_history: list[SourcedInput]  # newest first: FY0, FY-1, ...
    base_revenue: SourcedInput
    operating_margin: SourcedInput
    incremental_investment_rate: SourcedInput
    price: SourcedInput
    price_bar: PriceBar
    shares_outstanding: SourcedInput
    market_cap: SourcedInput
    total_debt: SourcedInput
    cash: SourcedInput
    net_debt: SourcedInput
    risk_free_rate: SourcedInput
    risk_free_observation: MacroObservation
    beta: SourcedInput
    cost_of_debt: SourcedInput
    tax_rate: SourcedInput
    equity_risk_premium: SourcedInput
    assumptions: list[str]
    # The raw companyfacts JSON, kept so xbrl_fact evidence can quote each
    # fact's exact raw entry. Excluded from dumps: it's megabytes.
    xbrl_raw: dict[str, Any] = Field(default_factory=dict, exclude=True, repr=False)


class ValuationUnavailable(BaseModel):
    """Assembly couldn't produce a valuation. `missing` lists EVERY
    required input that couldn't be found, so one run tells you everything
    that's wrong instead of one problem at a time."""

    ticker: str
    as_of: date
    missing: list[str]


# --------------------------------------------------------------------------
# Pure fact selection (no I/O, unit-tested directly)
# --------------------------------------------------------------------------


def _ranked(facts: Sequence[XBRLFact], aliases: tuple[str, ...], unit: str) -> list[XBRLFact]:
    return [f for f in facts if f.concept in aliases and f.unit == unit]


def _priority(fact: XBRLFact, aliases: tuple[str, ...]) -> int:
    return aliases.index(fact.concept)


def annual_series(
    facts: Sequence[XBRLFact], aliases: tuple[str, ...], unit: str = _USD
) -> dict[date, XBRLFact]:
    """One annual (fiscal-year) value per fiscal-year end, from 10-K filings.

    When several facts describe the same fiscal year (a figure repeated in
    later 10-Ks, or two aliases), the higher-priority alias wins, then the
    most recently filed. Restatements filed on or before as_of are
    legitimate information as of that date, and the XBRL client has
    already dropped anything filed later.
    """
    best: dict[date, XBRLFact] = {}
    for fact in _ranked(facts, aliases, unit):
        if fact.period_start is None or not fact.form.startswith("10-K"):
            continue
        days = (fact.period_end - fact.period_start).days
        if not _ANNUAL_MIN_DAYS <= days <= _ANNUAL_MAX_DAYS:
            continue
        current = best.get(fact.period_end)
        if current is None or (-_priority(fact, aliases), fact.filed) > (
            -_priority(current, aliases),
            current.filed,
        ):
            best[fact.period_end] = fact
    return best


def latest_instant(
    facts: Sequence[XBRLFact],
    aliases: tuple[str, ...],
    *,
    as_of: date,
    max_age_days: int,
    unit: str = _USD,
) -> XBRLFact | None:
    """The most recent point-in-time value (any form, including 10-Q), or
    None if the newest one is older than `max_age_days` before as_of."""
    instants = [f for f in _ranked(facts, aliases, unit) if f.period_start is None]
    if not instants:
        return None
    newest = max(instants, key=lambda f: (f.period_end, -_priority(f, aliases), f.filed))
    if (as_of - newest.period_end).days > max_age_days:
        return None
    return newest


def instant_on(
    facts: Sequence[XBRLFact], aliases: tuple[str, ...], day: date, unit: str = _USD
) -> XBRLFact | None:
    """The point-in-time value reported for exactly `day`, if any."""
    matches = [f for f in _ranked(facts, aliases, unit) if f.period_start is None]
    matches = [f for f in matches if f.period_end == day]
    if not matches:
        return None
    return max(matches, key=lambda f: (-_priority(f, aliases), f.filed))


def prior_fiscal_year_ends(
    series: dict[date, XBRLFact], latest: date, count: int
) -> list[date | None]:
    """[latest, ~1y earlier, ~2y earlier, ...], `count` entries, each the
    available fiscal-year end closest to latest − k years (within the
    shared YoY tolerance), or None when there isn't one."""
    ends: list[date | None] = [latest]
    for k in range(1, count):
        target = latest - timedelta(days=round(365.25 * k))
        candidates = [d for d in series if d < latest]
        best = min(candidates, key=lambda d: abs((d - target).days), default=None)
        if best is None or abs((best - target).days) > PRIOR_YEAR_TOLERANCE_DAYS:
            ends.append(None)
        else:
            ends.append(best)
    return ends


def operating_working_capital(
    facts: Sequence[XBRLFact], day: date
) -> tuple[float, list[Term]] | None:
    """(current assets − cash − current marketable securities)
    − (current liabilities − current portion of long-term debt − short-term debt)
    on fiscal-year-end `day`. Cash-like assets and debt are removed because
    they're financing, not operations.

    Returns (value, signed terms), or None if current assets, current
    liabilities or cash is missing. The marketable-securities and debt
    components count as zero when untagged.
    """
    required = {}
    for name in ("current_assets", "current_liabilities", "cash"):
        fact = instant_on(facts, VALUATION_CONCEPTS[name], day)
        if fact is None:
            return None
        required[name] = fact
    optional = {
        name: instant_on(facts, VALUATION_CONCEPTS[name], day)
        for name in ("marketable_securities_current", "long_term_debt_current", "short_term_debt")
    }

    signs = {
        "current_assets": 1.0,
        "cash": -1.0,
        "marketable_securities_current": -1.0,
        "current_liabilities": -1.0,
        "long_term_debt_current": 1.0,
        "short_term_debt": 1.0,
    }
    present = {**required, **{k: f for k, f in optional.items() if f is not None}}
    terms = [(signs[name], fact) for name, fact in present.items()]
    return sum(c * f.value for c, f in terms), terms


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


class _Missing:
    """Collects every missing input so assembly reports them all at once."""

    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, item: str) -> None:
        self.items.append(item)


def _xbrl_input(name: str, fact: XBRLFact, note: str | None = None) -> SourcedInput:
    return SourcedInput(name=name, value=fact.value, source="xbrl", facts=[fact], note=note)


def _computed(
    name: str,
    result: RatioResult,
    facts: list[XBRLFact],
    note: str | None = None,
    *,
    param_terms: dict[str, list[Term]] | None = None,
) -> SourcedInput:
    return SourcedInput(
        name=name,
        value=result.value,
        source="computed",
        facts=facts,
        param_terms=param_terms or {},
        calculation=result,
        note=note,
    )


class ValuationInputAssembler:
    """Fetches and selects every valuation input for (ticker, as_of)."""

    def __init__(
        self, xbrl: XBRLClient, prices: PricesClient, macro: MacroClient, settings: Settings
    ) -> None:
        self._xbrl = xbrl
        self._prices = prices
        self._macro = macro
        self._settings = settings

    async def assemble(self, ticker: str, as_of: date) -> ValuationInputs | ValuationUnavailable:
        settings = self._settings
        missing = _Missing()
        assumptions: list[str] = [
            f"Equity risk premium {settings.valuation_equity_risk_premium:.2%} (configured "
            "constant, not estimated).",
            f"Tax rate {settings.valuation_tax_rate:.0%} (US statutory), not the effective rate.",
            "Debt at book value, not market value. Leases excluded from debt.",
            "Cash includes current marketable securities; non-current investments excluded.",
            f"Explicit horizon {settings.valuation_horizon_years} years; after it, growth adds "
            "no value (perpetuity terminal value).",
            "Operating margin, investment rate and WACC are held constant over the horizon.",
        ]

        facts_result = await self._xbrl.get_company_facts_with_raw(
            ticker, as_of, concepts=ALL_VALUATION_CONCEPTS
        )
        if isinstance(facts_result, DataUnavailable):
            return ValuationUnavailable(
                ticker=ticker, as_of=as_of, missing=[f"XBRL facts: {facts_result.reason}"]
            )
        # WHY re-filter when XBRLClient already filters on `filed`: the
        # project's rule is never to trust an upstream filter without a
        # local, testable check (C3). This line is what the leakage test
        # exercises.
        facts = [f for f in facts_result.facts if f.filed <= as_of]

        # ---- Fundamentals from the latest fiscal year ------------------
        revenue = annual_series(facts, VALUATION_CONCEPTS["revenue"])
        if not revenue:
            return ValuationUnavailable(
                ticker=ticker, as_of=as_of, missing=["annual revenue (no 10-K revenue facts)"]
            )
        fy0 = max(revenue)
        ends = prior_fiscal_year_ends(revenue, fy0, 6)  # FY0..FY-5
        revenue_history = [
            _xbrl_input(f"revenue_fy{-k}" if k else "revenue_fy0", revenue[end])
            for k, end in enumerate(ends)
            if end is not None
        ]

        opinc = annual_series(facts, VALUATION_CONCEPTS["operating_income"])
        margin_years = ends[:3]
        operating_margin: SourcedInput | None = None
        if all(e is not None and e in opinc for e in margin_years):
            years = [e for e in margin_years if e is not None]
            result = average_operating_margin(
                operating_incomes=[opinc[e].value for e in years],
                revenues=[revenue[e].value for e in years],
            )
            operating_margin = _computed(
                "operating_margin",
                result,
                [opinc[e] for e in years] + [revenue[e] for e in years],
                note="Average of the last 3 fiscal years.",
                param_terms={
                    **{f"operating_income_{i}": [(1.0, opinc[e])] for i, e in enumerate(years)},
                    **{f"revenue_{i}": [(1.0, revenue[e])] for i, e in enumerate(years)},
                },
            )
        else:
            missing.add("operating income for each of the last 3 fiscal years")

        iir = self._incremental_investment_rate(facts, revenue, ends, missing, assumptions)

        # ---- Market data -----------------------------------------------
        bars = await self._prices.get_price_history(
            ticker,
            as_of,
            lookback_days=settings.valuation_price_lookback_days,
            adjustment="as_traded",
        )
        price_bar: PriceBar | None = None
        if isinstance(bars, DataUnavailable):
            missing.add(f"as-traded price: {bars.reason}")
        else:
            price_bar = bars[-1]

        splits = await self._prices.get_splits(ticker, as_of)
        if isinstance(splits, DataUnavailable):
            missing.add(f"split history: {splits.reason}")
            splits = []

        shares = self._shares(facts, as_of, price_bar, splits, missing)

        debt = self._total_debt(facts, as_of, missing)
        cash = self._cash(facts, as_of, missing)

        rf_observation: MacroObservation | None = None
        rf_series = await self._macro.get_series("risk_free_rate", as_of, lookback_days=30)
        if isinstance(rf_series, DataUnavailable):
            missing.add(f"risk-free rate (FRED DGS10): {rf_series.reason}")
        else:
            rf_observation = rf_series[-1]

        beta = await self._beta(ticker, as_of, missing)

        if (
            missing.items
            or operating_margin is None
            or iir is None
            or price_bar is None
            or shares is None
            or debt is None
            or cash is None
            or rf_observation is None
            or beta is None
        ):
            return ValuationUnavailable(ticker=ticker, as_of=as_of, missing=missing.items)

        risk_free = SourcedInput(
            name="risk_free_rate",
            value=rf_observation.value / 100,
            source="fred",
            note=f"FRED DGS10 on {rf_observation.date.isoformat()} "
            f"({rf_observation.value:.2f}%), converted to a decimal.",
        )
        cost_of_debt = self._cost_of_debt(facts, revenue, fy0, debt, risk_free, assumptions)

        price = SourcedInput(
            name="price",
            value=price_bar.close,
            source="market",
            note=f"As-traded close on {price_bar.date.isoformat()} (yfinance, un-adjusted "
            "for splits after that date; ADR-0021).",
        )
        market_cap = _computed(
            "market_cap",
            market_capitalisation(price=price.value, shares_outstanding=shares.value),
            list(shares.facts),
        )
        nd = _computed(
            "net_debt",
            net_debt(total_debt=debt.value, cash=cash.value),
            list(debt.facts) + list(cash.facts),
        )
        return ValuationInputs(
            ticker=ticker,
            as_of=as_of,
            fiscal_year_end=fy0,
            revenue_history=revenue_history,
            base_revenue=_xbrl_input("base_revenue", revenue[fy0]),
            operating_margin=operating_margin,
            incremental_investment_rate=iir,
            price=price,
            price_bar=price_bar,
            shares_outstanding=shares,
            market_cap=market_cap,
            total_debt=debt,
            cash=cash,
            net_debt=nd,
            risk_free_rate=risk_free,
            risk_free_observation=rf_observation,
            beta=beta,
            cost_of_debt=cost_of_debt,
            tax_rate=SourcedInput(
                name="tax_rate", value=settings.valuation_tax_rate, source="config"
            ),
            equity_risk_premium=SourcedInput(
                name="equity_risk_premium",
                value=settings.valuation_equity_risk_premium,
                source="config",
            ),
            assumptions=assumptions,
            xbrl_raw=facts_result.raw,
        )

    # ---- Individual inputs ---------------------------------------------

    def _incremental_investment_rate(
        self,
        facts: Sequence[XBRLFact],
        revenue: dict[date, XBRLFact],
        ends: list[date | None],
        missing: _Missing,
        assumptions: list[str],
    ) -> SourcedInput | None:
        """Σ3y(capex − D&A) + ΔNWC over 3 years, per $ of 3-year revenue
        growth. Falls back to the configured placeholder only when revenue
        didn't grow (the ratio is undefined then)."""
        fy0, fy3 = ends[0], ends[3]
        investing_years = ends[:3]
        if fy3 is None or any(e is None for e in investing_years):
            missing.add("revenue for 4 consecutive fiscal years (to measure 3 years of growth)")
            return None
        capex_series = annual_series(facts, VALUATION_CONCEPTS["capex"])
        dna_series = annual_series(facts, VALUATION_CONCEPTS["depreciation"])
        years = [e for e in investing_years if e is not None]
        if not all(e in capex_series for e in years):
            missing.add("capital expenditure for each of the last 3 fiscal years")
            return None
        if not all(e in dna_series for e in years):
            missing.add("depreciation & amortization for each of the last 3 fiscal years")
            return None
        assert fy0 is not None
        revenue_change = revenue[fy0].value - revenue[fy3].value
        cited = (
            [capex_series[e] for e in years]
            + [dna_series[e] for e in years]
            + [revenue[fy0], revenue[fy3]]
        )

        if revenue_change <= 0:
            value = self._settings.valuation_default_incremental_investment_rate
            assumptions.append(
                f"ASSUMED incremental investment rate {value:.2f}: revenue fell over the last "
                "3 fiscal years, so the historical rate is undefined. The implied growth is "
                "illustrative."
            )
            return SourcedInput(
                name="incremental_investment_rate",
                value=value,
                source="assumed",
                facts=cited,
                note="Placeholder; revenue did not grow over the lookback.",
            )

        nwc_now = operating_working_capital(facts, fy0)
        nwc_then = operating_working_capital(facts, fy3)
        nwc_terms: list[Term] = []
        if nwc_now is not None and nwc_then is not None:
            increase_in_nwc = nwc_now[0] - nwc_then[0]
            nwc_terms = nwc_now[1] + [(-c, f) for c, f in nwc_then[1]]
            cited += [f for _, f in nwc_terms]
            nwc_note = "Includes the 3-year change in operating working capital."
        else:
            increase_in_nwc = 0.0
            nwc_note = "Working-capital change EXCLUDED (balance-sheet facts missing)."
            assumptions.append(
                "Incremental investment rate excludes working capital: current assets, current "
                "liabilities or cash were missing at a fiscal-year end."
            )
        result = incremental_investment_rate(
            capex=sum(capex_series[e].value for e in years),
            depreciation=sum(dna_series[e].value for e in years),
            increase_in_working_capital=increase_in_nwc,
            revenue_change=revenue_change,
        )
        return _computed(
            "incremental_investment_rate",
            result,
            cited,
            note=nwc_note,
            param_terms={
                "capex": [(1.0, capex_series[e]) for e in years],
                "depreciation": [(1.0, dna_series[e]) for e in years],
                "increase_in_working_capital": nwc_terms,
                "revenue_change": [(1.0, revenue[fy0]), (-1.0, revenue[fy3])],
            },
        )

    def _shares(
        self,
        facts: Sequence[XBRLFact],
        as_of: date,
        price_bar: PriceBar | None,
        splits: list[StockSplit],
        missing: _Missing,
    ) -> SourcedInput | None:
        """The latest reported share count, restated to the price date's
        share basis. WHY: a count reported before a split, multiplied by an
        as-traded price after it, is off by the split ratio (ADR-0021)."""
        fact = latest_instant(
            facts,
            VALUATION_CONCEPTS["shares"],
            as_of=as_of,
            max_age_days=self._settings.valuation_max_fact_age_days,
            unit=_SHARES,
        )
        if fact is None:
            missing.add(
                f"shares outstanding reported within {self._settings.valuation_max_fact_age_days}"
                " days of as_of"
            )
            return None
        if price_bar is None:
            return SourcedInput(
                name="shares_outstanding",
                value=fact.value,
                source="xbrl",
                facts=[fact],
                terms=[(1.0, fact)],
            )
        factor = split_factor_between(splits, fact.period_end, price_bar.date)
        note = f"As reported for {fact.period_end.isoformat()}."
        if factor != 1.0:
            note += (
                f" Multiplied by {factor:g} for splits between then and the price date "
                f"{price_bar.date.isoformat()}."
            )
        return SourcedInput(
            name="shares_outstanding",
            value=fact.value * factor,
            source="xbrl" if factor == 1.0 else "computed",
            facts=[fact],
            terms=[(factor, fact)],
            note=note,
        )

    def _total_debt(
        self, facts: Sequence[XBRLFact], as_of: date, missing: _Missing
    ) -> SourcedInput | None:
        max_age = self._settings.valuation_max_fact_age_days
        total = latest_instant(
            facts, VALUATION_CONCEPTS["long_term_debt_total"], as_of=as_of, max_age_days=max_age
        )
        noncurrent = latest_instant(
            facts,
            VALUATION_CONCEPTS["long_term_debt_noncurrent"],
            as_of=as_of,
            max_age_days=max_age,
        )
        # WHY prefer the more recent of the two bases: some filers tag the
        # total only in 10-Ks but the noncurrent split every quarter.
        if total is not None and (noncurrent is None or total.period_end >= noncurrent.period_end):
            base_facts = [total]
            day = total.period_end
            base = total.value
        elif noncurrent is not None:
            day = noncurrent.period_end
            current = instant_on(facts, VALUATION_CONCEPTS["long_term_debt_current"], day)
            base_facts = [noncurrent] + ([current] if current else [])
            base = noncurrent.value + (current.value if current else 0.0)
        else:
            # WHY unavailable, not zero: an untagged debt figure can't be
            # told apart from no debt. Ford, with large real debt, tags
            # none of these, so assuming zero would be badly wrong.
            missing.add(f"long-term debt reported within {max_age} days of as_of")
            return None
        short = instant_on(facts, VALUATION_CONCEPTS["short_term_debt"], day)
        used = base_facts + ([short] if short else [])
        value = base + (short.value if short else 0.0)
        return SourcedInput(
            name="total_debt",
            value=value,
            source="xbrl" if len(used) == 1 else "computed",
            facts=used,
            terms=[(1.0, f) for f in used],
            note=f"Book debt as of {day.isoformat()}: " + " + ".join(f.concept for f in used) + ".",
        )

    def _cash(
        self, facts: Sequence[XBRLFact], as_of: date, missing: _Missing
    ) -> SourcedInput | None:
        fact = latest_instant(
            facts,
            VALUATION_CONCEPTS["cash"],
            as_of=as_of,
            max_age_days=self._settings.valuation_max_fact_age_days,
        )
        if fact is None:
            missing.add("cash and cash equivalents reported within the staleness limit")
            return None
        securities = instant_on(
            facts, VALUATION_CONCEPTS["marketable_securities_current"], fact.period_end
        )
        used = [fact] + ([securities] if securities else [])
        return SourcedInput(
            name="cash",
            value=fact.value + (securities.value if securities else 0.0),
            source="xbrl" if securities is None else "computed",
            facts=used,
            terms=[(1.0, f) for f in used],
            note=f"As of {fact.period_end.isoformat()}: "
            + " + ".join(f.concept for f in used)
            + ".",
        )

    def _cost_of_debt(
        self,
        facts: Sequence[XBRLFact],
        revenue: dict[date, XBRLFact],
        fy0: date,
        debt: SourcedInput,
        risk_free: SourcedInput,
        assumptions: list[str],
    ) -> SourcedInput:
        interest = annual_series(facts, VALUATION_CONCEPTS["interest_expense"]).get(fy0)
        if interest is not None and debt.value > 0:
            try:
                result = implied_cost_of_debt(
                    interest_expense=interest.value, total_debt=debt.value
                )
            except ValuationInputError:
                pass
            else:
                assumptions.append(
                    "Cost of debt = last fiscal year's interest expense / current book debt "
                    "(historical average rate, not today's market yield)."
                )
                return _computed(
                    "cost_of_debt",
                    result,
                    [interest, *debt.facts],
                    param_terms={
                        "interest_expense": [(1.0, interest)],
                        "total_debt": list(debt.terms),
                    },
                )
        spread = self._settings.valuation_default_credit_spread
        value = risk_free.value + spread
        assumptions.append(
            f"ASSUMED cost of debt {value:.2%} = risk-free rate + {spread:.2%}: interest "
            "expense or debt was unavailable for the latest fiscal year."
        )
        return SourcedInput(name="cost_of_debt", value=value, source="assumed")

    async def _beta(self, ticker: str, as_of: date, missing: _Missing) -> SourcedInput | None:
        settings = self._settings
        lookback = round(365.25 * settings.valuation_beta_lookback_years) + 31
        series = {}
        for symbol in (ticker, settings.valuation_beta_market_ticker):
            bars = await self._prices.get_price_history(
                symbol, as_of, lookback_days=lookback, adjustment="total_return"
            )
            if isinstance(bars, DataUnavailable):
                missing.add(f"price history for beta ({symbol}): {bars.reason}")
                return None
            series[symbol] = monthly_returns([(b.date, b.close) for b in bars])
        try:
            result = ols_beta(
                stock_returns=series[ticker],
                market_returns=series[settings.valuation_beta_market_ticker],
                min_observations=settings.valuation_beta_min_observations,
            )
        except ValuationInputError as exc:
            missing.add(f"beta: {exc}")
            return None
        return SourcedInput(
            name="beta",
            value=result.value,
            source="computed",
            calculation=result,
            note=f"OLS on {int(result.inputs['observations'])} monthly total returns vs "
            f"{settings.valuation_beta_market_ticker}, {settings.valuation_beta_lookback_years}"
            " years to as_of.",
        )
