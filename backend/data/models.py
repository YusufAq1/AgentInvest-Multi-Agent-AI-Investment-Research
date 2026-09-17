"""Shared value types for backend.data.

WHY these are plain Pydantic models, not dataclasses: every one of them
either crosses a module boundary (a return value future agents will
consume) or gets logged/serialized — Pydantic validation and `.model_dump()`
are worth the small overhead dataclasses don't give us for free.
"""

from datetime import date, datetime
from typing import Literal

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
