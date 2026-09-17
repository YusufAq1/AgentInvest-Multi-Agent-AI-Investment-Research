"""Tests for backend.data.news (8-K material events)."""

from datetime import date
from pathlib import Path

import respx
from backend.data.cache import Cache
from backend.data.cik import CikResolver
from backend.data.http import SecHttpClient
from backend.data.models import DataUnavailable
from backend.data.news import NewsClient

from tests.data.conftest import make_settings

_TICKERS_FIXTURE = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}

_SUBMISSIONS_FIXTURE = {
    "cik": "320193",
    "filings": {
        "recent": {
            "form": ["8-K", "10-K", "8-K"],
            "filingDate": ["2024-05-01", "2024-03-01", "2024-08-01"],
            "accessionNumber": ["PRE-AS-OF", "OTHER-FORM", "POST-AS-OF"],
            "items": ["2.02,9.01", "", "5.02"],
        },
        "files": [],
    },
}


def _make_client(tmp_path: Path) -> tuple[NewsClient, SecHttpClient]:
    http = SecHttpClient(make_settings())
    cache = Cache(tmp_path / "cache.sqlite3")
    cik = CikResolver(http, cache)
    return NewsClient(http, cik, cache), http


@respx.mock
async def test_get_8k_events_excludes_filings_after_as_of(tmp_path: Path) -> None:
    respx.get("https://www.sec.gov/files/company_tickers.json").respond(json=_TICKERS_FIXTURE)
    respx.get("https://data.sec.gov/submissions/CIK0000320193.json").respond(
        json=_SUBMISSIONS_FIXTURE
    )
    client, http = _make_client(tmp_path)
    as_of = date(2024, 6, 30)

    result = await client.get_8k_events("AAPL", as_of, lookback_days=120)

    assert not isinstance(result, DataUnavailable)
    assert all(event.filing_date <= as_of for event in result)
    assert not any(event.accession_number == "POST-AS-OF" for event in result)
    assert any(event.accession_number == "PRE-AS-OF" for event in result)
    await http.aclose()


@respx.mock
async def test_get_8k_events_excludes_non_8k_forms(tmp_path: Path) -> None:
    respx.get("https://www.sec.gov/files/company_tickers.json").respond(json=_TICKERS_FIXTURE)
    respx.get("https://data.sec.gov/submissions/CIK0000320193.json").respond(
        json=_SUBMISSIONS_FIXTURE
    )
    client, http = _make_client(tmp_path)

    result = await client.get_8k_events("AAPL", date(2024, 6, 30), lookback_days=120)

    assert not isinstance(result, DataUnavailable)
    assert not any(event.accession_number == "OTHER-FORM" for event in result)
    await http.aclose()


@respx.mock
async def test_item_codes_are_split_from_comma_separated_string(tmp_path: Path) -> None:
    respx.get("https://www.sec.gov/files/company_tickers.json").respond(json=_TICKERS_FIXTURE)
    respx.get("https://data.sec.gov/submissions/CIK0000320193.json").respond(
        json=_SUBMISSIONS_FIXTURE
    )
    client, http = _make_client(tmp_path)

    result = await client.get_8k_events("AAPL", date(2024, 6, 30), lookback_days=120)

    assert not isinstance(result, DataUnavailable)
    pre_as_of = next(e for e in result if e.accession_number == "PRE-AS-OF")
    assert pre_as_of.items == ["2.02", "9.01"]
    assert pre_as_of.item_labels == [
        "Results of Operations and Financial Condition",
        "Financial Statements and Exhibits",
    ]
    await http.aclose()


@respx.mock
async def test_window_predating_filings_recent_returns_data_unavailable(tmp_path: Path) -> None:
    respx.get("https://www.sec.gov/files/company_tickers.json").respond(json=_TICKERS_FIXTURE)
    respx.get("https://data.sec.gov/submissions/CIK0000320193.json").respond(
        json=_SUBMISSIONS_FIXTURE
    )
    client, http = _make_client(tmp_path)

    # Requested window starts long before the oldest filing in `recent`
    # (2024-03-01) — Phase 1 doesn't paginate into filings.files yet.
    result = await client.get_8k_events("AAPL", date(2024, 6, 30), lookback_days=3650)

    assert isinstance(result, DataUnavailable)
    assert result.source == "sec_8k"
    await http.aclose()
