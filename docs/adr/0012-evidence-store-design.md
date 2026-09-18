# ADR-0012: Evidence Store design — in-memory, two-path citation validation, batch-level retry

## Status
Accepted — 2026-09-18

## Context
CLAUDE.md §6 requires an Evidence Store that makes fabricated citations
structurally impossible, not just discouraged by a prompt: every material
claim must carry an evidence_id that resolves to stored, verbatim-quoted
source text, enforced in code. Phase 2 is the first phase that needs this,
and it's also the first phase with an LLM-driven agent (the Financial
Agent) producing structured output that must be validated and, on failure,
retried once with the error fed back (§6 rule 4, §16).

Three design questions didn't have a single spec-mandated answer and were
resolved with explicit tradeoffs, discussed and confirmed before building:
whether the store persists across runs, how "verbatim in the stored
source" applies to non-prose evidence (XBRL facts, computed ratios), and at
what granularity a failed validation triggers a retry.

## Decision

**1. The Evidence Store is in-memory, per-run.** `EvidenceStore` holds
`Evidence`/`Claim` objects for the duration of one process, keyed by
`run_id`/`as_of` at construction. This matches Phase 0/1's precedent of
deferring Postgres writes until something actually forces cross-run
durability — nothing in Phase 2 needs a claim to outlive one run. The
`evidence`/`claims`/`claim_evidence`/`documents` tables from CLAUDE.md §14
get built for real whenever a later phase needs a claim to survive past a
single process (likely once orchestration/API responses need to reference
a past run).

**2. Citation validation has two paths by `source_type`.** "Quote appears
verbatim in the stored source" means substring containment for prose
(`sec_filing`/`news`) and for structured JSON (`xbrl_fact`, checked by
re-serializing the full raw companyfacts payload with the same
`json.dumps` arguments the Financial Agent used to build the quote, and
checking containment — a genuine, deterministic check on structured data,
not a trick that trivially passes). For `computed` evidence (a derived
ratio), there is no upstream document to contain it — instead,
`validation.py` resolves the evidence's cited input `evidence_ids` against
the real store and recomputes the value using the same `RATIO_FUNCS`
dispatch table the Financial Agent used, asserting it matches. Values used
in the recompute come from *resolving real stored evidence*, never from a
self-reported duplicate inside the evidence row's own `location` field —
this is what makes a fabricated `computed` evidence row structurally
detectable rather than trivially self-consistent.

**3. `Evidence.location` stays a free-form `dict[str, Any]`, not a typed
model.** The shape genuinely differs by `source_type` — byte-offset spans
for prose, concept/unit/value for XBRL facts, and for `computed` evidence,
`{"ratio_name", "input_evidence_ids": {param: evidence_id}, "value"}`,
where `input_evidence_ids` is what makes the anti-fabrication recompute
check in point 2 possible at all. A single rigid model would need every
field optional across every source_type, which is worse than a
per-source_type-documented dict. (Parameterized as `dict[str, Any]` rather
than CLAUDE.md's literal bare `dict`, purely to satisfy `mypy --strict`'s
`disallow_any_generics` — no behavior change.)

**4. Retry-then-drop applies at the whole-batch level, not per-claim.** The
Financial Agent requests all of a run's claims in one forced-tool-use call
(cheaper than one call per claim — C2). If any single claim in the batch
cites an evidence_id outside the set it was shown, one retry re-sends the
whole batch with the error, and if that also fails, the entire batch is
dropped and recorded (`EvidenceStore.record_dropped_claim`) — not just the
offending claim. This can occasionally lose claims that were individually
fine alongside one bad one. It was chosen over per-claim salvage (parsing
each claim independently, retrying/dropping only the bad ones) because:
per-claim salvage needs either re-prompting Claude for just the bad claims
(multi-turn bookkeeping to track which already survived) or partial-parse
of a validation-failed batch (a Pydantic `ValidationError` on a `list[Claim]`
doesn't cleanly say "keep items 0 and 1, drop item 2"); and Phase 2's exit
criterion is "claims that all pass validation," not maximizing claim
yield. The core safety property (never let an unsupported claim through)
holds either way.

**5. The retry-then-drop control flow is split across two layers, not one.**
Schema-level failures (malformed JSON, or `Claim`'s own Pydantic validator
rejecting an empty `evidence_ids` on an `evidence`-type claim) are generic
and reusable by every future agent — handled in
`ClaudeClient.call_structured` via an `extra_validation` hook. Domain-level
failures (a well-formed `Claim` citing an evidence_id outside the set it
was shown) aren't a Pydantic concern — no bare `Claim` has a store to check
against — so the Financial Agent supplies an `extra_validation` closure
over `EvidenceStore.resolve`, treated identically to a schema failure by
the same one-retry mechanism.

## Alternatives considered
- **Postgres-backed Evidence Store from day one.** Rejected: a bigger scope
  increase than Phase 2's exit criterion calls for, and Phase 0/1 already
  established that Postgres gets built when something needs it, not
  speculatively.
- **Substring containment everywhere, including computed evidence.**
  Rejected: would require formatting a derived number as if it were a
  stored document, which is artificial for something that never existed
  verbatim anywhere upstream — the recompute check is a real correctness
  check where containment would be theater.
- **Per-claim retry/drop granularity.** Rejected for Phase 2 (see point 4)
  — a legitimate future enhancement if batch-level drops turn out to
  discard good claims often enough in practice to matter.

## Consequences
Easy: the enforcement mechanism is provably real — a test deliberately
corrupts a quote (or a computed value) and asserts `validate_citation`
rejects it, which is Phase 2's literal exit criterion made concrete. Adding
a new evidence source later means adding one branch to `validate_citation`
and documenting what "verbatim" means for it, not redesigning the store.

Hard: occasional good-claim loss from batch-level drops (point 4) —
revisit if real runs show this happening often enough to matter. No
cross-run history yet (point 1) — revisit once a phase needs to reference
a past run's claims (the API, or a consistency-eval harness comparing runs
across time).
