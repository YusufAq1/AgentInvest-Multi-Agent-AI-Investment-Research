"""Citation-validity checks — CLAUDE.md §6 rule 3 and §12's "citation
validity" deterministic test, the mechanism this phase's exit criterion
("CI fails if a fabricated citation is introduced deliberately") tests
directly.

Two paths by `source_type`, because "verbatim in the stored source" means
something different for prose than for a derived number:
  - sec_filing / news: classic substring containment against stored text.
  - xbrl_fact: substring containment against the raw companyfacts JSON —
    still a real containment check, just on structured JSON instead of
    prose (see `_validate_xbrl_fact`'s docstring for why this is genuine,
    not a trick).
  - computed: no containment check at all (a ratio never existed verbatim
    in any upstream document) — instead, recompute the value from its
    cited input evidence and assert it matches.

Deliberately run at CI/test time, not inline in an agent's hot path: in
Phase 2, Claude never produces a quote (it only cites evidence_ids Python
already built), so an inline check would only catch a bug in the agent's
own quote-construction code — already covered directly by that code's unit
tests. Inline validation becomes load-bearing once a later agent asks an
LLM to produce a quoted span itself.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from uuid import UUID

from backend.calc.ratios import RATIO_FUNCS
from backend.evidence.errors import CitationInvalidError, CitationValidationSetupError
from backend.evidence.models import Evidence

Resolver = Callable[[Sequence[UUID]], list[Evidence]]

_JSON_DUMPS_KWARGS: dict[str, Any] = {"sort_keys": True, "separators": (",", ":")}


class CitationFailure:
    """One row that failed `validate_citation`, for `validate_all_evidence`'s
    batch sweep — a plain container, not a raised exception, since the
    sweep must keep going past the first bad row."""

    def __init__(self, evidence: Evidence, error: Exception) -> None:
        self.evidence = evidence
        self.error = error

    def __repr__(self) -> str:
        return f"CitationFailure(evidence_id={self.evidence.id}, error={self.error!r})"


def validate_citation(
    evidence: Evidence,
    *,
    source_document: str | None = None,
    xbrl_raw_payload: Mapping[str, Any] | None = None,
    resolve: Resolver | None = None,
) -> None:
    """Raises `CitationInvalidError` if `evidence`'s citation is fabricated
    (or buggy), or `CitationValidationSetupError` if the caller didn't
    supply what this evidence's `source_type` needs to be checked at all.
    Returns None (no exception) if the citation is valid.
    """
    if evidence.source_type in ("sec_filing", "news"):
        _validate_text_containment(evidence, source_document=source_document)
    elif evidence.source_type == "xbrl_fact":
        _validate_xbrl_fact(evidence, xbrl_raw_payload=xbrl_raw_payload)
    elif evidence.source_type == "computed":
        _validate_computed(evidence, resolve=resolve)
    else:
        raise CitationValidationSetupError(
            f"validate_citation has no check implemented yet for "
            f"source_type={evidence.source_type!r}"
        )


def _validate_text_containment(evidence: Evidence, *, source_document: str | None) -> None:
    if source_document is None:
        raise CitationValidationSetupError(
            f"evidence {evidence.id} is source_type={evidence.source_type!r}, "
            "which requires source_document"
        )
    if evidence.quote not in source_document:
        raise CitationInvalidError(
            f"evidence {evidence.id}: quote does not appear verbatim in the stored document"
        )


def _validate_xbrl_fact(evidence: Evidence, *, xbrl_raw_payload: Mapping[str, Any] | None) -> None:
    """The Financial Agent builds `quote` as
    `json.dumps(entry, sort_keys=True, separators=(",", ":"))`, where
    `entry` is the exact raw fact dict from the companyfacts payload. Here
    we re-serialize the ENTIRE raw payload with identical arguments and
    check containment. Because `json.dumps` applies the same encoder
    settings recursively to every nested object, the one fact's
    serialization is byte-identical and appears as a genuine contiguous
    substring — a real, deterministic containment check on structured
    JSON, the structural analogue of the prose-quote check above, not a
    trick that always trivially passes.
    """
    if xbrl_raw_payload is None:
        raise CitationValidationSetupError(
            f"evidence {evidence.id} is source_type='xbrl_fact', which requires xbrl_raw_payload"
        )
    canonical = json.dumps(xbrl_raw_payload, **_JSON_DUMPS_KWARGS)
    if evidence.quote not in canonical:
        raise CitationInvalidError(
            f"evidence {evidence.id}: quote does not appear verbatim in the raw XBRL payload"
        )


def _validate_computed(evidence: Evidence, *, resolve: Resolver | None) -> None:
    """Anti-fabrication by construction: the inputs used to recompute come
    from RESOLVING the cited evidence_ids against the real store, never
    from a self-reported duplicate inside `evidence.location` itself — a
    duplicated-inputs design would let a bug (or a compromised agent)
    fabricate `inputs` and `value` together with nothing to cross-check
    them against real stored facts.
    """
    if resolve is None:
        raise CitationValidationSetupError(
            f"evidence {evidence.id} is source_type='computed', which requires resolve"
        )

    location = evidence.location
    try:
        ratio_name: str = location["ratio_name"]
        input_ids_by_param: dict[str, str] = location["input_evidence_ids"]
        expected_value: float = location["value"]
    except KeyError as exc:
        raise CitationInvalidError(
            f"evidence {evidence.id}: computed evidence missing required location field {exc}"
        ) from None

    ratio_func = RATIO_FUNCS.get(ratio_name)
    if ratio_func is None:
        raise CitationInvalidError(f"evidence {evidence.id}: unknown ratio_name {ratio_name!r}")

    inputs: dict[str, float] = {}
    for param, id_str in input_ids_by_param.items():
        (source,) = resolve([UUID(id_str)])
        if source.source_type != "xbrl_fact":
            raise CitationInvalidError(
                f"evidence {evidence.id}: computed evidence cites non-xbrl_fact "
                f"input {source.id} (source_type={source.source_type!r})"
            )
        inputs[param] = source.location["value"]

    result = ratio_func(**inputs)
    if not math.isclose(result.value, expected_value, rel_tol=1e-9):
        raise CitationInvalidError(
            f"evidence {evidence.id}: recomputed {ratio_name}={result.value} "
            f"does not match stored value={expected_value}"
        )


def validate_all_evidence(
    evidence_rows: Sequence[Evidence],
    *,
    xbrl_raw_payloads: Mapping[str, dict[str, Any]] | None = None,
    documents: Mapping[str, str] | None = None,
    resolve: Resolver | None = None,
) -> list[CitationFailure]:
    """Runs `validate_citation` over every row, catching failures per-row
    rather than raising on the first one. An empty list means every
    citation is valid — this is what a CI citation-validity test calls, and
    what a deliberately-corrupted-quote test asserts is non-empty.

    `xbrl_raw_payloads`/`documents` are keyed by `source_ref` since a run
    may touch more than one filing/accession.
    """
    failures: list[CitationFailure] = []
    for evidence in evidence_rows:
        try:
            xbrl_raw_payload = (
                xbrl_raw_payloads.get(evidence.source_ref) if xbrl_raw_payloads else None
            )
            source_document = documents.get(evidence.source_ref) if documents else None
            validate_citation(
                evidence,
                source_document=source_document,
                xbrl_raw_payload=xbrl_raw_payload,
                resolve=resolve,
            )
        except (CitationInvalidError, CitationValidationSetupError) as exc:
            failures.append(CitationFailure(evidence, exc))
    return failures
