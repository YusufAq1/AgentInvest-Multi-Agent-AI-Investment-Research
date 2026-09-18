"""SEC XBRL companyfacts — the source of truth for structured fundamentals.

WHY this exists separately from edgar.py: CLAUDE.md §4/ADR-0005(-to-be)
draws a hard line — numbers come from XBRL, never from parsing filing text.
edgar.py is for narrative sections (Item 1A, MD&A); this module is for
actual figures (revenue, assets, ...).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from backend.data.cache import Cache
from backend.data.cik import CikResolver
from backend.data.errors import CikNotFoundError, UpstreamSchemaError
from backend.data.http import SecHttpClient
from backend.data.models import DataUnavailable, XBRLCompanyFacts, XBRLFact

_CACHE_SOURCE = "xbrl_companyfacts"


class XBRLClient:
    """Fetches and parses SEC XBRL companyfacts for one ticker."""

    def __init__(self, http: SecHttpClient, cik: CikResolver, cache: Cache) -> None:
        self._http = http
        self._cik = cik
        self._cache = cache

    async def get_company_facts(
        self,
        ticker: str,
        as_of: date,
        *,
        concepts: tuple[str, ...] | None = None,
    ) -> list[XBRLFact] | DataUnavailable:
        """Returns every XBRL fact filed on or before `as_of` (optionally
        restricted to `concepts`, e.g. `("Revenues", "Assets")`).

        WHY filtering on `filed`, never `end`/`fy`/`fp`: those describe the
        fiscal period a number reports on, not when it became public
        knowledge. A FY2023 10-K might be `filed` in early 2024 — with
        `as_of` in December 2023, it must be excluded even though the
        period fields look "in range." Filtering on anything but `filed`
        would leak future information into a point-in-time query — the
        exact bug class C3 exists to prevent.
        """
        result = await self.get_company_facts_with_raw(ticker, as_of, concepts=concepts)
        if isinstance(result, DataUnavailable):
            return result
        return result.facts

    async def get_company_facts_with_raw(
        self,
        ticker: str,
        as_of: date,
        *,
        concepts: tuple[str, ...] | None = None,
    ) -> XBRLCompanyFacts | DataUnavailable:
        """Same as `get_company_facts`, but also returns the raw
        companyfacts JSON payload the facts were parsed from.

        WHY this exists: a caller that needs to build or verify a
        byte-exact citation `quote` (the Financial Agent, and
        backend/evidence/validation.py's xbrl_fact containment check) needs
        the raw JSON, not just the parsed facts. See `find_raw_entry`
        below for locating one fact's exact raw dict within `raw`.
        """
        try:
            cik = await self._cik.resolve(ticker, as_of)
        except CikNotFoundError:
            return DataUnavailable(
                source="xbrl",
                identifier=ticker,
                as_of=as_of,
                reason=f"No CIK found for ticker {ticker!r}",
                attempted_at=datetime.now(UTC),
            )

        raw = await self._get_raw_companyfacts(cik)
        if raw is None:
            return DataUnavailable(
                source="xbrl",
                identifier=ticker,
                as_of=as_of,
                reason=f"No XBRL companyfacts available for CIK {cik}",
                attempted_at=datetime.now(UTC),
            )

        facts = _parse_facts(raw, as_of=as_of, concepts=concepts)
        if not facts:
            return DataUnavailable(
                source="xbrl",
                identifier=ticker,
                as_of=as_of,
                reason=f"No XBRL facts filed on/before {as_of.isoformat()}",
                attempted_at=datetime.now(UTC),
            )
        return XBRLCompanyFacts(facts=facts, raw=raw)

    async def _get_raw_companyfacts(self, cik: str) -> dict[str, Any] | None:
        # WHY date.today() as the cache key, not the query's real as_of:
        # companyfacts has no upstream as_of parameter — SEC always returns
        # the full current history. We cache that raw snapshot keyed by
        # fetch day (approximating cache_snapshot_ttl_hours, default 24h)
        # and apply the real as_of filter in Python on EVERY read, cache
        # hit or miss alike — see _parse_facts below.
        fetch_day = datetime.now(UTC).date()
        args = {"cik": cik}
        cached = await self._cache.get(source=_CACHE_SOURCE, args=args, as_of=fetch_day)
        if cached is not None:
            return cached

        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        raw = await self._http.get_json(url)
        await self._cache.set(source=_CACHE_SOURCE, args=args, as_of=fetch_day, payload=raw)
        return raw


def find_raw_entry(raw: dict[str, Any], fact: XBRLFact) -> dict[str, Any]:
    """Locates the exact raw fact dict a parsed `XBRLFact` came from.

    WHY this is needed: backend/evidence/validation.py's xbrl_fact
    containment check re-serializes the FULL raw payload with
    `json.dumps(sort_keys=True, separators=(",", ":"))` and requires this
    exact entry's identical serialization to appear as a substring. The
    Financial Agent must build its citation `quote` from THIS dict, not a
    reconstructed approximation, or the two serializations won't match
    byte-for-byte.

    Raises `UpstreamSchemaError` if no matching entry is found — this
    would mean the fact was parsed from a payload that no longer matches
    `raw` (a caller bug, not a data-availability problem).
    """
    try:
        entries: list[dict[str, Any]] = raw["facts"][fact.taxonomy][fact.concept]["units"][
            fact.unit
        ]
    except KeyError as exc:
        raise UpstreamSchemaError(
            f"raw payload has no {fact.taxonomy}/{fact.concept}/{fact.unit} entries"
        ) from exc

    expected_start = fact.period_start.isoformat() if fact.period_start else None
    for entry in entries:
        if (
            entry["accn"] == fact.accession_number
            and entry["end"] == fact.period_end.isoformat()
            and entry.get("start") == expected_start
        ):
            return entry
    raise UpstreamSchemaError(
        f"could not relocate raw entry for {fact.concept} accession {fact.accession_number}"
    )


def _parse_facts(
    raw: dict[str, Any], *, as_of: date, concepts: tuple[str, ...] | None
) -> list[XBRLFact]:
    facts: list[XBRLFact] = []
    taxonomies: dict[str, Any] = raw.get("facts", {})
    for taxonomy_name in ("us-gaap", "dei"):
        concept_map: dict[str, Any] = taxonomies.get(taxonomy_name, {})
        for concept_name, concept_data in concept_map.items():
            if concepts is not None and concept_name not in concepts:
                continue
            units: dict[str, Any] = concept_data.get("units", {})
            for unit_name, entries in units.items():
                for entry in entries:
                    filed = date.fromisoformat(entry["filed"])
                    if filed > as_of:
                        continue
                    facts.append(
                        XBRLFact(
                            concept=concept_name,
                            taxonomy=taxonomy_name,
                            unit=unit_name,
                            value=float(entry["val"]),
                            period_start=(
                                date.fromisoformat(entry["start"]) if "start" in entry else None
                            ),
                            period_end=date.fromisoformat(entry["end"]),
                            fiscal_year=entry.get("fy"),
                            fiscal_period=entry.get("fp"),
                            form=entry["form"],
                            filed=filed,
                            accession_number=entry["accn"],
                        )
                    )
    return facts
