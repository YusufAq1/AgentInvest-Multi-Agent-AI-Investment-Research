"""The cached SEC `submissions` JSON fetch, shared by every client that
reads it (news.py for 8-K events, company.py for name/SIC).

WHY its own module: originally private to NewsClient (Phase 1). Phase 5's
company profile needs the same document. Two separate fetches would mean
two cache entries and two SEC requests for one identical file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backend.data.cache import Cache
from backend.data.http import SecHttpClient

CACHE_SOURCE = "sec_submissions"


async def fetch_submissions(http: SecHttpClient, cache: Cache, cik: str) -> dict[str, Any]:
    """Returns the full, current `submissions` JSON for `cik`.

    Same fetch-day cache-key approach as xbrl.py: `submissions` has no
    upstream as_of parameter, so every caller applies its own as_of filter
    in Python on every read, never trusting the cache to have done it.
    """
    fetch_day = datetime.now(UTC).date()
    args = {"cik": cik}
    cached = await cache.get(source=CACHE_SOURCE, args=args, as_of=fetch_day)
    if cached is not None:
        return cached

    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    raw = await http.get_json(url)
    await cache.set(source=CACHE_SOURCE, args=args, as_of=fetch_day, payload=raw)
    return raw
