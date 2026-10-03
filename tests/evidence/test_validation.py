"""Tests for backend.evidence.validation — the direct proof of Phase 2's
exit criterion: "CI fails if a fabricated citation is introduced
deliberately."
"""

import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from backend.evidence.errors import CitationInvalidError, CitationValidationSetupError
from backend.evidence.models import Evidence
from backend.evidence.validation import validate_all_evidence, validate_citation

RUN_ID = uuid4()

_RAW_PAYLOAD: dict[str, Any] = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "label": "Revenues",
                "units": {
                    "USD": [
                        {
                            "start": "2023-01-01",
                            "end": "2023-12-31",
                            "val": 1_000_000,
                            "accn": "acc-1",
                            "fy": 2023,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2024-02-01",
                        }
                    ]
                },
            }
        }
    },
}
_REAL_ENTRY = _RAW_PAYLOAD["facts"]["us-gaap"]["Revenues"]["units"]["USD"][0]


def _make_evidence(**overrides: object) -> Evidence:
    defaults: dict[str, object] = {
        "id": uuid4(),
        "run_id": RUN_ID,
        "source_type": "xbrl_fact",
        "source_ref": "acc-1",
        "published_at": date(2024, 2, 1),
        "retrieved_at": datetime.now(UTC),
        "quote": json.dumps(_REAL_ENTRY, sort_keys=True, separators=(",", ":")),
        "location": {"concept": "Revenues", "value": 1_000_000},
    }
    defaults.update(overrides)
    return Evidence(**defaults)  # type: ignore[arg-type]


# ---- sec_filing / news: prose substring containment ----


def test_text_containment_valid_quote_passes() -> None:
    ev = _make_evidence(source_type="sec_filing", quote="the company faces significant risks")
    document = "Item 1A. Risk Factors: the company faces significant risks."
    validate_citation(ev, source_document=document)


def test_text_containment_fabricated_quote_is_rejected() -> None:
    # THE load-bearing test: a quote that was never in the real document.
    ev = _make_evidence(source_type="sec_filing", quote="revenue will triple next year")
    with pytest.raises(CitationInvalidError):
        validate_citation(ev, source_document="Item 1A. Risk Factors: the company faces risks.")


def test_text_containment_missing_document_raises_setup_error() -> None:
    ev = _make_evidence(source_type="news", quote="anything")
    with pytest.raises(CitationValidationSetupError):
        validate_citation(ev, source_document=None)


# ---- xbrl_fact: containment against the raw companyfacts payload ----


def test_xbrl_fact_valid_quote_passes() -> None:
    ev = _make_evidence()  # quote built from the real entry, matching fixture
    validate_citation(ev, xbrl_raw_payload=_RAW_PAYLOAD)


def test_xbrl_fact_fabricated_quote_is_rejected() -> None:
    # A quote that was never in the raw payload at all — the direct proof
    # of the exit criterion for structured/XBRL evidence.
    fabricated = json.dumps(
        {**_REAL_ENTRY, "val": 999_999_999}, sort_keys=True, separators=(",", ":")
    )
    ev = _make_evidence(quote=fabricated)
    with pytest.raises(CitationInvalidError):
        validate_citation(ev, xbrl_raw_payload=_RAW_PAYLOAD)


def test_xbrl_fact_missing_payload_raises_setup_error() -> None:
    ev = _make_evidence()
    with pytest.raises(CitationValidationSetupError):
        validate_citation(ev, xbrl_raw_payload=None)


# ---- computed: recompute-and-compare against resolved input evidence ----


def _resolver_for(
    evidence_by_id: dict[str, Evidence],
) -> Callable[[Sequence[UUID]], list[Evidence]]:
    def resolve(evidence_ids: Sequence[UUID]) -> list[Evidence]:
        return [evidence_by_id[str(eid)] for eid in evidence_ids]

    return resolve


def test_computed_valid_recompute_passes() -> None:
    revenue_ev = _make_evidence(location={"concept": "Revenues", "value": 1_000_000})
    cogs_ev = _make_evidence(id=uuid4(), location={"concept": "CostOfRevenue", "value": 600_000})
    computed_ev = _make_evidence(
        source_type="computed",
        quote="gross_margin = (revenue - cogs) / revenue = 0.400000",
        location={
            "ratio_name": "gross_margin",
            "input_evidence_ids": {"revenue": str(revenue_ev.id), "cogs": str(cogs_ev.id)},
            "value": 0.4,
        },
    )
    resolver = _resolver_for({str(revenue_ev.id): revenue_ev, str(cogs_ev.id): cogs_ev})

    validate_citation(computed_ev, resolve=resolver)


def test_computed_mismatched_value_is_rejected() -> None:
    revenue_ev = _make_evidence(location={"concept": "Revenues", "value": 1_000_000})
    cogs_ev = _make_evidence(id=uuid4(), location={"concept": "CostOfRevenue", "value": 600_000})
    # The stored value (0.9) doesn't match what recomputing from the real
    # inputs actually yields (0.4) — a fabricated/buggy computed claim.
    computed_ev = _make_evidence(
        source_type="computed",
        quote="gross_margin = (revenue - cogs) / revenue = 0.900000",
        location={
            "ratio_name": "gross_margin",
            "input_evidence_ids": {"revenue": str(revenue_ev.id), "cogs": str(cogs_ev.id)},
            "value": 0.9,
        },
    )
    resolver = _resolver_for({str(revenue_ev.id): revenue_ev, str(cogs_ev.id): cogs_ev})

    with pytest.raises(CitationInvalidError):
        validate_citation(computed_ev, resolve=resolver)


def test_computed_citing_non_xbrl_fact_input_is_rejected() -> None:
    other_computed = _make_evidence(
        source_type="computed", location={"ratio_name": "net_margin", "value": 0.1}
    )
    other_id = str(other_computed.id)
    computed_ev = _make_evidence(
        source_type="computed",
        quote="gross_margin = ... = 0.400000",
        location={
            "ratio_name": "gross_margin",
            "input_evidence_ids": {"revenue": other_id, "cogs": other_id},
            "value": 0.4,
        },
    )
    resolver = _resolver_for({str(other_computed.id): other_computed})

    with pytest.raises(CitationInvalidError):
        validate_citation(computed_ev, resolve=resolver)


def test_computed_missing_resolver_raises_setup_error() -> None:
    ev = _make_evidence(
        source_type="computed",
        location={"ratio_name": "gross_margin", "input_evidence_ids": {}, "value": 0.4},
    )
    with pytest.raises(CitationValidationSetupError):
        validate_citation(ev, resolve=None)


# ---- validate_all_evidence: sweep, don't stop at the first failure ----


def test_validate_all_evidence_collects_failures_without_raising() -> None:
    good = _make_evidence()
    bad = _make_evidence(id=uuid4(), quote="totally fabricated quote text")

    failures = validate_all_evidence([good, bad], xbrl_raw_payloads={"acc-1": _RAW_PAYLOAD})

    assert len(failures) == 1
    assert failures[0].evidence.id == bad.id


def test_validate_all_evidence_empty_when_all_valid() -> None:
    good = _make_evidence()

    failures = validate_all_evidence([good], xbrl_raw_payloads={"acc-1": _RAW_PAYLOAD})

    assert failures == []


# ---- Phase 6: linear combinations, declared constants, market data ----


def _value_row(source_type: str, value: float) -> Evidence:
    return _make_evidence(source_type=source_type, location={"value": value})


def test_computed_input_can_be_a_signed_combination_of_cited_rows() -> None:
    """net_debt(total_debt = 400 + 50, cash = 150 + 50) = 450 − 200 = 250."""
    ltd, cp, cash, sec = (_value_row("xbrl_fact", v) for v in (400, 50, 150, 50))
    row = _make_evidence(
        source_type="computed",
        location={
            "ratio_name": "net_debt",
            "input_evidence_ids": {
                "total_debt": [[1.0, str(ltd.id)], [1.0, str(cp.id)]],
                "cash": [[1.0, str(cash.id)], [1.0, str(sec.id)]],
            },
            "value": 250.0,
        },
    )
    validate_citation(row, resolve=_resolver_for({str(e.id): e for e in (ltd, cp, cash, sec)}))


def test_undeclared_constant_inputs_are_rejected() -> None:
    """net_debt allows no constants, so a row can't self-report its cash."""
    ltd = _value_row("xbrl_fact", 400)
    row = _make_evidence(
        source_type="computed",
        location={
            "ratio_name": "net_debt",
            "input_evidence_ids": {"total_debt": str(ltd.id)},
            "constant_inputs": {"cash": 100.0},
            "value": 300.0,
        },
    )
    with pytest.raises(CitationInvalidError, match="constants"):
        validate_citation(row, resolve=_resolver_for({str(ltd.id): ltd}))


def test_valuation_rows_may_chain_but_ratios_may_not() -> None:
    """cost_of_equity may cite macro and price rows; ERP is a declared
    constant. 0.04 + 1.2 × 0.05 = 0.10."""
    rf, beta = _value_row("macro", 0.04), _value_row("price_series", 1.2)
    row = _make_evidence(
        source_type="computed",
        location={
            "ratio_name": "cost_of_equity",
            "input_evidence_ids": {"risk_free_rate": str(rf.id), "beta": str(beta.id)},
            "constant_inputs": {"equity_risk_premium": 0.05},
            "value": 0.10,
        },
    )
    validate_citation(row, resolve=_resolver_for({str(rf.id): rf, str(beta.id): beta}))


def _market_row(location: dict[str, Any]) -> Evidence:
    from backend.evidence.market_quotes import render_market_quote

    return _make_evidence(
        source_type="price_series", quote=render_market_quote(location), location=location
    )


_BETA = {
    "kind": "beta",
    "ticker": "TICK",
    "market": "SPY",
    "observations": 60.0,
    "as_of": "2024-06-30",
    "value": 1.5,
    "covariance": 0.0006,
    "market_variance": 0.0004,
}


def test_market_datum_with_canonical_quote_passes() -> None:
    validate_citation(_market_row(_BETA))


def test_market_datum_whose_value_was_edited_fails() -> None:
    row = _market_row(_BETA)
    edited = row.model_copy(update={"location": {**_BETA, "value": 2.0}})
    with pytest.raises(CitationInvalidError, match="canonical"):
        validate_citation(edited)


def test_beta_inconsistent_with_its_moments_fails() -> None:
    with pytest.raises(CitationInvalidError, match="covariance"):
        validate_citation(_market_row({**_BETA, "value": 1.7}))


def test_risk_free_decimal_must_match_its_percent() -> None:
    location = {
        "kind": "risk_free_rate",
        "series_id": "DGS10",
        "date": "2024-06-27",
        "as_of": "2024-06-30",
        "percent": 4.29,
        "value": 0.0429,
    }
    validate_citation(_market_row(location))
    with pytest.raises(CitationInvalidError, match="percent"):
        validate_citation(_market_row({**location, "value": 0.05}))


def test_market_datum_of_unknown_kind_fails() -> None:
    row = _make_evidence(source_type="macro", quote="x", location={"kind": "mystery"})
    with pytest.raises(CitationInvalidError):
        validate_citation(row)
