"""Turn a ValuationResult into Evidence rows that CI can recompute.

Every number the Valuation Agent might cite becomes a row:
  - `xbrl_fact`: each XBRL fact any input used, quoting its exact raw
    companyfacts entry (same rows the Financial Agent builds)
  - `price_series`: the as-traded close and the beta
  - `macro`: the 10-year Treasury yield
  - `computed`: operating margin, investment rate, market cap, net debt,
    target EV, cost of debt, cost of equity, WACC, the break-even
    investment rate, the implied growth (or "no solution"), and the
    historical revenue CAGRs

Each computed row's `location` says how to recompute it:
`input_evidence_ids` maps every parameter to one row id or to signed
`[coefficient, row_id]` pairs (a 3-year capex sum, +FY0 − FY3, shares ×
split factor). `constant_inputs` holds only the configured assumptions the
registry allows. backend/evidence/validation.py recomputes each row from
the rows it cites and fails if the value doesn't match, so no valuation
number can enter a claim unless it reproduces from source data.

Pure: no I/O, no LLM.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel

from backend.agents.valuation_compute import ValuationResult
from backend.agents.valuation_inputs import SourcedInput, Term
from backend.agents.xbrl_facts import FactKey, fact_key, make_xbrl_fact_evidence
from backend.calc.dcf import ImpliedGrowth
from backend.core.config import Settings
from backend.data.models import XBRLFact
from backend.evidence.market_quotes import render_market_quote
from backend.evidence.models import Evidence

Reference = str | list[list[Any]]


class ValuationEvidence(BaseModel):
    """All rows to store, plus the headline rows shown to Claude by name.

    WHY only headline rows go into the prompt: a valuation cites about 40
    XBRL facts. Claude needs to cite the conclusions (implied growth, CAGR,
    WACC), and those rows already cite the facts. Showing every fact would
    multiply input tokens for nothing (CLAUDE.md §5)."""

    rows: list[Evidence]
    headline: dict[str, Evidence]


class _Builder:
    def __init__(self, result: ValuationResult, run_id: UUID, settings: Settings) -> None:
        self.result = result
        self.inputs = result.inputs
        self.run_id = run_id
        self.settings = settings
        self.rows: list[Evidence] = []
        self._fact_rows: dict[FactKey, Evidence] = {}
        self._by_id: dict[str, Evidence] = {}

    # ---- primitives ----------------------------------------------------

    def _add(self, evidence: Evidence) -> Evidence:
        self.rows.append(evidence)
        self._by_id[str(evidence.id)] = evidence
        return evidence

    def fact_row(self, fact: XBRLFact) -> Evidence:
        key = fact_key(fact)
        if key not in self._fact_rows:
            self._fact_rows[key] = self._add(
                make_xbrl_fact_evidence(fact, self.inputs.xbrl_raw, self.run_id)
            )
        return self._fact_rows[key]

    def terms_ref(self, terms: list[Term]) -> list[list[Any]]:
        return [[coefficient, str(self.fact_row(fact).id)] for coefficient, fact in terms]

    def market_row(
        self, source_type: str, source_ref: str, published_at: date, location: dict[str, Any]
    ) -> Evidence:
        return self._add(
            Evidence(
                id=uuid4(),
                run_id=self.run_id,
                source_type=source_type,  # type: ignore[arg-type]
                source_ref=source_ref,
                published_at=published_at,
                retrieved_at=datetime.now(UTC),
                quote=render_market_quote(location),
                location=location,
            )
        )

    def computed_row(
        self,
        ratio_name: str,
        value: float,
        formula: str,
        refs: dict[str, Reference],
        constants: dict[str, float] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Evidence:
        cited_ids = [ref for ref in refs.values() if isinstance(ref, str)] + [
            pair[1] for ref in refs.values() if isinstance(ref, list) for pair in ref
        ]
        cited = [self._by_id[i] for i in cited_ids]
        # A derived number can't have been published before the last of
        # its inputs existed.
        published_at = max((ev.published_at for ev in cited), default=self.inputs.as_of)
        return self._add(
            Evidence(
                id=uuid4(),
                run_id=self.run_id,
                source_type="computed",
                source_ref=f"valuation:{self.inputs.ticker}:{self.inputs.as_of.isoformat()}",
                published_at=published_at,
                retrieved_at=datetime.now(UTC),
                quote=f"{ratio_name} = {formula} = {value:.6f}",
                location={
                    "ratio_name": ratio_name,
                    "input_evidence_ids": refs,
                    "constant_inputs": constants or {},
                    "value": value,
                    **(extra or {}),
                },
            )
        )

    def computed_input(self, item: SourcedInput, ratio_name: str) -> Evidence:
        assert item.calculation is not None
        return self.computed_row(
            ratio_name,
            item.value,
            item.calculation.formula,
            {param: self.terms_ref(terms) for param, terms in item.param_terms.items()},
        )

    # ---- the valuation chain -------------------------------------------

    def build(self) -> ValuationEvidence:
        inputs, result, settings = self.inputs, self.result, self.settings
        ticker, as_of = inputs.ticker, inputs.as_of

        bar = inputs.price_bar
        price = self.market_row(
            "price_series",
            f"yfinance:{ticker}:{bar.date.isoformat()}",
            bar.date,
            {
                "kind": "as_traded_close",
                "ticker": ticker,
                "date": bar.date.isoformat(),
                "value": bar.close,
            },
        )
        beta_calc = inputs.beta.calculation
        assert beta_calc is not None
        beta = self.market_row(
            "price_series",
            f"yfinance:{ticker}:beta:{as_of.isoformat()}",
            bar.date,
            {
                "kind": "beta",
                "ticker": ticker,
                "market": settings.valuation_beta_market_ticker,
                "observations": beta_calc.inputs["observations"],
                "as_of": as_of.isoformat(),
                "value": beta_calc.value,
                "covariance": beta_calc.inputs["covariance"],
                "market_variance": beta_calc.inputs["market_variance"],
            },
        )
        obs = inputs.risk_free_observation
        risk_free = self.market_row(
            "macro",
            f"FRED:{obs.series_id}:{obs.date.isoformat()}",
            obs.date,
            {
                "kind": "risk_free_rate",
                "series_id": obs.series_id,
                "date": obs.date.isoformat(),
                "as_of": as_of.isoformat(),
                "percent": obs.value,
                "value": inputs.risk_free_rate.value,
            },
        )

        margin = self.computed_input(inputs.operating_margin, "average_operating_margin")
        iir_input = inputs.incremental_investment_rate
        if iir_input.source == "assumed":
            iir = self.computed_row(
                "assumed_incremental_investment_rate",
                iir_input.value,
                "configured placeholder (ASSUMED)",
                {},
                {"value": iir_input.value},
            )
        else:
            iir = self.computed_input(iir_input, "incremental_investment_rate")

        market_cap = self.computed_row(
            "market_capitalisation",
            inputs.market_cap.value,
            "price * shares_outstanding",
            {
                "price": str(price.id),
                "shares_outstanding": self.terms_ref(inputs.shares_outstanding.terms),
            },
        )
        net_debt = self.computed_row(
            "net_debt",
            inputs.net_debt.value,
            "total_debt - cash",
            {
                "total_debt": self.terms_ref(inputs.total_debt.terms),
                "cash": self.terms_ref(inputs.cash.terms),
            },
        )
        target = self.computed_row(
            "target_enterprise_value",
            result.target_enterprise_value.value,
            "market_cap + net_debt",
            {"market_cap": str(market_cap.id), "net_debt": str(net_debt.id)},
        )

        cod_input = inputs.cost_of_debt
        if cod_input.source == "assumed":
            cost_of_debt = self.computed_row(
                "assumed_cost_of_debt",
                cod_input.value,
                "risk_free_rate + credit_spread (ASSUMED)",
                {"risk_free_rate": str(risk_free.id)},
                {"credit_spread": settings.valuation_default_credit_spread},
            )
        else:
            cost_of_debt = self.computed_input(cod_input, "implied_cost_of_debt")

        cost_of_equity = self.computed_row(
            "cost_of_equity",
            result.cost_of_equity.value,
            result.cost_of_equity.formula,
            {"risk_free_rate": str(risk_free.id), "beta": str(beta.id)},
            {"equity_risk_premium": inputs.equity_risk_premium.value},
        )
        wacc = self.computed_row(
            "wacc",
            result.wacc.value,
            "(E/V) * cost_of_equity + (D/V) * cost_of_debt * (1 - tax_rate)",
            {
                "equity_value": str(market_cap.id),
                "debt_value": self.terms_ref(inputs.total_debt.terms),
                "cost_of_equity": str(cost_of_equity.id),
                "cost_of_debt": str(cost_of_debt.id),
            },
            {"tax_rate": inputs.tax_rate.value},
        )
        tax = {"tax_rate": inputs.tax_rate.value}
        break_even = self.computed_row(
            "value_neutral_investment_rate",
            (
                result.drivers.operating_margin
                * (1 - result.drivers.tax_rate)
                * (1 + result.drivers.wacc)
                / result.drivers.wacc
            ),
            "operating_margin * (1 - tax_rate) * (1 + wacc) / wacc",
            {"operating_margin": str(margin.id), "wacc": str(wacc.id)},
            tax,
        )

        solver_refs: dict[str, Reference] = {
            "base_revenue": self.terms_ref([(1.0, inputs.base_revenue.facts[0])]),
            "operating_margin": str(margin.id),
            "incremental_investment_rate": str(iir.id),
            "wacc": str(wacc.id),
            "target_enterprise_value": str(target.id),
        }
        solver_constants = {
            **tax,
            "horizon_years": float(result.drivers.horizon_years),
            "search_min": settings.valuation_growth_search_min,
            "search_max": settings.valuation_growth_search_max,
            "step": settings.valuation_growth_search_step,
            "tolerance": settings.valuation_growth_tolerance,
            "flat_tolerance": settings.valuation_flat_tolerance,
        }
        implied = result.implied_growth
        if isinstance(implied, ImpliedGrowth):
            implied_row = self.computed_row(
                "implied_revenue_growth",
                implied.growth,
                f"g such that EV(value drivers, g) = target EV, over "
                f"{result.drivers.horizon_years} years",
                solver_refs,
                solver_constants,
                {"growth_creates_value": implied.growth_creates_value},
            )
        else:
            implied_row = self.computed_row(
                "implied_growth_solutions",
                0.0,
                "number of growth rates in the search range that fit the price",
                solver_refs,
                solver_constants,
                {"reason": implied.reason},
            )

        headline: dict[str, Evidence] = {
            "implied_growth": implied_row,
            "value_neutral_investment_rate": break_even,
            "wacc": wacc,
            "cost_of_equity": cost_of_equity,
            "cost_of_debt": cost_of_debt,
            "beta": beta,
            "risk_free_rate": risk_free,
            "operating_margin": margin,
            "incremental_investment_rate": iir,
            "market_cap": market_cap,
            "net_debt": net_debt,
            "target_enterprise_value": target,
            "price": price,
        }
        history = inputs.revenue_history
        for years, cagr in sorted(result.historical_revenue_cagr.items()):
            headline[f"revenue_cagr_{years}y"] = self.computed_row(
                "revenue_cagr",
                cagr.value,
                cagr.formula,
                {
                    "revenue_start": self.terms_ref([(1.0, history[years].facts[0])]),
                    "revenue_end": self.terms_ref([(1.0, history[0].facts[0])]),
                },
                {"years": float(years)},
            )
        return ValuationEvidence(rows=self.rows, headline=headline)


def build_valuation_evidence(
    result: ValuationResult, run_id: UUID, settings: Settings
) -> ValuationEvidence:
    return _Builder(result, run_id, settings).build()
