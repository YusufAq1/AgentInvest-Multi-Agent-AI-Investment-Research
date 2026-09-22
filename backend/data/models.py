"""Shared value types for backend.data.

WHY these are plain Pydantic models, not dataclasses: every one of them
either crosses a module boundary (a return value future agents will
consume) or gets logged/serialized — Pydantic validation and `.model_dump()`
are worth the small overhead dataclasses don't give us for free.
"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel


class DataUnavailable(BaseModel):
    """CLAUDE.md C6's explicit "missing data" marker.

    Returned, never raised, from every public backend.data method for
    *expected* failure modes — a rate limit exhausted after retries, an
    unknown ticker, no data in the requested window. See errors.py's
    module docstring for why this is a return value and not an exception,
    and why some other failures are deliberately NOT converted to this.
    """

    source: Literal["edgar", "xbrl", "yfinance", "fred", "sec_8k", "cik_lookup"]
    identifier: str
    as_of: date
    reason: str
    detail: str | None = None
    attempted_at: datetime


class Filing(BaseModel):
    """One SEC filing (10-K, 10-Q, 8-K, ...), independent of source module."""

    accession_number: str
    form: str
    filing_date: date
    cik: str


class XBRLFact(BaseModel):
    """One structured fact from SEC XBRL companyfacts.

    WHY `filed` is a required field, not optional: it's the field every
    as_of filter in xbrl.py keys on. `end`/`fiscal_year`/`fiscal_period`
    describe the period being reported on, not when the number became
    public — filtering on those instead of `filed` is exactly the
    look-ahead-bias bug this project exists to prevent (C3).
    """

    concept: str
    taxonomy: Literal["us-gaap", "dei"]
    unit: str
    value: float
    period_start: date | None
    period_end: date
    fiscal_year: int | None
    fiscal_period: str | None
    form: str
    filed: date
    accession_number: str


class XBRLCompanyFacts(BaseModel):
    """Parsed facts plus the raw companyfacts payload they came from.

    WHY bundled together: callers that need to build or verify a
    byte-exact citation `quote` (the Financial Agent, and
    backend/evidence/validation.py's xbrl_fact containment check) need
    BOTH the parsed, typed facts and the raw JSON — returning them
    separately would mean fetching companyfacts twice (a second local
    cache read is cheap, but re-deriving the CIK-resolution/DataUnavailable
    handling a second time would duplicate xbrl.py's own logic elsewhere).
    """

    facts: list[XBRLFact]
    raw: dict[str, Any]


class PriceBar(BaseModel):
    """One daily OHLCV price bar."""

    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


class MacroObservation(BaseModel):
    """One FRED data point for a given series."""

    series_id: str
    date: date
    value: float


class EightKEvent(BaseModel):
    """One 8-K filing, with its typed item codes."""

    accession_number: str
    filing_date: date
    items: list[str]
    item_labels: list[str]


class SectionMeta(BaseModel):
    """One filing section's metadata, as edgartools' `Section` reports it.

    WHY a plain local projection instead of importing edgartools' own
    `Section` dataclass into backend.rag: decouples chunking.py (which
    reads these fields) from edgartools' internal shape drifting across
    versions, and keeps this module the single place that knows what a
    "section" from an upstream filing-parsing library looks like — the
    same reasoning `Filing` already applies to accession/form/date.

    WHY there's no `start_offset`/`end_offset` here, despite edgartools'
    `Section` exposing them: confirmed against a real AAPL 10-K that they
    are NOT usable global offsets into `Document.text()` — every section
    reported `start_offset=0`, because `Section.text()` and
    `Document.text()` are extracted through different internal code paths
    that aren't byte-comparable (confirmed: even a substring-search
    fallback failed to relocate any section's text inside the separately
    -extracted document text). See ADR-0015's revision history for the
    full story and why `backend/rag/chunking.py` instead reconstructs its
    own full text directly from these sections' own `text`, making
    offsets correct by construction rather than something to locate.

    `confidence`/`detection_method` ARE still used — not for offset trust
    (there's no offset here to trust), but to skip sections edgartools
    itself flags as unreliably detected (see `rag_section_confidence_floor`
    in core/config.py). `detection_method` is typed as plain `str`, not a
    Literal: edgartools' own set of values isn't part of its documented
    public contract, and constraining it here would raise a validation
    error on a legitimate value this project simply hasn't seen yet.
    """

    name: str
    title: str | None
    item: str | None
    part: str | None
    confidence: float
    detection_method: str
    text: str


class FilingDocument(BaseModel):
    """A filing's section-aware metadata — everything
    `backend/rag/indexing.py` needs to chunk the whole filing.

    WHY there's no separate `full_text` field here (unlike an earlier
    version of this model): edgartools' `Document.text()` isn't reliably
    relatable to any individual `Section.text()` (see `SectionMeta`'s
    docstring) — the text this project actually stores and chunks against
    is `backend.rag.chunking.build_full_text(sections)`, reconstructed
    from these sections directly, not fetched separately. Fetching
    `Document.text()` here would be extra work with no consumer.
    """

    sections: list[SectionMeta]
    period_of_report: date | None
