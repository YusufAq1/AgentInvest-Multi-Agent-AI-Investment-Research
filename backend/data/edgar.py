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
from backend.data.models import DataUnavailable, Filing
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
            obj = real_filing.obj()
            result = obj[section]
        except (KeyError, TypeError) as exc:
            raise PermanentDataError(f"Section {section!r} not available") from exc
        return str(result)
