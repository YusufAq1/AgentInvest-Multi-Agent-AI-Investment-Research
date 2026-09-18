"""Tests for backend.evidence.models — the Pydantic-level half of
CLAUDE.md §6's enforcement (the store-level half is in test_store.py)."""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from backend.evidence.models import Claim
from pydantic import ValidationError


def test_evidence_type_claim_requires_evidence_ids() -> None:
    with pytest.raises(ValidationError):
        Claim(
            id=uuid4(),
            agent="financial",
            statement="Revenue grew 10%",
            evidence_ids=[],
            claim_type="evidence",
            materiality="high",
        )


@pytest.mark.parametrize("claim_type", ["inference", "assumption"])
def test_non_evidence_claim_does_not_require_evidence_ids(claim_type: str) -> None:
    claim = Claim(
        id=uuid4(),
        agent="financial",
        statement="Margin expansion suggests pricing power",
        evidence_ids=[],
        claim_type=claim_type,  # type: ignore[arg-type]
        materiality="medium",
    )
    assert claim.evidence_ids == []


def test_evidence_type_claim_with_evidence_ids_is_valid() -> None:
    claim = Claim(
        id=uuid4(),
        agent="financial",
        statement="Revenue was $1,000,000 for FY2023",
        evidence_ids=[uuid4()],
        claim_type="evidence",
        materiality="high",
    )
    assert len(claim.evidence_ids) == 1


def test_evidence_timestamps_accept_real_values() -> None:
    from backend.evidence.models import Evidence

    ev = Evidence(
        id=uuid4(),
        run_id=uuid4(),
        source_type="xbrl_fact",
        source_ref="0000320193-24-000006",
        published_at=date(2024, 2, 1),
        retrieved_at=datetime.now(UTC),
        quote='{"val":100}',
        location={"concept": "Revenues", "value": 100},
    )
    assert ev.location["value"] == 100
