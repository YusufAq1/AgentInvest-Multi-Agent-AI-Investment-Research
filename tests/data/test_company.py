"""Tests for backend.data.company (name/SIC profile, resolved as of a date)."""

from datetime import date
from pathlib import Path

import respx
from backend.data.cache import Cache
from backend.data.cik import CikResolver
from backend.data.company import CompanyClient, resolve_name_as_of
from backend.data.http import SecHttpClient
from backend.data.models import DataUnavailable

from tests.data.conftest import make_settings

_TICKERS_FIXTURE = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}

# Shape taken from SEC's real submissions JSON for Apple (CIK 320193).
_SUBMISSIONS_FIXTURE = {
    "cik": "320193",
    "name": "Apple Inc.",
    "sic": "3571",
    "sicDescription": "Electronic Computers",
    "formerNames": [
        {
            "name": "APPLE COMPUTER INC",
            "from": "1994-01-26T00:00:00.000Z",
            "to": "2007-01-04T00:00:00.000Z",
        }
    ],
    "filings": {"recent": {}, "files": []},
}


def _make_client(tmp_path: Path) -> tuple[CompanyClient, SecHttpClient]:
    http = SecHttpClient(make_settings())
    cache = Cache(tmp_path / "cache.sqlite3")
    return CompanyClient(http, CikResolver(http, cache), cache), http


def test_name_inside_a_former_name_range_is_the_former_name() -> None:
    """C3: a 2006 run must see the name the company had in 2006."""
    assert resolve_name_as_of(_SUBMISSIONS_FIXTURE, date(2006, 6, 30)) == "APPLE COMPUTER INC"


def test_name_after_every_former_range_is_the_current_name() -> None:
    assert resolve_name_as_of(_SUBMISSIONS_FIXTURE, date(2024, 6, 30)) == "Apple Inc."


def test_range_boundaries_are_inclusive() -> None:
    assert resolve_name_as_of(_SUBMISSIONS_FIXTURE, date(2007, 1, 4)) == "APPLE COMPUTER INC"
    assert resolve_name_as_of(_SUBMISSIONS_FIXTURE, date(2007, 1, 5)) == "Apple Inc."


def test_missing_or_empty_former_names_uses_the_current_name() -> None:
    assert resolve_name_as_of({"name": "X Corp"}, date(2000, 1, 1)) == "X Corp"
    assert resolve_name_as_of({"name": "X Corp", "formerNames": None}, date(2000, 1, 1)) == (
        "X Corp"
    )


@respx.mock
async def test_get_company_profile_returns_as_of_name_and_sic(tmp_path: Path) -> None:
    respx.get("https://www.sec.gov/files/company_tickers.json").respond(json=_TICKERS_FIXTURE)
    respx.get("https://data.sec.gov/submissions/CIK0000320193.json").respond(
        json=_SUBMISSIONS_FIXTURE
    )
    client, http = _make_client(tmp_path)

    profile = await client.get_company_profile("aapl", date(2006, 6, 30))

    assert not isinstance(profile, DataUnavailable)
    assert profile.ticker == "AAPL"
    assert profile.cik == "0000320193"
    assert profile.name == "APPLE COMPUTER INC"
    assert profile.sic == "3571"
    assert profile.sic_description == "Electronic Computers"
    await http.aclose()


@respx.mock
async def test_unknown_ticker_returns_data_unavailable(tmp_path: Path) -> None:
    respx.get("https://www.sec.gov/files/company_tickers.json").respond(json=_TICKERS_FIXTURE)
    client, http = _make_client(tmp_path)

    profile = await client.get_company_profile("NOPE", date(2024, 6, 30))

    assert isinstance(profile, DataUnavailable)
    assert profile.source == "sec_submissions"
    await http.aclose()
