"""SEC filings (10-K/10-Q/section text) via edgartools.

WHY edgartools instead of hitting EDGAR ourselves: it parses filings into
typed objects with section access (`filing.obj()["Item 1A"]`) and manages
its own SEC rate limiting internally — reimplementing HTML/XBRL-viewer
parsing ourselves would be a large, fragile undertaking for no benefit.

WHY every call is wrapped in `asyncio.to_thread`: edgartools' public API is
synchronous (confirmed by inspecting the installed 5.58.0 package — no
documented public async entry point exists, even though it uses httpx
internally). Calling it directly from async code without offloading to a
thread would block the event loop.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

from edgar import Company, set_identity
from edgar import find as edgar_find
from edgar._filings import Filing as EdgarFiling

from backend.core.config import Settings
from backend.data.errors import (
    EdgarIdentityNotConfiguredError,
    PermanentDataError,
    TransientDataError,
)
from backend.data.models import DataUnavailable, Filing, FilingDocument, SectionMeta
from backend.data.retry import with_retry


class EdgarClient:
    """Fetches SEC filings and filing sections for one ticker at a time."""

    def __init__(self, settings: Settings) -> None:
        if not settings.edgar_identity:
            raise EdgarIdentityNotConfiguredError(
                "EDGAR_IDENTITY is not set — required before any SEC-hitting "
                "call (see .env.example)."
            )
        self._settings = settings
        set_identity(settings.edgar_identity)

    async def get_filings(
        self,
        ticker: str,
        as_of: date,
        *,
        forms: tuple[str, ...] = ("10-K", "10-Q"),
        limit: int = 10,
    ) -> list[Filing] | DataUnavailable:
        """Returns up to `limit` filings of the given `forms`, filed on or
        before `as_of`, most recent first.

        Uses edgartools' native `filing_date=":as_of"` range syntax AND
        re-checks `filing_date <= as_of` on every returned item ourselves —
        never trust a third-party library's date filtering without a
        local, testable check (this is what the leakage test targets).
        """
        retrying_fetch = with_retry(
            max_attempts=self._settings.data_retry_max_attempts,
            base_delay_s=self._settings.data_retry_base_delay_s,
            max_delay_s=self._settings.data_retry_max_delay_s,
        )(self._fetch_filings)
        filings = await retrying_fetch(ticker, as_of, forms, limit)

        if not filings:
            return DataUnavailable(
                source="edgar",
                identifier=ticker,
                as_of=as_of,
                reason=f"No {'/'.join(forms)} filings found for {ticker!r} on/before {as_of}",
                attempted_at=datetime.now(UTC),
            )
        return filings

    async def _fetch_filings(
        self, ticker: str, as_of: date, forms: tuple[str, ...], limit: int
    ) -> list[Filing]:
        return await asyncio.to_thread(self._fetch_filings_sync, ticker, as_of, forms, limit)

    def _fetch_filings_sync(
        self, ticker: str, as_of: date, forms: tuple[str, ...], limit: int
    ) -> list[Filing]:
        # WHY a broad `except Exception` at this one boundary: edgartools
        # doesn't document a crisp exception hierarchy for "ticker not
        # found" vs. "network failure" vs. other internal errors. This is
        # the single seam between our code and that library's opaque
        # exception surface — everything past it is re-raised as one of
        # our own typed exceptions, not left as a bare, unclassified catch.
        try:
            company = Company(ticker)
            results = company.get_filings(
                form=list(forms), filing_date=f":{as_of.isoformat()}"
            ).head(limit)
        except Exception as exc:
            raise TransientDataError(f"edgartools failed fetching filings for {ticker}") from exc

        filings: list[Filing] = []
        for f in results:
            filing_date = (
                f.filing_date
                if isinstance(f.filing_date, date)
                else date.fromisoformat(str(f.filing_date))
            )
            if filing_date > as_of:
                # Defensive re-check: don't trust the library's own filter.
                continue
            filings.append(
                Filing(
                    accession_number=f.accession_number,
                    form=f.form,
                    filing_date=filing_date,
                    cik=str(f.cik).zfill(10),
                )
            )
        return filings

    async def get_filing_section(
        self, filing: Filing, section: str, as_of: date
    ) -> str | DataUnavailable:
        """Returns the text of `section` (e.g. `"Item 1A"`) from `filing`.

        Re-asserts `filing.filing_date <= as_of` even though `filing` came
        from `get_filings` above — defends a caller reusing a stale `Filing`
        object across a different `as_of`. The full set of valid section
        keys isn't hardcoded here (it varies by form type and isn't fully
        enumerated in edgartools' docs) — introspect a live filing object's
        own attributes if a specific key is needed beyond `"Item 1A"`.
        """
        if filing.filing_date > as_of:
            return DataUnavailable(
                source="edgar",
                identifier=filing.accession_number,
                as_of=as_of,
                reason=(
                    f"Filing {filing.accession_number} was filed "
                    f"{filing.filing_date}, after as_of {as_of}"
                ),
                attempted_at=datetime.now(UTC),
            )

        try:
            text = await asyncio.to_thread(self._fetch_section_sync, filing, section)
        except PermanentDataError:
            return DataUnavailable(
                source="edgar",
                identifier=filing.accession_number,
                as_of=as_of,
                reason=f"Section {section!r} not found in filing {filing.accession_number}",
                attempted_at=datetime.now(UTC),
            )
        return text

    def _fetch_section_sync(self, filing: Filing, section: str) -> str:
        real_filing = edgar_find(filing.accession_number)
        if not isinstance(real_filing, EdgarFiling):
            raise PermanentDataError(
                f"{filing.accession_number} did not resolve to a filing (got "
                f"{type(real_filing).__name__})"
            )
        try:
            # WHY the ignore: edgartools' Filing.obj() has no return type
            # annotation (confirmed via inspect.signature on the installed
            # package) — a gap in their type hints, not ours to fix.
            obj = real_filing.obj()  # type: ignore[no-untyped-call]
            result = obj[section]
        except (KeyError, TypeError) as exc:
            raise PermanentDataError(f"Section {section!r} not available") from exc
        return str(result)

    async def get_filing_document(
        self, filing: Filing, as_of: date
    ) -> FilingDocument | DataUnavailable:
        """Returns `filing`'s full text plus section-aware metadata for
        every section edgartools detects — the input backend/rag/indexing.py
        chunks. `get_filing_section` above stays as the single-named-section
        reader; this answers "give me everything needed to chunk the whole
        filing" instead.

        Re-asserts `filing.filing_date <= as_of`, exactly like
        `get_filing_section` — same defense against a caller reusing a stale
        `Filing` object across a different `as_of`.
        """
        if filing.filing_date > as_of:
            return DataUnavailable(
                source="edgar",
                identifier=filing.accession_number,
                as_of=as_of,
                reason=(
                    f"Filing {filing.accession_number} was filed "
                    f"{filing.filing_date}, after as_of {as_of}"
                ),
                attempted_at=datetime.now(UTC),
            )

        try:
            document = await asyncio.to_thread(self._fetch_document_sync, filing)
        except PermanentDataError:
            return DataUnavailable(
                source="edgar",
                identifier=filing.accession_number,
                as_of=as_of,
                reason=f"No parseable document for filing {filing.accession_number}",
                attempted_at=datetime.now(UTC),
            )
        return document

    def _fetch_document_sync(self, filing: Filing) -> FilingDocument:
        real_filing = edgar_find(filing.accession_number)
        if not isinstance(real_filing, EdgarFiling):
            raise PermanentDataError(
                f"{filing.accession_number} did not resolve to a filing (got "
                f"{type(real_filing).__name__})"
            )
        try:
            # WHY the ignore: same undocumented-return-type gap as
            # _fetch_section_sync's obj() call above.
            obj = real_filing.obj()  # type: ignore[no-untyped-call]
        except Exception as exc:
            raise PermanentDataError(f"Failed to parse {filing.accession_number}") from exc
        if obj is None:
            # edgartools returns None from Filing.obj() for form types it
            # has no typed report class for — not every filing form is
            # chunkable, and that's an expected gap, not a bug.
            raise PermanentDataError(f"{filing.accession_number}'s form has no report object")

        document = obj.document
        # WHY document.text() is never fetched here: see SectionMeta's and
        # FilingDocument's docstrings — it isn't reliably relatable to any
        # individual section's own text, confirmed against real filings,
        # so fetching it here would be wasted work with no consumer.
        sections = [
            SectionMeta(
                name=name,
                title=section.title,
                item=section.item,
                part=section.part,
                confidence=section.confidence,
                detection_method=section.detection_method,
                text=section.text(),
            )
            for name, section in document.sections.items()
        ]
        return FilingDocument(
            sections=sections,
            period_of_report=self._parse_period_of_report(obj.period_of_report),
        )

    @staticmethod
    def _parse_period_of_report(value: object) -> date | None:
        """edgartools documents `CompanyReport.period_of_report` as
        `Optional[str]` (confirmed in the installed package's
        attachments.py) — unlike CurrentReport/SixK, the base TenK/TenQ
        path does not normalize it to a `date` internally. Parsed
        defensively here rather than trusting a specific string format;
        returns None (never fabricated) if it doesn't parse, per C6.
        """
        if value is None:
            return None
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value).strip())
        except ValueError:
            return None
