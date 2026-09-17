"""Tests for backend.data.http — no real network calls, ever (respx mocks
httpx.AsyncClient).
"""

import time

import httpx
import pytest
import respx
from backend.data.errors import PermanentDataError, SecIdentityRejectedError, TransientDataError
from backend.data.http import SecHttpClient, SecRateLimiter

from tests.data.conftest import make_settings


async def test_rate_limiter_enforces_minimum_spacing() -> None:
    limiter = SecRateLimiter(requests_per_second=10)  # 100ms minimum spacing

    start = time.monotonic()
    await limiter.acquire()
    await limiter.acquire()
    await limiter.acquire()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.2  # two waits of ~100ms each


@respx.mock
async def test_get_json_returns_parsed_json() -> None:
    respx.get("https://data.sec.gov/fake.json").respond(json={"hello": "world"})
    client = SecHttpClient(make_settings())

    result = await client.get_json("https://data.sec.gov/fake.json")

    assert result == {"hello": "world"}
    await client.aclose()


@respx.mock
async def test_get_json_maps_403_to_sec_identity_rejected() -> None:
    respx.get("https://data.sec.gov/fake.json").respond(status_code=403)
    client = SecHttpClient(make_settings())

    with pytest.raises(SecIdentityRejectedError):
        await client.get_json("https://data.sec.gov/fake.json")
    await client.aclose()


@respx.mock
async def test_get_json_retries_then_raises_on_persistent_500() -> None:
    route = respx.get("https://data.sec.gov/fake.json").respond(status_code=500)
    client = SecHttpClient(make_settings(data_retry_max_attempts=3))

    with pytest.raises(TransientDataError):
        await client.get_json("https://data.sec.gov/fake.json")

    assert route.call_count == 3
    await client.aclose()


@respx.mock
async def test_get_json_maps_404_to_permanent_error() -> None:
    respx.get("https://data.sec.gov/fake.json").respond(status_code=404)
    client = SecHttpClient(make_settings())

    with pytest.raises(PermanentDataError):
        await client.get_json("https://data.sec.gov/fake.json")
    await client.aclose()


@respx.mock
async def test_get_json_sets_user_agent_header() -> None:
    route = respx.get("https://data.sec.gov/fake.json").respond(json={})
    client = SecHttpClient(make_settings(edgar_identity="AgentInvest Test test@example.com"))

    await client.get_json("https://data.sec.gov/fake.json")

    assert route.calls.last.request.headers["User-Agent"] == "AgentInvest Test test@example.com"
    await client.aclose()


async def test_injected_client_is_used_directly() -> None:
    # Confirms the constructor's `client` override is actually wired
    # through, the same injection seam every test above relies on via
    # respx patching the default transport.
    injected = httpx.AsyncClient()
    wrapper = SecHttpClient(make_settings(), client=injected)

    assert wrapper._client is injected  # testing the injection seam itself
    await injected.aclose()
