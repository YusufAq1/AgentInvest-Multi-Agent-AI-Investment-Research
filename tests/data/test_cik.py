"""Tests for backend.data.cik."""

from datetime import date
from pathlib import Path

import pytest
import respx
from backend.data.cache import Cache
from backend.data.cik import CikResolver
from backend.data.errors import CikNotFoundError
from backend.data.http import SecHttpClient

from tests.data.conftest import make_settings

_TICKERS_FIXTURE = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
}


@respx.mock
async def test_resolve_pads_cik_to_ten_digits(tmp_path: Path) -> None:
    respx.get("https://www.sec.gov/files/company_tickers.json").respond(json=_TICKERS_FIXTURE)
    http = SecHttpClient(make_settings())
    cache = Cache(tmp_path / "cache.sqlite3")
    resolver = CikResolver(http, cache)

    cik = await resolver.resolve("aapl", date(2024, 6, 30))

    assert cik == "0000320193"
    await http.aclose()


@respx.mock
async def test_resolve_unknown_ticker_raises_cik_not_found(tmp_path: Path) -> None:
    respx.get("https://www.sec.gov/files/company_tickers.json").respond(json=_TICKERS_FIXTURE)
    http = SecHttpClient(make_settings())
    cache = Cache(tmp_path / "cache.sqlite3")
    resolver = CikResolver(http, cache)

    with pytest.raises(CikNotFoundError):
        await resolver.resolve("NOTATICKER", date(2024, 6, 30))
    await http.aclose()


@respx.mock
async def test_mapping_is_cached_across_calls(tmp_path: Path) -> None:
    route = respx.get("https://www.sec.gov/files/company_tickers.json").respond(
        json=_TICKERS_FIXTURE
    )
    http = SecHttpClient(make_settings())
    cache = Cache(tmp_path / "cache.sqlite3")
    resolver = CikResolver(http, cache)

    await resolver.resolve("AAPL", date(2024, 6, 30))
    await resolver.resolve("MSFT", date(2024, 6, 30))

    assert route.call_count == 1
    await http.aclose()
