"""Exception hierarchy for backend.evidence."""

from backend.core.llm import AgentInvestError


class AgentInvestEvidenceError(AgentInvestError):
    """Base class for all backend.evidence exceptions."""


class EvidenceNotFoundError(AgentInvestEvidenceError):
    """An evidence_id doesn't resolve to any stored Evidence row — CLAUDE.md
    §6 rule 2, enforced in EvidenceStore.resolve/add_claim."""


class EvidenceRunMismatchError(AgentInvestEvidenceError):
    """Evidence.run_id doesn't match the store's own run_id — evidence from
    one run must never leak into another's claims."""


class LookAheadEvidenceError(AgentInvestEvidenceError):
    """Evidence.published_at is after the store's as_of date — CLAUDE.md C3,
    the look-ahead-bias guard, enforced at the Evidence Store boundary too,
    not just in backend/data."""


class CitationInvalidError(AgentInvestEvidenceError):
    """validation.py: the stored quote doesn't appear verbatim in its real
    source, or a computed value doesn't match its recomputed value — a
    fabricated (or buggy) citation, caught by CLAUDE.md §6 rule 3."""


class CitationValidationSetupError(AgentInvestEvidenceError):
    """validation.py: the caller didn't supply what this evidence's
    source_type needs to be checked at all (e.g. no xbrl_raw_payload for an
    xbrl_fact row). Distinct from CitationInvalidError so "we never actually
    checked this" is never confused with "we checked, and it's fine."""
