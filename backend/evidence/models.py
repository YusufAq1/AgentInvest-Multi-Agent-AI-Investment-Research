"""Evidence/Claim models — CLAUDE.md §6's exact spec, with one typing
adjustment for strict mypy (see Evidence.location below).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, model_validator


class Evidence(BaseModel):
    """A verbatim passage from a real source, with provenance.

    Evidence is IMMUTABLE and always quotes source text verbatim. It is
    never paraphrased at storage time — paraphrase happens at render time
    so we can always verify the quote against the original.
    """

    id: UUID
    run_id: UUID
    source_type: Literal["sec_filing", "xbrl_fact", "news", "price_series", "macro", "computed"]
    source_ref: str
    published_at: date
    retrieved_at: datetime
    quote: str
    # WHY dict[str, Any], not bare `dict`: mypy --strict's
    # disallow_any_generics rejects an unparameterized `dict`. Kept as a
    # free-form mapping (not a typed/discriminated model) because the shape
    # genuinely differs by source_type — byte-offset spans for prose,
    # concept/unit/value for XBRL facts, and for "computed" evidence,
    # {"ratio_name", "input_evidence_ids": {param: evidence_id_str},
    # "value"} — that last shape is load-bearing for the anti-fabrication
    # recompute check in validation.py, not just descriptive metadata.
    location: dict[str, Any]


class Claim(BaseModel):
    """An assertion made by an agent. Cannot exist without support."""

    id: UUID
    agent: str
    statement: str
    evidence_ids: list[UUID]
    claim_type: Literal["evidence", "inference", "assumption"]
    materiality: Literal["high", "medium", "low"]

    @model_validator(mode="after")
    def _evidence_claims_require_evidence(self) -> Claim:
        # CLAUDE.md §6 enforcement rule 1: claim_type="evidence" requires
        # >=1 evidence_id, checked by a validator, not by asking nicely.
        # This is a NECESSARY but not SUFFICIENT check: it can only verify
        # the list is non-empty, since a bare Claim has no store to resolve
        # ids against — see EvidenceStore.add_claim for the check that the
        # ids actually resolve to real evidence.
        if self.claim_type == "evidence" and not self.evidence_ids:
            raise ValueError(
                "claim_type='evidence' requires at least one evidence_id (CLAUDE.md §6 rule 1)"
            )
        return self


class ClaimBatch(BaseModel):
    """The Financial Agent's structured-output shape: all of a run's claims
    in one tool call, for cost reasons (C2 — one Claude call beats N).

    WHY this means retry-then-drop applies to the whole batch, not one
    claim: see docs/adr/0012 for the documented tradeoff.
    """

    claims: list[Claim]


class DroppedClaim(BaseModel):
    """CLAUDE.md §6 rule 4's "record the drop" — a claim (or claim batch)
    that failed validation twice and was never added to the store.
    """

    agent: str
    raw_input: dict[str, Any]
    reason: str
    dropped_at: datetime
