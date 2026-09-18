"""The Evidence Store — AgentInvest's core anti-hallucination mechanism.

CLAUDE.md §6: every material claim an agent makes must carry an evidence_id
that resolves to a stored, verbatim-quoted source. Enforced here in code —
Pydantic validation at construction time (Claim.evidence_ids non-empty for
claim_type="evidence"), resolution against real stored evidence at add-time
(EvidenceStore.add_claim), and citation-validity checks that re-verify every
stored quote against its real source (validation.py) — never left to a
prompt asking the model nicely.

Modules:
    models.py       Evidence, Claim, DroppedClaim — the data shapes.
    errors.py       The exception hierarchy.
    store.py        EvidenceStore — in-memory, per-run (see docs/adr/0012).
    validation.py   Citation-validity checks (the CI-gate mechanism).
"""

from backend.evidence.errors import AgentInvestEvidenceError
from backend.evidence.models import Claim, DroppedClaim, Evidence

__all__ = ["AgentInvestEvidenceError", "Claim", "DroppedClaim", "Evidence"]
