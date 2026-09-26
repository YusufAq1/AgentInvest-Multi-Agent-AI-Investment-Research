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
from backend.core.config import ModelPricing
from backend.core.llm import (
    ClaudeAuthenticationError,
    ClaudeClient,
    ClaudeInvalidRequestError,
    ClaudeRateLimitError,
    ClaudeRefusalError,
    ClaudeTruncatedToolCallError,
    LLMCallResult,
    StructuredOutputError,
    compute_cost,
    total_cost,
)
from pydantic import BaseModel

from tests.conftest import make_settings

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


def _fake_message(text: str = "hello", **usage_kwargs: int) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=_usage(**usage_kwargs),
    )


def test_claude_client_call_parses_response_and_computes_cost() -> None:
    settings = make_settings()
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
    settings = make_settings()
    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.return_value = _fake_message()

    client = ClaudeClient(settings, client=mock_sdk_client)
    client.call(agent="test_agent", messages=[], model="claude-sonnet-5")

    _, call_kwargs = mock_sdk_client.messages.create.call_args
    assert call_kwargs["model"] == "claude-sonnet-5"


def test_claude_client_unpriced_model_raises_invalid_request() -> None:
    settings = make_settings()
    client = ClaudeClient(settings, client=MagicMock())

    with pytest.raises(ClaudeInvalidRequestError):
        client.call(agent="test_agent", messages=[], model="claude-opus-does-not-exist")


def _api_status_error(exc_type: type[anthropic.APIStatusError], status_code: int) -> Exception:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status_code, request=request)
    return exc_type("boom", response=response, body=None)


def test_claude_client_maps_authentication_error() -> None:
    settings = make_settings()
    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.side_effect = _api_status_error(
        anthropic.AuthenticationError, 401
    )
    client = ClaudeClient(settings, client=mock_sdk_client)

    with pytest.raises(ClaudeAuthenticationError):
        client.call(agent="test_agent", messages=[])


def test_claude_client_maps_rate_limit_error() -> None:
    settings = make_settings()
    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.side_effect = _api_status_error(anthropic.RateLimitError, 429)
    client = ClaudeClient(settings, client=mock_sdk_client)

    with pytest.raises(ClaudeRateLimitError):
        client.call(agent="test_agent", messages=[])


# ---- call_structured: forced tool use, Pydantic validation, retry-once ----


class _Point(BaseModel):
    x: int
    y: int


def _fake_tool_response(
    tool_input: dict[str, object],
    *,
    tool_use_id: str = "toolu_1",
    stop_reason: str = "tool_use",
    **usage_kwargs: int,
) -> SimpleNamespace:
    content = [
        SimpleNamespace(type="tool_use", id=tool_use_id, name="emit_point", input=tool_input)
    ]
    return SimpleNamespace(content=content, usage=_usage(**usage_kwargs), stop_reason=stop_reason)


def test_call_structured_returns_parsed_model_on_first_success() -> None:
    settings = make_settings()
    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.return_value = _fake_tool_response({"x": 1, "y": 2})
    client = ClaudeClient(settings, client=mock_sdk_client)

    result = client.call_structured(
        agent="test_agent",
        model_cls=_Point,
        messages=[{"role": "user", "content": "go"}],
        tool_name="emit_point",
        tool_description="emit a point",
    )

    assert result == _Point(x=1, y=2)
    assert mock_sdk_client.messages.create.call_count == 1


def test_call_structured_retries_once_then_succeeds() -> None:
    settings = make_settings()
    mock_sdk_client = MagicMock()
    bad = _fake_tool_response({"x": 1}, tool_use_id="toolu_bad")  # missing required "y"
    good = _fake_tool_response({"x": 1, "y": 2})
    mock_sdk_client.messages.create.side_effect = [bad, good]
    client = ClaudeClient(settings, client=mock_sdk_client)

    result = client.call_structured(
        agent="test_agent",
        model_cls=_Point,
        messages=[{"role": "user", "content": "go"}],
        tool_name="emit_point",
        tool_description="emit a point",
    )

    assert result == _Point(x=1, y=2)
    assert mock_sdk_client.messages.create.call_count == 2
    retry_kwargs = mock_sdk_client.messages.create.call_args_list[1].kwargs
    tool_result_block = retry_kwargs["messages"][-1]["content"][0]
    assert tool_result_block["type"] == "tool_result"
    assert tool_result_block["tool_use_id"] == "toolu_bad"
    assert tool_result_block["is_error"] is True


def test_call_structured_fails_twice_raises_structured_output_error() -> None:
    settings = make_settings()
    mock_sdk_client = MagicMock()
    bad1 = _fake_tool_response({"x": 1}, tool_use_id="toolu_bad1")
    bad2 = _fake_tool_response({"y": 2}, tool_use_id="toolu_bad2")
    mock_sdk_client.messages.create.side_effect = [bad1, bad2]
    client = ClaudeClient(settings, client=mock_sdk_client)

    with pytest.raises(StructuredOutputError) as exc_info:
        client.call_structured(
            agent="test_agent",
            model_cls=_Point,
            messages=[{"role": "user", "content": "go"}],
            tool_name="emit_point",
            tool_description="emit a point",
        )

    assert exc_info.value.raw_input == {"y": 2}
    assert mock_sdk_client.messages.create.call_count == 2


def test_call_structured_extra_validation_failure_triggers_retry() -> None:
    settings = make_settings()
    mock_sdk_client = MagicMock()
    first = _fake_tool_response({"x": 1, "y": 2}, tool_use_id="toolu_1")  # schema-valid
    second = _fake_tool_response({"x": 5, "y": 6}, tool_use_id="toolu_2")
    mock_sdk_client.messages.create.side_effect = [first, second]
    client = ClaudeClient(settings, client=mock_sdk_client)

    def extra_validation(point: _Point) -> None:
        if point.x != 5:
            raise ValueError("x must be 5")

    result = client.call_structured(
        agent="test_agent",
        model_cls=_Point,
        messages=[{"role": "user", "content": "go"}],
        tool_name="emit_point",
        tool_description="emit a point",
        extra_validation=extra_validation,
    )

    assert result == _Point(x=5, y=6)
    assert mock_sdk_client.messages.create.call_count == 2


def test_call_structured_refusal_raises_without_retry() -> None:
    settings = make_settings()
    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.return_value = _fake_tool_response(
        {"x": 1, "y": 2}, stop_reason="refusal"
    )
    client = ClaudeClient(settings, client=mock_sdk_client)

    with pytest.raises(ClaudeRefusalError):
        client.call_structured(
            agent="test_agent",
            model_cls=_Point,
            messages=[{"role": "user", "content": "go"}],
            tool_name="emit_point",
            tool_description="emit a point",
        )
    assert mock_sdk_client.messages.create.call_count == 1


def test_call_structured_truncated_raises_without_retry() -> None:
    settings = make_settings()
    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.return_value = _fake_tool_response(
        {"x": 1, "y": 2}, stop_reason="max_tokens"
    )
    client = ClaudeClient(settings, client=mock_sdk_client)

    with pytest.raises(ClaudeTruncatedToolCallError):
        client.call_structured(
            agent="test_agent",
            model_cls=_Point,
            messages=[{"role": "user", "content": "go"}],
            tool_name="emit_point",
            tool_description="emit a point",
        )
    assert mock_sdk_client.messages.create.call_count == 1


def test_call_structured_builds_tool_schema_from_model() -> None:
    settings = make_settings()
    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.return_value = _fake_tool_response({"x": 1, "y": 2})
    client = ClaudeClient(settings, client=mock_sdk_client)

    client.call_structured(
        agent="test_agent",
        model_cls=_Point,
        messages=[{"role": "user", "content": "go"}],
        tool_name="emit_point",
        tool_description="emit a point",
    )

    call_kwargs = mock_sdk_client.messages.create.call_args.kwargs
    assert call_kwargs["tool_choice"] == {"type": "tool", "name": "emit_point"}
    tool = call_kwargs["tools"][0]
    assert tool["name"] == "emit_point"
    assert tool["input_schema"] == _Point.model_json_schema()


def test_on_result_fires_once_per_api_call_including_the_retry() -> None:
    """Phase 5's per-run cost comes from this callback, so a retry must be
    reported as the second paid call it really is, not folded into one."""
    settings = make_settings()
    mock_sdk_client = MagicMock()
    bad = _fake_tool_response({"x": 1}, tool_use_id="toolu_bad", input_tokens=100)
    good = _fake_tool_response({"x": 1, "y": 2}, input_tokens=200)
    mock_sdk_client.messages.create.side_effect = [bad, good]
    seen: list[LLMCallResult] = []
    client = ClaudeClient(settings, client=mock_sdk_client, on_result=seen.append)

    client.call_structured(
        agent="test_agent",
        model_cls=_Point,
        messages=[{"role": "user", "content": "go"}],
        tool_name="emit_point",
        tool_description="emit a point",
    )

    assert [r.input_tokens for r in seen] == [100, 200]
    assert all(r.agent == "test_agent" for r in seen)


def test_total_cost_sums_hand_worked_values() -> None:
    def _result(cost: str) -> LLMCallResult:
        return LLMCallResult(
            agent="a",
            model="m",
            input_tokens=0,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            latency_ms=0.0,
            cost_usd=Decimal(cost),
            text="",
        )

    assert total_cost([]) == Decimal("0")
    assert total_cost([_result("0.001250"), _result("0.000500")]) == Decimal("0.001750")
