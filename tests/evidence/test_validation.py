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
