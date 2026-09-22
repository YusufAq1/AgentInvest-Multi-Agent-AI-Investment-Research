# ADR-0015: Reconstructing filing text from sections, after real-data testing falsified the original offset-trust design

## Status
Accepted — 2026-09-19 (revised same day after real-data testing; see "What was tried first" below)

## Context
`backend/rag/chunking.py` needs to locate each filing section's exact
character span within a filing's stored text, so `document_chunks` rows
carry a real, checkable location — the same property `xbrl_fact` evidence
already has via its deterministic quote construction (ADR-0012). The
library `edgartools` reports `Section.start_offset`/`end_offset` for every
detected section, alongside `confidence` (0.0-1.0) and `detection_method`
(`'toc'`, `'heading'`, or `'pattern'`), and separately exposes a filing's
whole text via `Document.text()`.

## What was tried first, and how it broke
The original design (this project's established principle: never trust a
third-party library's own claims without a local, testable check — every
`as_of` filter in `backend/data` re-verifies dates itself rather than
trusting upstream filtering) was to **trust `start_offset`/`end_offset`
directly when `confidence`/`detection_method` cleared a bar, and otherwise
recompute the span by searching for `section.text()` inside
`Document.text()`**.

Running this against a real AAPL 10-K (accession `0000320193-23-000106`)
falsified it outright, not partially:

1. **`start_offset` was `0` for every one of 23 sections.** They are not
   global offsets into `Document.text()` at all — confirmed directly, not
   inferred (two sections with wildly different real content both
   reported `start_offset=0`, which is only possible if the field isn't
   what a "global document offset" would mean).
2. **The substring-search fallback also failed for every section.**
   `full_text.find(section.text())` returned `-1` even for content that
   was genuinely present in both — verified with a whitespace-normalized
   comparison, which *did* find the content nearby. `Section.text()` and
   `Document.text()` turn out to be produced by different internal
   extraction code paths inside edgartools (one synthesizes/cleans
   headings and whitespace differently than the other), so they are not
   byte-comparable for the same underlying content even when both are
   individually correct.

Net effect: the original design would have produced **zero usable chunks
from a real filing** — a unit-test suite built entirely from small,
hand-crafted fixtures (where the search trivially succeeds because the
fixture author controls both strings) would never have caught this. This
is the same category of bug Phase 2's `INTERVIEW_NOTES.md` entry describes
for the Financial Agent's evidence-construction blowup: a design that
looks correct against synthetic fixtures and breaks the first time it
meets real data, which is exactly why this project's phase scripts exist
as real end-to-end checks alongside the test suite.

## Decision
**Don't relocate anything. Reconstruct the filing's storable text FROM the
sections, so offsets are correct by construction.**

`backend/rag/chunking.build_full_text(sections)` concatenates each
section's own `text` (in edgartools' detection order, joined by a blank
line) — this, not `Document.text()`, is what `backend/rag/indexing.py`
stores as `Document.full_text`. Because every `SectionChunk.char_start`/
`char_end` is computed *while building this string* — not searched for
inside a separately-obtained one — a chunk's offset is guaranteed valid
against the text it's actually stored alongside, with no search, no trust
threshold, and no failure mode to fall back from.

`confidence`/`detection_method` are still used, but for a different
purpose than originally planned: **skipping** sections edgartools itself
flags as unreliably detected (default floor `0.6`,
`rag_section_confidence_floor`), rather than deciding whether to trust a
location. This preserves the same underlying principle this project
applies everywhere else — don't take an upstream signal of low
reliability and use it anyway — just applied to "should this section be
chunked at all," not "where exactly does it start."

**Consequence: `document_chunks` gains `char_start`/`char_end`, but NOT
`start_offset`/`end_offset` passthrough from edgartools** — those fields
were removed from `SectionMeta` entirely once confirmed unusable, rather
than kept as dead, misleading metadata. The `char_start`/`char_end`
addition beyond CLAUDE.md §14's literal `accession`/`item`/`filing_date`/
`fiscal_period` list stands as originally justified: without them there is
no way to run the same verbatim-containment citation check
`backend/evidence/validation.py` already runs for `xbrl_fact` evidence,
applied to chunk-sourced quotes.

**Real-data verification**: re-running chunking against the same AAPL
filing after this fix produced 110 chunks across 22 of 23 sections (one,
`Item 1C`, correctly skipped at 0.5 confidence, below the 0.6 floor), with
**zero offset mismatches** when checked against the reconstructed text —
`full_text[char_start:char_end] == chunk.text` held for every single
chunk.

## Alternatives considered
- **Fix the substring-search fallback with whitespace normalization
  instead of abandoning it.** Rejected: normalizing whitespace to make the
  search succeed would then require translating a match position in
  *normalized* text back to a position in the *original* text — solvable,
  but strictly more complex than reconstruction, for a benefit
  (preserving `Document.text()`'s exact byte layout) that Phase 3 doesn't
  need. Reconstruction sidesteps the translation problem entirely.
- **Keep `Document.text()` as the stored `full_text` and accept that
  chunk offsets won't always resolve against it.** Rejected outright: an
  offset that sometimes doesn't check out against its own document is
  worse than no offset at all — it would look verifiable and not
  actually be.
- **Trust edgartools' offsets unconditionally (the very first idea,
  before any real-data testing).** Rejected for the reasons in the
  original context section, and now additionally disproven: they aren't
  global offsets at all, so "trusting" them would have been trusting a
  number that has no relationship to the text being stored.

## Consequences
Easy: a `document_chunks` row's `char_start`/`char_end` are now correct by
construction, not by verification against an unreliable upstream claim —
there is no failure mode left to test for at the offset level (the earlier
design needed tests for "trusted," "fallback-recovered," and "dropped";
the current design only needs "confidence too low, skipped," which is
simpler and was directly verified against real data before being
considered done.

Hard: `Document.full_text` (and therefore every stored citation quote) no
longer includes any content edgartools didn't classify into a detected
section — a cover page, an exhibit index, or boilerplate outside any
`Item` heading is not stored or citable. This is an acceptable scope
narrowing for Phase 3 (nothing in the RAG pipeline needs to cite
un-sectioned content), but is a real, documented difference from "the
filing's literal full text" and should be kept in mind if a future phase
ever wants to cite something outside a detected section.
