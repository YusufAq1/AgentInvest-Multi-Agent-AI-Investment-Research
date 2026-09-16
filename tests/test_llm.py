"""Tests for backend.core.llm.

CI must never spend money: every test here either calls `compute_cost`
directly (a pure function) or injects a `MagicMock` in place of the real
`anthropic.Anthropic` client. No test in this file constructs a real
`anthropic.Anthropic()` or reaches the network.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import httpx2  # anthropic 1.6.0's HTTP transport dependency, not classic `httpx`
import pytest
from backend.core.config import ModelPricing, Settings
from backend.core.llm import (
    ClaudeAuthenticationError,
    ClaudeClient,
    ClaudeInvalidRequestError,
    ClaudeRateLimitError,
    compute_cost,
)

PRICING = ModelPricing(
    input_per_mtok=Decimal("1.00"),
    output_per_mtok=Decimal("5.00"),
    cache_write_5m_per_mtok=Decimal("1.25"),
    cache_write_1h_per_mtok=Decimal("2.00"),
    cache_read_per_mtok=Decimal("0.10"),
)


def _usage(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )


def test_compute_cost_input_and_output_only() -> None:
    usage = _usage(input_tokens=1000, output_tokens=500)
    # 1000 * $1.00/Mtok + 500 * $5.00/Mtok = $0.001 + $0.0025 = $0.0035
    assert compute_cost(usage, PRICING) == Decimal("0.0035")


def test_compute_cost_includes_cache_terms() -> None:
    usage = _usage(
        input_tokens=1000,
        output_tokens=500,
        cache_creation_input_tokens=2000,
        cache_read_input_tokens=4000,
    )
    # base (as above): $0.0035
    # cache write: 2000 * $1.25/Mtok = $0.0025
    # cache read:  4000 * $0.10/Mtok = $0.0004
    expected = Decimal("0.0035") + Decimal("0.0025") + Decimal("0.0004")
    assert compute_cost(usage, PRICING) == expected


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "anthropic_api_key": "sk-test",
        "postgres_user": "agentinvest",
        "postgres_password": "changeme",
        "postgres_db": "agentinvest",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type,call-arg]


def _fake_message(text: str = "hello", **usage_kwargs: int) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=_usage(**usage_kwargs),
    )


def test_claude_client_call_parses_response_and_computes_cost() -> None:
    settings = _settings()
    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.return_value = _fake_message(
        text="hi there", input_tokens=100, output_tokens=50
    )

    client = ClaudeClient(settings, client=mock_sdk_client)
    result = client.call(agent="test_agent", messages=[{"role": "user", "content": "hi"}])

    assert result.agent == "test_agent"
    assert result.model == settings.default_model
    assert result.text == "hi there"
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.cache_creation_input_tokens == 0
    assert result.cache_read_input_tokens == 0

    expected_cost = compute_cost(
        _usage(input_tokens=100, output_tokens=50),
        settings.model_pricing[settings.default_model],
    )
    assert result.cost_usd == expected_cost


def test_claude_client_call_uses_model_override() -> None:
    settings = _settings()
    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.return_value = _fake_message()

    client = ClaudeClient(settings, client=mock_sdk_client)
    client.call(agent="test_agent", messages=[], model="claude-sonnet-5")

    _, call_kwargs = mock_sdk_client.messages.create.call_args
    assert call_kwargs["model"] == "claude-sonnet-5"


def test_claude_client_unpriced_model_raises_invalid_request() -> None:
    settings = _settings()
    client = ClaudeClient(settings, client=MagicMock())

    with pytest.raises(ClaudeInvalidRequestError):
        client.call(agent="test_agent", messages=[], model="claude-opus-does-not-exist")


def _api_status_error(exc_type: type[anthropic.APIStatusError], status_code: int) -> Exception:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status_code, request=request)
    return exc_type("boom", response=response, body=None)


def test_claude_client_maps_authentication_error() -> None:
    settings = _settings()
    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.side_effect = _api_status_error(
        anthropic.AuthenticationError, 401
    )
    client = ClaudeClient(settings, client=mock_sdk_client)

    with pytest.raises(ClaudeAuthenticationError):
        client.call(agent="test_agent", messages=[])


def test_claude_client_maps_rate_limit_error() -> None:
    settings = _settings()
    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.side_effect = _api_status_error(anthropic.RateLimitError, 429)
    client = ClaudeClient(settings, client=mock_sdk_client)

    with pytest.raises(ClaudeRateLimitError):
        client.call(agent="test_agent", messages=[])
