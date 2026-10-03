"""Company profile (name, SIC) from SEC `submissions`, resolved as of a date.

WHY this exists: the Phase 5 Research Manager needs to know *what kind of
company* a ticker is to plan research (a bank and a chipmaker warrant
different Filings questions). It's the only company-specific input the
Manager gets (CLAUDE.md C7). It comes from SEC's own structured metadata,
never from retrieved news or filing text.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from backend.data.cache import Cache
from backend.data.cik import CikResolver
from backend.data.errors import CikNotFoundError
from backend.data.http import SecHttpClient
from backend.data.models import CompanyProfile, DataUnavailable
from backend.data.submissions import fetch_submissions


def resolve_name_as_of(raw: dict[str, Any], as_of: date) -> str:
    """The company's name as it was on `as_of`.

    SEC's `formerNames` lists `{"name", "from", "to"}` entries with ISO
    timestamps. If `as_of` falls inside one of those ranges, that was the
    name then. Otherwise the current `name` applies.

    WHY this matters (C3): an as_of=2006 run for AAPL should be planning
    research on "APPLE COMPUTER INC". Showing the model today's name would
    leak a fact (the 2007 rename) from after as_of.

    Known gap: if as_of predates every recorded name (before the company's
    earliest EDGAR record), this returns the current name.
    """
    for former in raw.get("formerNames") or []:
        start = _parse_sec_date(former.get("from"))
        end = _parse_sec_date(former.get("to"))
        if start is not None and end is not None and start <= as_of <= end:
            return str(former["name"])
    return str(raw["name"])


def _parse_sec_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])


class CompanyClient:
    """Fetches a ticker's company profile, resolved as of a date."""

    def __init__(self, http: SecHttpClient, cik: CikResolver, cache: Cache) -> None:
        self._http = http
        self._cik = cik
        self._cache = cache

    async def get_company_profile(
        self, ticker: str, as_of: date
    ) -> CompanyProfile | DataUnavailable:
        try:
            cik = await self._cik.resolve(ticker, as_of)
        except CikNotFoundError:
            return DataUnavailable(
                source="sec_submissions",
                identifier=ticker,
                as_of=as_of,
                reason=f"No CIK found for ticker {ticker!r}",
                attempted_at=datetime.now(UTC),
            )

        raw = await fetch_submissions(self._http, self._cache, cik)
        if "name" not in raw:
            return DataUnavailable(
                source="sec_submissions",
                identifier=ticker,
                as_of=as_of,
                reason=f"SEC submissions for CIK {cik} has no company name",
                attempted_at=datetime.now(UTC),
            )
        return CompanyProfile(
            ticker=ticker.upper(),
            cik=cik,
            name=resolve_name_as_of(raw, as_of),
            sic=raw.get("sic") or None,
            sic_description=raw.get("sicDescription") or None,
        )
