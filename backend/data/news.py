"""SEC 8-K filings — material events, via the free EDGAR `submissions` API.

Phase 1 scope is 8-K only. CLAUDE.md §13 shows this module eventually
covering "8-K primary, RSS/GDELT secondary," but RSS/GDELT are explicitly
deferred to the Phase 4 News Agent, which is the first thing that actually
needs them for materiality classification — building them now would be
scaffolding ahead of the phase that uses them.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Final

from backend.data.cache import Cache
from backend.data.cik import CikResolver
from backend.data.errors import CikNotFoundError
from backend.data.http import SecHttpClient
from backend.data.models import DataUnavailable, EightKEvent
from backend.data.submissions import fetch_submissions

# Source of truth: the current SEC Form 8-K instructions
# (https://www.sec.gov/files/form8-k.pdf). Deliberately includes Item 1.05
# (Material Cybersecurity Incidents, added December 2023) — an older
# reference list would omit it.
ITEM_CODE_LABELS: Final[dict[str, str]] = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events That Accelerate a Direct Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure/Election of Directors or Officers",
    "5.03": "Amendments to Articles of Incorporation or Bylaws",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}


class NewsClient:
    """Fetches 8-K material-event filings for one ticker."""

    def __init__(self, http: SecHttpClient, cik: CikResolver, cache: Cache) -> None:
        self._http = http
        self._cik = cik
        self._cache = cache

    async def get_8k_events(
        self, ticker: str, as_of: date, *, lookback_days: int = 365
    ) -> list[EightKEvent] | DataUnavailable:
        """Returns 8-K filings on/before `as_of`, back to `as_of - lookback_days`.

        NOTE (Phase 1 simplification, documented not silent): `submissions`
        JSON's `filings.recent` only covers roughly the most recent ~1000
        filings or 1 year. Older `as_of` windows would require following
        `filings.files` pagination pointers to older archived JSON files —
        not implemented yet. A window that falls entirely outside
        `filings.recent` returns `DataUnavailable` rather than silently
        returning an incomplete result.
        """
        try:
            cik = await self._cik.resolve(ticker, as_of)
        except CikNotFoundError:
            return DataUnavailable(
                source="sec_8k",
                identifier=ticker,
                as_of=as_of,
                reason=f"No CIK found for ticker {ticker!r}",
                attempted_at=datetime.now(UTC),
            )

        raw = await self._get_raw_submissions(cik)
        window_start = as_of.fromordinal(as_of.toordinal() - lookback_days)
        events = _parse_8k_events(raw, as_of=as_of, window_start=window_start)

        recent = raw.get("filings", {}).get("recent", {})
        oldest_recent_date = _oldest_filing_date(recent)
        if oldest_recent_date is not None and oldest_recent_date > window_start:
            return DataUnavailable(
                source="sec_8k",
                identifier=ticker,
                as_of=as_of,
                reason=(
                    f"Requested window starting {window_start.isoformat()} predates "
                    f"filings.recent's coverage (oldest available: "
                    f"{oldest_recent_date.isoformat()}); older-archive pagination "
                    "via filings.files is not implemented in Phase 1"
                ),
                attempted_at=datetime.now(UTC),
            )

        if not events:
            return DataUnavailable(
                source="sec_8k",
                identifier=ticker,
                as_of=as_of,
                reason=f"No 8-K filings found for {ticker!r} in the requested window",
                attempted_at=datetime.now(UTC),
            )
        return events

    async def _get_raw_submissions(self, cik: str) -> dict[str, Any]:
        return await fetch_submissions(self._http, self._cache, cik)


def _oldest_filing_date(recent: dict[str, Any]) -> date | None:
    dates = recent.get("filingDate", [])
    if not dates:
        return None
    return min(date.fromisoformat(d) for d in dates)


def _parse_8k_events(raw: dict[str, Any], *, as_of: date, window_start: date) -> list[EightKEvent]:
    recent = raw.get("filings", {}).get("recent", {})
    forms: list[str] = recent.get("form", [])
    filing_dates: list[str] = recent.get("filingDate", [])
    accession_numbers: list[str] = recent.get("accessionNumber", [])
    items_raw: list[str] = recent.get("items", [])

    events: list[EightKEvent] = []
    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        filing_date = date.fromisoformat(filing_dates[i])
        if filing_date > as_of or filing_date < window_start:
            continue
        # `items` is a comma-separated STRING per filing (e.g. "2.02,9.01"),
        # not a JSON array — split it ourselves. It can also be empty.
        item_codes = [code for code in items_raw[i].split(",") if code]
        events.append(
            EightKEvent(
                accession_number=accession_numbers[i],
                filing_date=filing_date,
                items=item_codes,
                item_labels=[ITEM_CODE_LABELS.get(code, "Unknown item") for code in item_codes],
            )
        )
    return events
