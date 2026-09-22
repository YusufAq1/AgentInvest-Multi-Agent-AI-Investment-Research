"""Section-aware, metadata-preserving chunking of SEC filings.

WHY this reconstructs its own full text from sections rather than locating
sections within edgartools' separately-extracted `Document.text()`: an
earlier version of this module tried exactly that — trust
`Section.start_offset`/`end_offset` when confidence/detection_method were
high, else recompute the span via `full_text.find(section.text())`. Real
end-to-end testing against a live AAPL 10-K falsified that design:
`Section.start_offset` was `0` for every single section (they aren't
global offsets into `Document.text()` at all), and the substring-search
fallback ALSO failed for every section, because `Section.text()` and
`Document.text()` turn out to be extracted through different internal
code paths inside edgartools that produce non-byte-comparable text for
the same underlying content (confirmed: the content is genuinely present
in both, just formatted differently — a whitespace-normalized search
found it, an exact one didn't). Trying to relocate one extraction's output
inside a different extraction's output was the wrong approach entirely.

The fix: don't relocate anything. `build_full_text()` reconstructs the
filing's storable text by concatenating each section's own `text` in
detection order — the same text `document_chunks.text` is a substring of
by construction, not by search. Offsets are correct because they're
computed while building the text, never trusted from an upstream claim
and never searched for. See ADR-0015's revision history for the full
account of what was tried, why it broke, and why this is the design that
actually works against real filings.

`confidence`/`detection_method` are still used — not to trust an offset
(there is none to trust anymore), but to skip sections edgartools itself
flags as unreliably detected in the first place (CLAUDE.md's "never trust
an upstream library's claim without a check," applied here as "don't
bother chunking content the library itself isn't confident it correctly
identified").

This module is pure — no I/O, no network, no DB — so it's trivially
testable with fake `SectionMeta` fixtures. It joins `backend.calc.*`/
`backend.evidence.*` under mypy `--strict` for exactly that reason.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date

from pydantic import BaseModel

from backend.data.models import SectionMeta
from backend.rag.embeddings import get_tokenizer

logger = logging.getLogger(__name__)

# Joins each section's text when reconstructing the filing's full text —
# a blank line, matching how SEC filings visually separate items, and
# short enough that its own length is a negligible, easily-accounted-for
# contribution to every char_start/char_end past the first section.
_SECTION_SEPARATOR = "\n\n"


class SectionChunk(BaseModel):
    """One chunk of a filing section, carrying everything
    `backend.db.models.DocumentChunk` needs plus the char offsets a later
    citation-quote check needs — offsets into the text
    `build_full_text()` returns for the same sections, which
    `backend/rag/indexing.py` stores as `Document.full_text`.
    """

    text: str
    chunk_index: int
    item: str | None
    section_name: str
    char_start: int
    char_end: int
    accession: str
    filing_date: date
    fiscal_period: str | None


def build_full_text(sections: Sequence[SectionMeta]) -> str:
    """Reconstructs a filing's storable full text by concatenating each
    section's own `text`, in the order edgartools detected them, joined by
    `_SECTION_SEPARATOR`. This — not edgartools' `Document.text()` — is
    what `backend/rag/indexing.py` stores as `Document.full_text`, so that
    every `SectionChunk.char_start`/`char_end` this module computes is
    guaranteed valid against it (see this module's docstring for why that
    guarantee doesn't hold against edgartools' own document text).
    """
    return _SECTION_SEPARATOR.join(section.text for section in sections)


def chunk_filing_sections(
    *,
    sections: Sequence[SectionMeta],
    accession: str,
    filing_date: date,
    fiscal_period: str | None,
    max_tokens: int,
    overlap_tokens: int,
    confidence_floor: float,
) -> list[SectionChunk]:
    """Chunks every section edgartools detected with confidence at or
    above `confidence_floor`, sub-chunking long sections by token count
    with overlap. Sections below the floor are skipped and logged — not
    because their location is untrustworthy (there's no location to
    trust or distrust anymore, see module docstring), but because
    edgartools itself flags them as an unreliable detection.
    """
    chunks: list[SectionChunk] = []
    chunk_index = 0
    cursor = 0

    for position, section in enumerate(sections):
        if position > 0:
            cursor += len(_SECTION_SEPARATOR)
        section_start = cursor
        cursor += len(section.text)

        if section.confidence < confidence_floor:
            logger.warning(
                "Skipping section %r (item=%s) of filing %s: detection confidence "
                "%.2f is below the configured floor %.2f",
                section.name,
                section.item,
                accession,
                section.confidence,
                confidence_floor,
            )
            continue
        if not section.text:
            continue

        for sub_start, sub_end in _sub_chunk_offsets(section.text, max_tokens, overlap_tokens):
            chunks.append(
                SectionChunk(
                    text=section.text[sub_start:sub_end],
                    chunk_index=chunk_index,
                    item=section.item,
                    section_name=section.name,
                    char_start=section_start + sub_start,
                    char_end=section_start + sub_end,
                    accession=accession,
                    filing_date=filing_date,
                    fiscal_period=fiscal_period,
                )
            )
            chunk_index += 1

    return chunks


def _sub_chunk_offsets(text: str, max_tokens: int, overlap_tokens: int) -> list[tuple[int, int]]:
    """Splits `text` into token-bounded windows (BGE-M3's own tokenizer,
    via `get_tokenizer()` — no extra dependency), returning each window's
    CHAR span so callers never need a second substring search to locate a
    sub-chunk within its section. Uses the tokenizer's `offset_mapping`
    (a HuggingFace fast-tokenizer feature) to recover exact char spans
    directly from token spans.
    """
    if not text:
        return []
    if overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens must be smaller than max_tokens")

    tokenizer = get_tokenizer()
    encoding = tokenizer(text, return_offsets_mapping=True, add_special_tokens=False)
    offsets: list[tuple[int, int]] = encoding["offset_mapping"]
    if not offsets:
        return []

    total_tokens = len(offsets)
    if total_tokens <= max_tokens:
        return [(offsets[0][0], offsets[-1][1])]

    step = max_tokens - overlap_tokens
    spans: list[tuple[int, int]] = []
    token_start = 0
    while token_start < total_tokens:
        token_end = min(token_start + max_tokens, total_tokens)
        spans.append((offsets[token_start][0], offsets[token_end - 1][1]))
        if token_end == total_tokens:
            break
        token_start += step
    return spans
