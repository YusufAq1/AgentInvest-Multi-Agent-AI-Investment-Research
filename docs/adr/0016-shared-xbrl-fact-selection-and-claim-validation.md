# ADR-0016: Extract shared XBRL fact-selection and claim-validation plumbing ahead of Phase 4's second and third agents

## Status
Accepted — 2026-09-22

## Context
Phase 2's Financial Agent (`backend/agents/financial.py`) built two pieces
of plumbing as private, file-local code: (1) a concept-alias table plus
`_select_anchor`/`_select_matching`/`_select_prior_year` for resolving
filer-specific XBRL tagging to a consistent set of ratio inputs, and (2) a
`_validate_claim_batch` function bridging `EvidenceStore.resolve`'s
`EvidenceNotFoundError` into the `ValueError` `ClaudeClient.call_structured`'s
`extra_validation` contract requires to trigger its retry.

At the time (one agent, Phase 2), keeping both private was correct —
extracting shared code ahead of a second consumer would have been
speculative. Phase 4 introduces that second, third, and fourth consumer:
the Competitive Agent needs the identical XBRL selection logic (across
multiple companies, not one), and the Filings/News/Competitive agents all
need the identical `extra_validation` bridge Financial Agent already has.

## Decision
**Extract the XBRL selection logic to a new module, `backend/agents/
xbrl_facts.py`** (`CONCEPT_ALIASES`, `ALL_ALIASES`, `PRIOR_YEAR_TOLERANCE_DAYS`,
`FactKey`, `fact_key`, `select_anchor`, `select_matching`,
`select_prior_year` — renamed from their `_`-prefixed originals since
they're now a public, imported API). `_make_fact_evidence`/`_try_add_ratio`
**stay** in `financial.py`: they close over agent state (`self._store.run_id`)
and construct Evidence in a shape specific to the Financial Agent's full
ratio set — only the *selection* logic (which fact answers "revenue" for
this filer, in this period) is genuinely identical across agents; Evidence
construction differs enough (Competitive Agent computes fewer ratios per
company, across multiple companies) that sharing it would force an
awkward, over-parameterized interface for no real duplication saved.

**Move `_validate_claim_batch` onto `EvidenceStore` itself**, as
`EvidenceStore.validate_claim_batch(batch: ClaimBatch) -> None`. It
belongs there rather than as agent-local glue: `EvidenceStore` already
owns `resolve` and already imports `EvidenceNotFoundError`, and every
agent's `_emit_claims` now passes `self._store.validate_claim_batch`
directly as `extra_validation` instead of each agent defining its own
wrapper closing over `self._store`.

## Alternatives considered
- **Leave both duplicated across four agents.** Rejected: CLAUDE.md §17's
  "agents that all do the same thing" is aimed at whole agents, but the
  same reasoning applies to shared plumbing — four copies of
  byte-identical selection/validation logic is a maintenance liability
  (a bug fix in one copy silently not applying to the other three) with
  no offsetting benefit.
- **A shared base agent class providing these as methods.** Rejected:
  `backend/agents/base.py`'s `ResearchAgent` is deliberately a `Protocol`,
  not an ABC (its own docstring: no shared implementation existed with
  only one agent to justify one). The selection functions don't need
  `self` at all — they're pure functions over `XBRLFact` lists — so a
  base class would add inheritance machinery around code that doesn't
  need it.
- **Extract Evidence construction too (`_make_fact_evidence`/
  `_try_add_ratio`).** Rejected: Competitive Agent's Evidence rows are
  tagged per-company (`location["company"]`) and cover a narrower ratio
  set (revenue + net margin only) — forcing one shared constructor to
  handle both shapes would need enough parameters/branches to erase the
  simplicity extraction is supposed to buy.

## Consequences
Easy: a bug fix or refinement to fact-selection heuristics (e.g. the
documented "most recent period_end" simplification in `select_anchor`)
now applies to every agent that uses it, verified once in
`tests/agents/test_xbrl_facts.py` instead of indirectly through each
agent's own test suite. `test_financial.py` needed zero changes — every
assertion there is black-box against `agent.run(...)`/`store`, confirming
the extraction is behavior-preserving.

Hard: `backend/agents/financial.py` and `backend/agents/competitive.py`
now both depend on `xbrl_facts.py` — a breaking change to that module's
function signatures affects two agents instead of one. This is the
accepted cost of removing real duplication, not a new risk introduced by
this refactor.
