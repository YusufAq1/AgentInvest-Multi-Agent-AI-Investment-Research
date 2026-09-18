"""The in-memory, per-run Evidence Store.

WHY in-memory, not Postgres-backed (CLAUDE.md §14's evidence/claims/
claim_evidence tables): Phase 0/1 deliberately deferred writing to Postgres
until something actually needs cross-run durability. Nothing in Phase 2
needs a claim or evidence row to outlive one process — this store holds
everything for the duration of one run, then the caller (a script, later an
API handler) reads out `.claims()`/`.dropped_claims()` for the report. See
docs/adr/0012 for the full reasoning.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from backend.evidence.errors import (
    EvidenceNotFoundError,
    EvidenceRunMismatchError,
    LookAheadEvidenceError,
)
from backend.evidence.models import Claim, DroppedClaim, Evidence


class EvidenceStore:
    """Holds every Evidence/Claim for one research run.

    Two enforcement responsibilities live here, both from CLAUDE.md §6:
    rule 2 ("every evidence_id must resolve to a row that exists") via
    `resolve`/`add_claim`, and a look-ahead guard (C3) on `add_evidence`.
    Rule 4's retry-then-drop control flow does NOT live here — see
    `backend/agents/financial.py` and `ClaudeClient.call_structured` for
    where the two halves of that (schema-level vs. domain-level failure)
    actually live; this store is the thing they both check against.
    """

    def __init__(self, run_id: UUID, as_of: date) -> None:
        self._run_id = run_id
        self._as_of = as_of
        self._evidence: dict[UUID, Evidence] = {}
        self._claims: dict[UUID, Claim] = {}
        self._dropped: list[DroppedClaim] = []

    @property
    def run_id(self) -> UUID:
        return self._run_id

    @property
    def as_of(self) -> date:
        return self._as_of

    def add_evidence(self, evidence: Evidence) -> None:
        """Raises EvidenceRunMismatchError / LookAheadEvidenceError rather
        than silently accepting evidence that couldn't belong to this run.
        """
        if evidence.run_id != self._run_id:
            raise EvidenceRunMismatchError(
                f"Evidence {evidence.id} has run_id={evidence.run_id}, "
                f"but this store is for run_id={self._run_id}"
            )
        if evidence.published_at > self._as_of:
            raise LookAheadEvidenceError(
                f"Evidence {evidence.id} published_at={evidence.published_at} "
                f"is after as_of={self._as_of}"
            )
        self._evidence[evidence.id] = evidence

    def get_evidence(self, evidence_id: UUID) -> Evidence:
        """Raises EvidenceNotFoundError if `evidence_id` isn't stored."""
        try:
            return self._evidence[evidence_id]
        except KeyError:
            raise EvidenceNotFoundError(f"No evidence found for id {evidence_id}") from None

    def resolve(self, evidence_ids: Sequence[UUID]) -> list[Evidence]:
        """CLAUDE.md §6 rule 2, in one place — every other caller that
        needs "does this id exist" (add_claim, validation.py's computed-
        evidence recompute path) calls this, never re-implements the
        lookup. Raises EvidenceNotFoundError naming the first id that
        doesn't resolve.
        """
        return [self.get_evidence(evidence_id) for evidence_id in evidence_ids]

    def all_evidence(self, *, source_type: str | None = None) -> list[Evidence]:
        """The context set an agent shows Claude — every evidence row in
        this run, optionally filtered to one source_type.
        """
        values = list(self._evidence.values())
        if source_type is not None:
            values = [e for e in values if e.source_type == source_type]
        return values

    def add_claim(self, claim: Claim) -> None:
        """The single authoritative gate before a Claim is considered part
        of this run. Calls `resolve` — NOT redundant with Claim's own
        Pydantic validator, which can only enforce "non-empty for
        claim_type=evidence" at construction time (it has no store to
        check ids against). Raises EvidenceNotFoundError if any cited id
        doesn't resolve.
        """
        self.resolve(claim.evidence_ids)
        self._claims[claim.id] = claim

    def record_dropped_claim(self, *, agent: str, raw_input: dict[str, Any], reason: str) -> None:
        """CLAUDE.md §6 rule 4: "record the drop" — never a silent discard."""
        self._dropped.append(
            DroppedClaim(
                agent=agent, raw_input=raw_input, reason=reason, dropped_at=datetime.now(UTC)
            )
        )

    def claims(self) -> list[Claim]:
        return list(self._claims.values())

    def dropped_claims(self) -> list[DroppedClaim]:
        return list(self._dropped)
