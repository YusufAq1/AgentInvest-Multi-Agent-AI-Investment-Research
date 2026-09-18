"""Tests for backend.evidence.store.EvidenceStore."""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from backend.evidence.errors import (
    EvidenceNotFoundError,
    EvidenceRunMismatchError,
    LookAheadEvidenceError,
)
from backend.evidence.models import Claim, Evidence
from backend.evidence.store import EvidenceStore

RUN_ID = uuid4()
AS_OF = date(2024, 6, 30)


def _make_evidence(**overrides: object) -> Evidence:
    defaults: dict[str, object] = {
        "id": uuid4(),
        "run_id": RUN_ID,
        "source_type": "xbrl_fact",
        "source_ref": "acc-1",
        "published_at": date(2024, 2, 1),
        "retrieved_at": datetime.now(UTC),
        "quote": '{"val": 100}',
        "location": {"concept": "Revenues", "value": 100},
    }
    defaults.update(overrides)
    return Evidence(**defaults)  # type: ignore[arg-type]


def test_add_and_get_evidence_round_trips() -> None:
    store = EvidenceStore(run_id=RUN_ID, as_of=AS_OF)
    ev = _make_evidence()

    store.add_evidence(ev)

    assert store.get_evidence(ev.id) == ev


def test_add_evidence_rejects_wrong_run_id() -> None:
    store = EvidenceStore(run_id=RUN_ID, as_of=AS_OF)
    ev = _make_evidence(run_id=uuid4())

    with pytest.raises(EvidenceRunMismatchError):
        store.add_evidence(ev)


def test_add_evidence_rejects_look_ahead_published_at() -> None:
    store = EvidenceStore(run_id=RUN_ID, as_of=AS_OF)
    ev = _make_evidence(published_at=date(2024, 8, 1))  # after AS_OF

    with pytest.raises(LookAheadEvidenceError):
        store.add_evidence(ev)


def test_get_evidence_raises_for_unknown_id() -> None:
    store = EvidenceStore(run_id=RUN_ID, as_of=AS_OF)

    with pytest.raises(EvidenceNotFoundError):
        store.get_evidence(uuid4())


def test_resolve_raises_naming_missing_id() -> None:
    store = EvidenceStore(run_id=RUN_ID, as_of=AS_OF)
    ev = _make_evidence()
    store.add_evidence(ev)
    missing_id = uuid4()

    with pytest.raises(EvidenceNotFoundError):
        store.resolve([ev.id, missing_id])


def test_add_claim_rejects_claim_citing_unresolved_evidence() -> None:
    store = EvidenceStore(run_id=RUN_ID, as_of=AS_OF)
    # A Claim can be constructed with a well-formed evidence_id that simply
    # doesn't exist in this store — Pydantic's own validator can't catch
    # this (it has no store to check against); add_claim must.
    claim = Claim(
        id=uuid4(),
        agent="financial",
        statement="Revenue was $100",
        evidence_ids=[uuid4()],
        claim_type="evidence",
        materiality="high",
    )

    with pytest.raises(EvidenceNotFoundError):
        store.add_claim(claim)


def test_add_claim_succeeds_when_evidence_resolves() -> None:
    store = EvidenceStore(run_id=RUN_ID, as_of=AS_OF)
    ev = _make_evidence()
    store.add_evidence(ev)
    claim = Claim(
        id=uuid4(),
        agent="financial",
        statement="Revenue was $100",
        evidence_ids=[ev.id],
        claim_type="evidence",
        materiality="high",
    )

    store.add_claim(claim)

    assert store.claims() == [claim]


def test_record_dropped_claim_round_trips() -> None:
    store = EvidenceStore(run_id=RUN_ID, as_of=AS_OF)

    store.record_dropped_claim(
        agent="financial", raw_input={"claims": []}, reason="validation failed twice"
    )

    dropped = store.dropped_claims()
    assert len(dropped) == 1
    assert dropped[0].reason == "validation failed twice"


def test_all_evidence_filters_by_source_type() -> None:
    store = EvidenceStore(run_id=RUN_ID, as_of=AS_OF)
    fact_ev = _make_evidence(source_type="xbrl_fact")
    computed_ev = _make_evidence(
        source_type="computed", location={"ratio_name": "gross_margin", "value": 0.4}
    )
    store.add_evidence(fact_ev)
    store.add_evidence(computed_ev)

    assert store.all_evidence(source_type="computed") == [computed_ev]
    assert len(store.all_evidence()) == 2
