"""Claude client wrapper: cost tracking, structured logging, and a small
exception hierarchy that hides Anthropic SDK types from callers.

WHY this module exists (new-concept note, since agent loops are new to the
project's author): every specialist agent that gets built from Phase 2
onward will call Claude through this one wrapper rather than the raw SDK.
Centralizing it here is what makes CLAUDE.md §5's requirements enforceable
in one place instead of "please remember to log cost" scattered across a
dozen agent files: every call is priced from `Settings.model_pricing`
(never a hardcoded number, C_no-magic-numbers / §16), logged as one
structured line, and returns its cost so a caller can total it up.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Any, Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from backend.core.config import ModelPricing, Settings

logger = logging.getLogger("agentinvest.llm")

ModelT = TypeVar("ModelT", bound=BaseModel)


class AgentInvestError(Exception):
    """Base class for all AgentInvest-specific exceptions."""


class ClaudeAPIError(AgentInvestError):
    """Base class for errors raised while calling the Anthropic API.

    WHY a custom hierarchy instead of letting `anthropic.*` exceptions
    propagate: callers (agents, the manager, retry logic) should reason
    about "auth failed" vs "rate limited" vs "server is down" without
    depending on the Anthropic SDK's own exception names — that's an
    implementation detail of how we happen to call Claude today.
    """


class ClaudeAuthenticationError(ClaudeAPIError):
    """The API key was missing or rejected."""


class ClaudeRateLimitError(ClaudeAPIError):
    """The request was rate-limited (HTTP 429)."""


class ClaudeInvalidRequestError(ClaudeAPIError):
    """The request itself was malformed (bad params, unknown model id, etc.)."""


class ClaudeServerError(ClaudeAPIError):
    """Anthropic's API returned an error that wasn't our fault (5xx, and,
    as a Phase 0 simplification, any other 4xx not explicitly distinguished
    above — see the mapping in ClaudeClient.call)."""


class ClaudeConnectionError(ClaudeAPIError):
    """The request never reached Anthropic (network failure, timeout)."""


class ClaudeRefusalError(ClaudeAPIError):
    """Claude declined to answer (`stop_reason == "refusal"`). Not a
    validation problem — retrying with the same prompt won't help, so
    `call_structured` never retries this."""


class ClaudeTruncatedToolCallError(ClaudeAPIError):
    """The tool call's JSON input may have been cut off
    (`stop_reason == "max_tokens"`). Not retried: a truncated payload needs
    a larger `max_tokens`, not feedback — retrying with the same limit
    would just truncate again."""


class StructuredOutputError(ClaudeAPIError):
    """`call_structured` validated the tool output twice (initial call plus
    one retry) and both attempts failed. Carries the failure detail so the
    caller can record what was dropped (CLAUDE.md §6 rule 4: "record the
    drop," never a silent discard).
    """

    def __init__(self, message: str, *, last_error: Exception, raw_input: dict[str, Any]) -> None:
        super().__init__(message)
        self.last_error = last_error
        self.raw_input = raw_input


class UsageLike(Protocol):
    """Structural type for anything with the four token-count fields
    `compute_cost` needs — matches `anthropic.types.Usage`'s shape without
    importing it, so tests can pass a plain `SimpleNamespace` instead of
    constructing a real SDK response object.
    """

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


def compute_cost(usage: UsageLike, pricing: ModelPricing) -> Decimal:
    """Compute the USD cost of one Claude call from its token usage.

    WHY this is a standalone pure function (CLAUDE.md C4 — numbers never
    come from an LLM, Python computes them): it can be unit-tested against
    hand-worked values without a network call or even a `ClaudeClient`.

    Known simplification: all cache-creation tokens are priced at the
    5-minute-TTL write rate. The Anthropic API can also report a 1-hour-TTL
    breakdown when a call explicitly requests it via `cache_control`, which
    would need `pricing.cache_write_1h_per_mtok` instead. Phase 0's
    hello-world call sets no `cache_control` breakpoint at all (its prompt
    is far under Haiku 4.5's ~4,096-token minimum cacheable prefix), so this
    never actually diverges yet.
    # TODO(phase2+): once an agent uses an explicit 1-hour cache breakpoint,
    # branch on the TTL actually used instead of assuming 5 minutes.
    """
    mtok = Decimal(1_000_000)
    return (
        usage.input_tokens * pricing.input_per_mtok
        + usage.output_tokens * pricing.output_per_mtok
        + usage.cache_creation_input_tokens * pricing.cache_write_5m_per_mtok
        + usage.cache_read_input_tokens * pricing.cache_read_per_mtok
    ) / mtok


def total_cost(results: Iterable[LLMCallResult]) -> Decimal:
    """Sum the cost of a set of calls — e.g. every call one research run
    made. Pure and trivially testable, same reasoning as `compute_cost`:
    CLAUDE.md §5 wants "what does one research run cost?" answered with a
    number Python computed, not a guess.
    """
    return sum((result.cost_usd for result in results), Decimal("0"))


class LLMCallResult(BaseModel):
    """Everything about one Claude call worth knowing after the fact."""

    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    latency_ms: float
    cost_usd: Decimal
    text: str


class ClaudeClient:
    """Thin wrapper around `anthropic.Anthropic`.

    WHY a class, not a bare function: it holds the SDK client (constructed
    once, not per call) and the `Settings` it was built from (default model,
    pricing table). A bare function would need both passed at every call
    site. The `client` constructor parameter lets tests inject a fake SDK
    client — `ClaudeClient(settings, client=<mock>)` — so no test in this
    project ever makes a real network call.

    `on_result`, if given, is called with every `LLMCallResult` this client
    produces — once per underlying API call, so a `call_structured` retry
    reports two. WHY a callback rather than returning cost from
    `call_structured`: that method is deliberately generic (it returns the
    caller's own Pydantic model), and every agent would otherwise need its
    signature changed just to thread cost back out. The orchestration layer
    (Phase 5) gives each agent node its own client whose callback appends
    to a per-node list, so a run's cost is collected as data in the graph
    state — replacing the Phase 2-4 demos' trick of scraping cost back out
    of log records, which would mix two concurrent runs' costs together.
    Called from whatever thread made the call (agents run
    `call_structured` via `asyncio.to_thread`, ADR-0019), so it must be
    thread-safe; `list.append` is.
    """

    def __init__(
        self,
        settings: Settings,
        client: anthropic.Anthropic | None = None,
        *,
        on_result: Callable[[LLMCallResult], None] | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or anthropic.Anthropic(
            api_key=settings.anthropic_api_key.get_secret_value()
        )
        self._on_result = on_result

    def call(
        self,
        *,
        agent: str,
        messages: list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 512,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> LLMCallResult:
        """Make one Claude call and return its parsed, priced result.

        `agent` is required with no default: every call site — including
        this project's very first one, the hello-world script — names who
        it's calling for. That discipline is what makes the per-agent cost
        breakdown in later phases possible without retrofitting anything.

        `tools`/`tool_choice` are accepted for a plain-text call that
        happens to offer tools without forcing one. For forced tool use
        with Pydantic validation and a retry-on-failure loop (CLAUDE.md
        §16), use `call_structured` instead.
        """
        _, result = self._call_raw(
            agent=agent,
            messages=messages,
            system=system,
            model=model,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
        )
        return result

    def call_structured(
        self,
        *,
        agent: str,
        model_cls: type[ModelT],
        messages: list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 1024,
        tool_name: str,
        tool_description: str,
        extra_validation: Callable[[ModelT], None] | None = None,
    ) -> ModelT:
        """Force a tool call, validate its input as `model_cls`, and retry
        once with the validation error fed back if it fails — the exact
        pattern CLAUDE.md §16 requires: "use native structured outputs or
        forced tool use, then Pydantic validation, then a single retry that
        feeds the validation error back. Never rely on 'reply only in
        JSON' prompting."

        `extra_validation`, if given, runs after Pydantic validation
        succeeds and should raise `ValueError` (or a subclass) to signal a
        failure that should trigger the same retry-with-feedback flow —
        e.g. an agent closing over `EvidenceStore.resolve` to reject a
        well-formed `Claim` that cites an evidence_id outside the set it
        was shown. Anything else `extra_validation` raises propagates
        uncaught, same as any other unexpected exception.

        Raises `StructuredOutputError` (carrying `.last_error`/`.raw_input`
        for the caller to record a drop) if both the initial call and the
        one retry fail. Raises `ClaudeRefusalError`/
        `ClaudeTruncatedToolCallError` immediately, with no retry, if
        Claude refused or its tool call may have been truncated — neither
        is a validation problem feedback can fix.
        """
        tool: dict[str, Any] = {
            "name": tool_name,
            "description": tool_description,
            "input_schema": model_cls.model_json_schema(),
        }
        tool_choice: dict[str, Any] = {"type": "tool", "name": tool_name}

        response, _ = self._call_raw(
            agent=agent,
            messages=messages,
            system=system,
            model=model,
            max_tokens=max_tokens,
            tools=[tool],
            tool_choice=tool_choice,
        )
        parsed, error, raw_input, tool_use = self._parse_tool_output(
            response, model_cls, extra_validation
        )
        if parsed is not None:
            return parsed

        # WHY replaying `response.content` unmodified: verified against the
        # actually-installed SDK that its request-transform layer
        # (anthropic/_utils/_transform.py::_transform_recursive) detects any
        # nested pydantic.BaseModel and calls model_dump(mode="json", ...)
        # on it automatically — no manual serialization needed here.
        retry_messages: list[dict[str, Any]] = [
            *messages,
            {"role": "assistant", "content": response.content},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "is_error": True,
                        "content": str(error),
                    }
                ],
            },
        ]
        response2, _ = self._call_raw(
            agent=agent,
            messages=retry_messages,
            system=system,
            model=model,
            max_tokens=max_tokens,
            tools=[tool],
            tool_choice=tool_choice,
        )
        parsed2, error2, raw_input2, _ = self._parse_tool_output(
            response2, model_cls, extra_validation
        )
        if parsed2 is not None:
            return parsed2

        assert error2 is not None  # guaranteed: _parse_tool_output only returns
        # (None, ...) via its except clause, which always sets a real error.
        raise StructuredOutputError(
            f"{tool_name!r} failed validation twice for agent={agent!r}: {error2}",
            last_error=error2,
            raw_input=raw_input2,
        )

    def _parse_tool_output(
        self,
        response: Any,
        model_cls: type[ModelT],
        extra_validation: Callable[[ModelT], None] | None,
    ) -> tuple[ModelT | None, Exception | None, dict[str, Any], Any]:
        """Returns `(parsed, None, raw_input, tool_use)` on success, or
        `(None, error, raw_input, tool_use)` on a validation failure the
        caller should retry. Raises directly (no retry) for a refusal or a
        possibly-truncated tool call — see call_structured's docstring."""
        if response.stop_reason == "refusal":
            raise ClaudeRefusalError(f"Claude refused to produce structured output for {model_cls}")
        if response.stop_reason == "max_tokens":
            raise ClaudeTruncatedToolCallError(
                f"Tool call input for {model_cls} may have been truncated "
                "(stop_reason=max_tokens) — increase max_tokens"
            )
        tool_use = next(block for block in response.content if block.type == "tool_use")
        try:
            parsed = model_cls.model_validate(tool_use.input)
            if extra_validation is not None:
                extra_validation(parsed)
        except (ValidationError, ValueError) as exc:
            return None, exc, tool_use.input, tool_use
        return parsed, None, tool_use.input, tool_use

    def _call_raw(
        self,
        *,
        agent: str,
        messages: list[dict[str, Any]],
        system: str | None,
        model: str | None,
        max_tokens: int,
        tools: list[dict[str, Any]] | None,
        tool_choice: dict[str, Any] | None,
    ) -> tuple[Any, LLMCallResult]:
        """The actual SDK call: exception mapping, cost computation, and
        structured logging — shared by `call()` and `call_structured()` so
        neither reimplements cost tracking. Returns both the raw SDK
        response (needed by `call_structured` to extract a tool_use block
        and to replay `response.content` in a retry) and the existing,
        unchanged `LLMCallResult`.

        Retries are deliberately not hand-rolled here: the Anthropic SDK
        already retries connection errors, 408/409/429, and 5xx with
        exponential backoff by default (`max_retries=2`). CLAUDE.md §16's
        transient/permanent distinction at the application level is a
        judgment call (skip a claim vs. retry a whole research task) that
        belongs with whichever agent loop needs to make it.
        """
        resolved_model = model or self._settings.default_model
        pricing = self._settings.model_pricing.get(resolved_model)
        if pricing is None:
            raise ClaudeInvalidRequestError(
                f"No pricing configured for model {resolved_model!r} — "
                "add it to Settings.model_pricing before calling it."
            )

        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system is not None:
            kwargs["system"] = system
        if tools is not None:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice

        start = time.monotonic()
        try:
            response = self._client.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise ClaudeAuthenticationError(str(exc)) from exc
        except anthropic.RateLimitError as exc:
            raise ClaudeRateLimitError(str(exc)) from exc
        except anthropic.BadRequestError as exc:
            raise ClaudeInvalidRequestError(str(exc)) from exc
        except anthropic.APIConnectionError as exc:
            raise ClaudeConnectionError(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            # Catch-all for any other status Anthropic can return (403,
            # 404, 409, 422, 5xx) that this project doesn't need to
            # distinguish further yet.
            raise ClaudeServerError(str(exc)) from exc
        latency_ms = (time.monotonic() - start) * 1000

        usage = response.usage
        cost = compute_cost(usage, pricing)
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )

        result = LLMCallResult(
            agent=agent,
            model=resolved_model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            latency_ms=latency_ms,
            cost_usd=cost,
            text=text,
        )

        logger.info(
            "llm_call",
            extra={
                "agent": result.agent,
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cache_creation_input_tokens": result.cache_creation_input_tokens,
                "cache_read_input_tokens": result.cache_read_input_tokens,
                "latency_ms": round(result.latency_ms, 1),
                "cost_usd": str(result.cost_usd),
            },
        )
        if self._on_result is not None:
            self._on_result(result)
        return response, result
