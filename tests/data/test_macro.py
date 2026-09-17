"""Tests for backend.data.macro (FRED)."""

from datetime import date
from pathlib import Path

import respx
from backend.data.cache import Cache
from backend.data.macro import MacroClient
from backend.data.models import DataUnavailable

from tests.data.conftest import make_settings

_OBSERVATIONS_FIXTURE = {
    "observations": [
        {"date": "2024-06-28", "value": "4.35"},
        {"date": "2024-06-29", "value": "."},  # FRED's own "missing" marker
    ]
}


@respx.mock
async def test_get_series_requests_both_observation_and_realtime_end(tmp_path: Path) -> None:
    route = respx.get("https://api.stlouisfed.org/fred/series/observations").respond(
        json=_OBSERVATIONS_FIXTURE
    )
    client = MacroClient(make_settings(), Cache(tmp_path / "cache.sqlite3"))
    as_of = date(2024, 6, 30)

    result = await client.get_series("risk_free_rate", as_of)

    assert not isinstance(result, DataUnavailable)
    request_params = dict(route.calls.last.request.url.params)
    assert request_params["observation_end"] == "2024-06-30"
    assert request_params["realtime_end"] == "2024-06-30"
    assert request_params["series_id"] == "DGS10"
    await client.aclose()


@respx.mock
async def test_get_series_skips_missing_value_marker(tmp_path: Path) -> None:
    respx.get("https://api.stlouisfed.org/fred/series/observations").respond(
        json=_OBSERVATIONS_FIXTURE
    )
    client = MacroClient(make_settings(), Cache(tmp_path / "cache.sqlite3"))

    result = await client.get_series("risk_free_rate", date(2024, 6, 30))

    assert not isinstance(result, DataUnavailable)
    assert len(result) == 1
    assert result[0].value == 4.35
    await client.aclose()


@respx.mock
async def test_get_series_empty_result_is_data_unavailable(tmp_path: Path) -> None:
    respx.get("https://api.stlouisfed.org/fred/series/observations").respond(
        json={"observations": []}
    )
    client = MacroClient(make_settings(), Cache(tmp_path / "cache.sqlite3"))

    result = await client.get_series("cpi", date(2024, 6, 30))

    assert isinstance(result, DataUnavailable)
    assert result.source == "fred"
    await client.aclose()


@respx.mock
async def test_real_gdp_uses_gdpc1_not_nominal_gdp(tmp_path: Path) -> None:
    route = respx.get("https://api.stlouisfed.org/fred/series/observations").respond(
        json=_OBSERVATIONS_FIXTURE
    )
    client = MacroClient(make_settings(), Cache(tmp_path / "cache.sqlite3"))

    await client.get_series("real_gdp", date(2024, 6, 30))

    assert dict(route.calls.last.request.url.params)["series_id"] == "GDPC1"
    await client.aclose()
