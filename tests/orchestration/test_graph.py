"""Tests for the Phase 5 research graph (backend/orchestration/).

DB-free and network-free, like every other agent test: agents are small
fakes injected through `RunDependencies.agent_factories` (the same seam the
real agents are built through), Claude is a mocked SDK client wrapped in a
real `ClaudeClient` (so cost tracking runs for real), and the checkpointer
is LangGraph's `InMemorySaver`. It's given our production serializer, so the
checkpoint type allowlist is exercised on every test.

What the real agents do internally is already covered by tests/agents/*.
These tests cover what the ORCHESTRATION adds: routing, parallel fan-out,
the failure bulkhead, cost collection, fan-in re-validation, streaming
progress, and resume.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from backend.core.llm import ClaudeClient, LLMCallResult, total_cost
from backend.evidence.errors import EvidenceNotFoundError, LookAheadEvidenceError
from backend.evidence.models import Claim, Evidence
from backend.orchestration.graph import (
    AgentContext,
    AgentFactory,
    RunDependencies,
    _collect_node,
    build_research_graph,
)
from backend.orchestration.runner import (
    ResearchGraph,
    RunNotFoundError,
    make_serializer,
    resume_run,
    start_run,
)
from backend.orchestration.state import (
    ALL_AGENTS,
    CHECKPOINT_TYPES,
    AgentName,
    AgentOutcome,
    ResearchState,
)
from langgraph.checkpoint.memory import InMemorySaver

from tests.conftest import make_settings

AS_OF = date(2024, 6, 30)
TICKER = "TICK"
PEER_MAP: dict[str, tuple[str, ...]] = {TICKER: ("PEER1",)}


class _Interrupted(BaseException):
    """Stands in for Ctrl-C: a BaseException, so it passes straight through
    the agent bulkhead (which only catches Exception) exactly like
    KeyboardInterrupt would."""


def _mock_sdk(input_tokens: int = 1000) -> MagicMock:
    """An Anthropic SDK stand-in: every call bills `input_tokens` Haiku
    input tokens ($1/Mtok → 0.001 USD per call at the default 1000)."""
    sdk = MagicMock()
    sdk.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        stop_reason="end_turn",
    )
    return sdk


class _FakeAgent:
    """Adds one evidence row and one claim citing it, after one (mocked)
    Claude call, the minimum shape of a real agent's run."""

    def __init__(self, name: AgentName, ctx: AgentContext, *, fail_with: BaseException | None):
        self._name = name
        self._ctx = ctx
        self._fail_with = fail_with

    @property
    def agent_name(self) -> str:
        return self._name

    async def run(self, ticker: str, as_of: date) -> list[Claim]:
        # Off the event loop, exactly like the real agents (ADR-0019).
        await asyncio.to_thread(
            self._ctx.claude.call, agent=self._name, messages=[{"role": "user", "content": "x"}]
        )
        if self._fail_with is not None:
            # Yield first, so sibling agents in the same superstep finish
            # before this one fails (makes the resume test deterministic).
            await asyncio.sleep(0.05)
            raise self._fail_with
        evidence = Evidence(
            id=uuid4(),
            run_id=self._ctx.store.run_id,
            source_type="computed",
            source_ref=f"src-{self._name}",
            published_at=as_of,
            retrieved_at=datetime.now(UTC),
            quote=f"quote from {self._name}",
            location={},
        )
        self._ctx.store.add_evidence(evidence)
        claim = Claim(
            id=uuid4(),
            agent=self._name,
            statement=f"{self._name} says something",
            evidence_ids=[evidence.id],
            claim_type="evidence",
            materiality="medium",
        )
        self._ctx.store.add_claim(claim)
        return [claim]


def _deps(
    *,
    failures: dict[AgentName, BaseException | Callable[[], BaseException | None]] | None = None,
    peer_map: dict[str, tuple[str, ...]] = PEER_MAP,
    built: Counter[str] | None = None,
) -> RunDependencies:
    """Fake dependencies. `failures[name]` is either an exception to raise,
    or a zero-arg callable returning one (or None) per construction, so
    the resume test can fail an agent the first time only. `built` counts
    how many times each agent was constructed."""
    failures = failures or {}
    settings = make_settings()

    def factory_for(name: AgentName) -> AgentFactory:
        def build(ctx: AgentContext) -> _FakeAgent:
            if built is not None:
                built[name] += 1
            failure = failures.get(name)
            if callable(failure) and not isinstance(failure, BaseException):
                failure = failure()
            return _FakeAgent(name, ctx, fail_with=failure)

        return build

    def claude_factory(on_result: Callable[[LLMCallResult], None]) -> ClaudeClient:
        return ClaudeClient(settings, client=_mock_sdk(), on_result=on_result)

    return RunDependencies(
        settings=settings,
        agent_factories={name: factory_for(name) for name in ALL_AGENTS},
        claude_factory=claude_factory,
        peer_map=peer_map,
    )


def _graph(deps: RunDependencies) -> ResearchGraph:
    return build_research_graph(deps, InMemorySaver(serde=make_serializer()))


async def test_all_agents_run_and_every_claim_is_attributed_correctly() -> None:
    result = await start_run(_graph(_deps()), run_id=uuid4(), ticker=TICKER, as_of=AS_OF)

    assert result.completed
    assert {o.agent: o.status for o in result.outcomes} == dict.fromkeys(ALL_AGENTS, "succeeded")
    claims = result.state["claims"]
    assert sorted(c.agent for c in claims) == sorted(ALL_AGENTS)
    evidence_by_id = {e.id: e for e in result.state["evidence"]}
    for claim in claims:
        # Nothing cross-attributed: each claim cites its own agent's evidence.
        assert all(
            evidence_by_id[eid].source_ref == f"src-{claim.agent}" for eid in claim.evidence_ids
        )


async def test_run_cost_is_collected_per_call_and_summed() -> None:
    result = await start_run(_graph(_deps()), run_id=uuid4(), ticker=TICKER, as_of=AS_OF)

    # One mocked call per agent, 1000 Haiku input tokens each = $0.001.
    assert Counter(c.agent for c in result.state["llm_calls"]) == dict.fromkeys(ALL_AGENTS, 1)
    assert result.total_cost_usd == Decimal("0.004")
    assert result.total_cost_usd == total_cost(result.state["llm_calls"])


async def test_one_failing_agent_is_recorded_and_the_rest_still_finish() -> None:
    deps = _deps(failures={"news": RuntimeError("SEC is down")})
    result = await start_run(_graph(deps), run_id=uuid4(), ticker=TICKER, as_of=AS_OF)

    assert result.completed
    outcomes = {o.agent: o for o in result.outcomes}
    assert outcomes["news"].status == "failed"
    assert outcomes["news"].detail == "RuntimeError: SEC is down"
    assert all(outcomes[a].status == "succeeded" for a in ("financial", "filings", "competitive"))
    # A failed agent contributes no claims or evidence...
    assert "news" not in {c.agent for c in result.state["claims"]}
    # ...but the money it spent before failing is still counted.
    assert "news" in {c.agent for c in result.state["llm_calls"]}


async def test_ticker_without_peers_skips_competitive_without_building_it() -> None:
    built: Counter[str] = Counter()
    deps = _deps(peer_map={}, built=built)
    result = await start_run(_graph(deps), run_id=uuid4(), ticker=TICKER, as_of=AS_OF)

    competitive = next(o for o in result.outcomes if o.agent == "competitive")
    assert competitive.status == "skipped"
    assert competitive.detail is not None and "peer map" in competitive.detail
    assert built["competitive"] == 0
    assert result.plan is not None and "competitive" not in result.plan.routed_agents()


async def test_progress_events_are_streamed_in_order() -> None:
    events: list[dict[str, Any]] = []
    await start_run(
        _graph(_deps()), run_id=uuid4(), ticker=TICKER, as_of=AS_OF, on_event=events.append
    )

    kinds = [e["event"] for e in events]
    assert kinds[0] == "plan_ready"
    assert kinds[-1] == "run_collected"
    assert Counter(kinds) == {
        "plan_ready": 1,
        "agent_started": 4,
        "agent_finished": 4,
        "run_collected": 1,
    }
    # Every agent starts before any finishes: the fan-out is one superstep.
    assert max(i for i, k in enumerate(kinds) if k == "agent_started") < min(
        i for i, k in enumerate(kinds) if k == "agent_finished"
    )


async def test_no_evidence_in_the_final_state_postdates_as_of() -> None:
    """CLAUDE.md §12's leakage test, at the orchestration level."""
    result = await start_run(_graph(_deps()), run_id=uuid4(), ticker=TICKER, as_of=AS_OF)
    assert result.state["evidence"]
    assert all(e.published_at <= AS_OF for e in result.state["evidence"])


def _state_with(evidence: list[Evidence], claims: list[Claim]) -> ResearchState:
    run_id = evidence[0].run_id if evidence else uuid4()
    return {
        "run_id": run_id,
        "ticker": TICKER,
        "as_of": AS_OF,
        "evidence": evidence,
        "claims": claims,
        "dropped_claims": [],
        "agent_outcomes": [],
        "llm_calls": [],
    }


def _evidence(run_id: Any, published_at: date) -> Evidence:
    return Evidence(
        id=uuid4(),
        run_id=run_id,
        source_type="computed",
        source_ref="x",
        published_at=published_at,
        retrieved_at=datetime.now(UTC),
        quote="q",
        location={},
    )


def test_collect_rejects_a_claim_citing_evidence_no_agent_produced() -> None:
    """Deliberately injected fabricated citation: the fan-in re-validation
    must catch it even though no agent node would ever let one through."""
    run_id = uuid4()
    real = _evidence(run_id, AS_OF)
    fabricated = Claim(
        id=uuid4(),
        agent="financial",
        statement="cites nothing real",
        evidence_ids=[uuid4()],
        claim_type="evidence",
        materiality="high",
    )
    with pytest.raises(EvidenceNotFoundError):
        _collect_node(_state_with([real], [fabricated]))


def test_collect_rejects_evidence_published_after_as_of() -> None:
    run_id = uuid4()
    leaked = _evidence(run_id, date(2024, 7, 1))
    with pytest.raises(LookAheadEvidenceError):
        _collect_node(_state_with([leaked], []))


async def test_interrupted_run_resumes_without_rerunning_finished_agents() -> None:
    """Simulates Ctrl-C during the agent fan-out, then resumes the run."""
    interrupted_once: list[bool] = []

    def news_fails_first_time() -> BaseException | None:
        if interrupted_once:
            return None
        interrupted_once.append(True)
        return _Interrupted()

    built: Counter[str] = Counter()
    deps = _deps(failures={"news": news_fails_first_time}, built=built)
    graph = _graph(deps)  # one checkpointer shared by both attempts
    run_id = uuid4()

    with pytest.raises(_Interrupted):
        await start_run(graph, run_id=run_id, ticker=TICKER, as_of=AS_OF)

    result = await resume_run(graph, run_id=run_id)

    assert result.completed
    assert {o.agent: o.status for o in result.outcomes} == dict.fromkeys(ALL_AGENTS, "succeeded")
    # The three agents that finished before the interruption were not
    # rebuilt or rerun: their state updates came from the checkpoint.
    assert built == {"financial": 1, "filings": 1, "competitive": 1, "news": 2}
    # Each finished agent's cost is counted exactly once. The interrupted
    # attempt's news call was never checkpointed (the node didn't return),
    # so it's missing here. See ADR-0001 on this known under-count.
    assert Counter(c.agent for c in result.state["llm_calls"]) == dict.fromkeys(ALL_AGENTS, 1)


async def test_resuming_an_unknown_run_raises() -> None:
    with pytest.raises(RunNotFoundError):
        await resume_run(_graph(_deps()), run_id=uuid4())


async def test_every_state_type_survives_a_checkpoint_round_trip() -> None:
    """Guards make_serializer()'s allowlist. A type missing from it comes
    back from a checkpoint as a plain dict (LangGraph only logs a warning),
    so this checks the real final state of a run, element by element."""
    serde = make_serializer()
    result = await start_run(_graph(_deps()), run_id=uuid4(), ticker=TICKER, as_of=AS_OF)
    samples: list[object] = [result.plan]
    for key in ("evidence", "claims", "agent_outcomes", "llm_calls"):
        samples.append(result.state[key][0])
    for sample in samples:
        restored = serde.loads_typed(serde.dumps_typed(sample))
        assert type(restored) is type(sample)
        assert restored == sample
    assert {type(s) for s in samples} <= set(CHECKPOINT_TYPES)


def test_serializer_does_not_rebuild_types_outside_the_allowlist() -> None:
    class NotAllowlisted(AgentOutcome):
        pass

    serde = make_serializer()
    restored = serde.loads_typed(serde.dumps_typed(NotAllowlisted(agent="news", status="failed")))
    assert not isinstance(restored, NotAllowlisted)
