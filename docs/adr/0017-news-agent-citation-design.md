# ADR-0017: News Agent cites a deterministic canonical string, not the 8-K's narrative text

## Status
Accepted — 2026-09-22

## Context
The News Agent (CLAUDE.md §15's Phase 4) classifies the materiality of
SEC 8-K material-event filings. `backend/data/news.py`'s `NewsClient`
(Phase 1) fetches only structured metadata per filing — accession number,
filing date, item code(s), and their standard SEC labels — never the
filing's narrative body text. Its own module docstring explicitly and
deliberately scoped Phase 1 this way, deferring richer narrative sourcing
(RSS, GDELT) to whichever later phase actually needs it.

The question: what does the News Agent's `Evidence.quote` contain, given
there is no narrative text to quote from at all?

## Decision
**A deterministic, Python-constructed canonical string**, built only from
`EightKEvent`'s own real fields:
```python
f"8-K filed {event.filing_date.isoformat()} (accession {event.accession_number}): "
f"{'; '.join(f'{code} ({label})' for code, label in zip(event.items, event.item_labels))}"
```
This is the same category of construction as `"computed"` evidence's
`f"{name} = {formula} = {value:.6f}"` string (`backend/agents/financial.py`)
— never freeform or paraphrased, always reconstructible bit-for-bit from
data Python already has and verified.

**We deliberately did not fetch the real 8-K narrative body** (which
`edgartools` could, in principle, retrieve the same way
`EdgarClient.get_filing_document` retrieves 10-K/10-Q text), for three
concrete reasons:
1. `EdgarClient.get_filing_document`'s whole pipeline was built and
   validated specifically against `TenK`/`TenQ` — ADR-0015's offset-trust
   saga was discovering that `edgartools`' extraction internals aren't
   uniform even across the forms already tested. Extending it to
   `CurrentReport` (8-K) is real, unverified surface area, not a one-line
   change.
2. `NewsClient` deferred narrative sourcing as its **own** future work
   (RSS/GDELT), not as "have the News Agent reach around it into
   `EdgarClient` instead." Building a parallel narrative-fetch path in the
   News Agent would duplicate the Filings Agent's whole
   fetch→chunk→embed→index→cite pipeline for a different form type — a
   structurally bigger piece of work than "materiality classification,"
   this phase's actual exit criterion for the News Agent.
3. CLAUDE.md §7 lists "materiality judgement on events" as the LLM task,
   contrasted only with deterministic date filtering — not "materiality
   judgement over full narrative text." Item code + label + filing date is
   a real, judgeable signal on its own: a bankruptcy (1.03) or
   cybersecurity-incident (1.05) code is inherently higher-materiality
   than a routine exhibit filing (9.01), independent of what the filing's
   body actually says.

## Honest limitation — stated plainly, not hidden
This design makes the citation-validity containment check
(`backend/evidence/validation.py`'s `("sec_filing", "news")` branch,
`quote in source_document`) **tautological** for News evidence: the
`documents` mapping the caller supplies for this check uses the exact same
string as `quote` itself, since there is no separately-fetched document to
check it against. The check can only ever catch a coding bug where two
call sites constructed the quote differently — it cannot catch fabrication,
because there is nothing external it's checking against.

This is an accepted tradeoff, not a gap papered over: the real
anti-fabrication guarantee for News evidence, as for every other source
type, is structural — Claude never writes `quote`, it only cites an
`evidence_id` from the set Python already built (see
`EvidenceStore.validate_claim_batch`). The containment check is a second,
independent layer that happens to add nothing extra for this one source
type, and that's worth saying outright rather than presenting a symmetry
with `sec_filing` evidence's containment check that doesn't actually carry
the same weight.

**Real fidelity gap, also stated plainly:** the News Agent cannot cite
specific numbers, guidance, or narrative detail from inside an 8-K's
actual disclosure — only the fact that a given item code was filed on a
given date. A future, more capable News Agent that generalizes
`backend/rag/`'s indexing pipeline past 10-K/10-Q to arbitrary filing
forms could close this gap; that's real future work, not something this
phase does by omission.

## Alternatives considered
- **Fetch the real 8-K narrative via `EdgarClient`.** Rejected for the
  three reasons above — genuinely more work than this phase's exit
  criterion calls for, and risks repeating ADR-0015's exact mistake
  (assuming an unverified extraction path behaves like a verified one).
- **Leave `Evidence.quote` empty or a placeholder for News evidence.**
  Rejected: `Evidence.quote` is documented as always containing the
  verbatim source text — an empty or placeholder quote would violate that
  invariant for one source_type without a clear signal that it's a
  special case.
- **Add a `source_type="news_metadata"` variant with its own validation
  path.** Rejected as unnecessary complexity: the existing `("sec_filing",
  "news")` containment branch already accepts any `documents` mapping the
  caller supplies — no validation.py changes were needed at all, which is
  itself evidence the existing design already generalizes to this case
  without a new branch.

## Consequences
Easy: zero new code in `backend/evidence/validation.py` — the News Agent
plugs into the exact same `("sec_filing", "news")` containment path
`sec_filing` evidence already uses. `Claim.claim_type="evidence"` for "this
8-K item was filed" is directly verifiable and required no new enforcement
machinery.

Hard: the News Agent's claims are inherently shallow compared to what a
narrative-aware version could produce — a real, acknowledged product
limitation communicated honestly in `docs/INTERVIEW_NOTES.md` rather than
implied to be more capable than it is.
