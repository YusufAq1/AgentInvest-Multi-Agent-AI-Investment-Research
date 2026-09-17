"""Tests for backend.data.xbrl — including the single most important test
in this phase: proving as_of filtering uses `filed`, never `end`/`fy`/`fp`.
"""

from datetime import date
from pathlib import Path

import respx
from backend.data.cache import Cache
from backend.data.cik import CikResolver
from backend.data.http import SecHttpClient
from backend.data.models import DataUnavailable
from backend.data.xbrl import XBRLClient

from tests.data.conftest import make_settings

_TICKERS_FIXTURE = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}

_COMPANYFACTS_FIXTURE = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "label": "Revenues",
                "units": {
                    "USD": [
                        {
                            # Period looks safely in the past...
                            "start": "2023-01-01",
                            "end": "2023-12-31",
                            "val": 100,
                            "accn": "PRE-AS-OF",
                            "fy": 2023,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2024-02-01",  # ...and filed before as_of.
                        },
                        {
                            # Period ALSO looks in range if you (wrongly)
                            # filter on `end`/`fy`/`fp` — but this was
                            # actually FILED after as_of, so it must be
                            # excluded. This is the look-ahead-bias bug
                            # class C3 exists to prevent.
                            "start": "2024-04-01",
                            "end": "2024-06-30",
                            "val": 110,
                            "accn": "POST-AS-OF",
                            "fy": 2024,
                            "fp": "Q3",
                            "form": "10-Q",
                            "filed": "2024-08-01",
                        },
                    ]
                },
            }
        }
    },
}


def _make_client(tmp_path: Path) -> tuple[XBRLClient, SecHttpClient]:
    http = SecHttpClient(make_settings())
    cache = Cache(tmp_path / "cache.sqlite3")
    cik = CikResolver(http, cache)
    return XBRLClient(http, cik, cache), http


@respx.mock
async def test_get_company_facts_excludes_facts_filed_after_as_of(tmp_path: Path) -> None:
    respx.get("https://www.sec.gov/files/company_tickers.json").respond(json=_TICKERS_FIXTURE)
    respx.get("https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json").respond(
        json=_COMPANYFACTS_FIXTURE
    )
    client, http = _make_client(tmp_path)
    as_of = date(2024, 6, 30)

    result = await client.get_company_facts("AAPL", as_of)

    assert not isinstance(result, DataUnavailable)
    assert all(fact.filed <= as_of for fact in result)
    assert not any(fact.accession_number == "POST-AS-OF" for fact in result)
    assert any(fact.accession_number == "PRE-AS-OF" for fact in result)
    await http.aclose()


@respx.mock
async def test_get_company_facts_filters_by_concept(tmp_path: Path) -> None:
    respx.get("https://www.sec.gov/files/company_tickers.json").respond(json=_TICKERS_FIXTURE)
    respx.get("https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json").respond(
        json=_COMPANYFACTS_FIXTURE
    )
    client, http = _make_client(tmp_path)

    result = await client.get_company_facts(
        "AAPL", date(2024, 6, 30), concepts=("NonexistentConcept",)
    )

    assert isinstance(result, DataUnavailable)
    assert result.source == "xbrl"
    await http.aclose()


@respx.mock
async def test_get_company_facts_unknown_ticker_returns_data_unavailable(tmp_path: Path) -> None:
    respx.get("https://www.sec.gov/files/company_tickers.json").respond(json=_TICKERS_FIXTURE)
    client, http = _make_client(tmp_path)

    result = await client.get_company_facts("NOTATICKER", date(2024, 6, 30))

    assert isinstance(result, DataUnavailable)
    assert result.source == "xbrl"
    assert result.identifier == "NOTATICKER"
    await http.aclose()


@respx.mock
async def test_companyfacts_raw_response_is_cached(tmp_path: Path) -> None:
    respx.get("https://www.sec.gov/files/company_tickers.json").respond(json=_TICKERS_FIXTURE)
    route = respx.get("https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json").respond(
        json=_COMPANYFACTS_FIXTURE
    )
    client, http = _make_client(tmp_path)

    await client.get_company_facts("AAPL", date(2024, 6, 30))
    # A second call with a DIFFERENT as_of must still hit the cache, not
    # refetch — the raw snapshot is cached by fetch day, and the as_of
    # filter is reapplied in Python on every read (see xbrl.py docstring).
    await client.get_company_facts("AAPL", date(2024, 1, 1))

    assert route.call_count == 1
    await http.aclose()
