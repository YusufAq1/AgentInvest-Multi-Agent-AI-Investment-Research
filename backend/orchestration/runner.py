"""Running (and resuming) the research graph against real clients.

This module owns the live resources a run needs, meaning SEC HTTP, the
edgartools client, the DB engine and the checkpointer, and the two entry
points: `start_run` and `resume_run`. The graph itself lives in graph.py;
this is the part that touches the outside world.

New-concept note: a *checkpointer* saves the graph's state after every
superstep, keyed by a `thread_id`. We use the run_id as the thread_id, so
"resume run X" means "load thread X's last checkpoint and continue". Agents
that finished before an interruption are not re-run, because their state
updates were already saved.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

# WHY import from langchain_core (a transitive dependency, not one we
# declare): RunnableConfig is the config type in LangGraph's own public
# signatures. langgraph requires langchain-core and imports RunnableConfig
# from there itself, so this adds no package, just a name we need for mypy.
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import StreamMode
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.agents.competitive import CompetitiveAgent
from backend.agents.filings import FilingsAgent
from backend.agents.financial import FinancialAgent
from backend.agents.manager import ResearchManager
from backend.agents.news import NewsAgent
from backend.agents.peer_map import PEER_MAP
from backend.agents.valuation import ValuationAgent
from backend.agents.valuation_inputs import ValuationInputAssembler
from backend.core.config import Settings
from backend.core.llm import AgentInvestError, ClaudeClient, LLMCallResult, total_cost
from backend.data.cache import Cache
from backend.data.cik import CikResolver
from backend.data.company import CompanyClient
from backend.data.edgar import EdgarClient
from backend.data.http import SecHttpClient
from backend.data.macro import MacroClient
from backend.data.news import NewsClient
from backend.data.prices import PricesClient
from backend.data.xbrl import XBRLClient
from backend.db.session import make_engine, make_session_factory
from backend.orchestration.graph import AgentContext, AgentFactory, RunDependencies
from backend.orchestration.planning import Planner
from backend.orchestration.state import (
    CHECKPOINT_TYPES,
    AgentName,
    AgentOutcome,
    ResearchPlan,
    ResearchState,
)

logger = logging.getLogger("agentinvest.orchestration")

ResearchGraph = CompiledStateGraph[ResearchState, None, ResearchState, ResearchState]
EventHandler = Callable[[dict[str, Any]], None]

# WHY both modes: "custom" carries the agent_started / plan_ready /
# agent_finished events nodes emit through LangGraph's stream writer (the
# progress log); "updates" carries each node's raw state delta, which we
# only use to notice that `collect` finished. Phase 8's SSE endpoint can
# forward this same stream to a browser.
_STREAM_MODES: list[StreamMode] = ["custom", "updates"]


def make_serializer() -> JsonPlusSerializer:
    """The checkpoint serializer, restricted to this project's own types.

    WHY an explicit allowlist: LangGraph's default serializer will rebuild
    ANY Python class named in a checkpoint (it only logs a warning). Its
    own docs say that means anyone who can write to the checkpoint table
    can trigger code execution on load. Listing exactly the models our
    state holds shuts that off.

    Caveat, verified against the installed version: a type missing from
    the list is NOT rejected with an error. LangGraph logs a warning and
    hands back a plain dict, so a forgotten type would surface later as an
    AttributeError far from its cause. tests/orchestration/test_graph.py
    round-trips every CHECKPOINT_TYPES entry to catch that at test time.
    """
    return JsonPlusSerializer(allowed_msgpack_modules=list(CHECKPOINT_TYPES))


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[AsyncPostgresSaver]:
    """A Postgres checkpointer, with LangGraph's own tables created.

    WHY `setup()` on every open: it's idempotent (LangGraph tracks its own
    schema version in a `checkpoint_migrations` table), and running it here
    means a fresh database never needs a separate manual step. These tables
    are LangGraph's, not ours, so Alembic is told to ignore them
    (backend/db/migrations/env.py).

    WHY `postgres_dsn` (plain), not `postgres_async_dsn`: the checkpointer
    uses psycopg 3, not asyncpg, and psycopg wants a libpq-style URL with no
    `+asyncpg` driver suffix. See ADR-0001 for the cost of the second driver.
    """
    async with AsyncPostgresSaver.from_conn_string(
        settings.postgres_dsn, serde=make_serializer()
    ) as saver:
        await saver.setup()
        yield saver


def _default_agent_factories(
    *,
    xbrl: XBRLClient,
    news: NewsClient,
    edgar: EdgarClient,
    session_factory: async_sessionmaker[AsyncSession],
    peer_map: dict[str, tuple[str, ...]],
    valuation_assembler: ValuationInputAssembler,
) -> dict[AgentName, AgentFactory]:
    """How each real agent is built for one run.

    The plan's per-run choices go in here; today that's only the Filings
    questions."""

    def financial(ctx: AgentContext) -> FinancialAgent:
        return FinancialAgent(xbrl, ctx.claude, ctx.store, ctx.settings)

    def filings(ctx: AgentContext) -> FilingsAgent:
        return FilingsAgent(
            edgar,
            session_factory,
            ctx.claude,
            ctx.store,
            ctx.settings,
            research_questions=ctx.plan.filings_questions or None,
        )

    def news_agent(ctx: AgentContext) -> NewsAgent:
        return NewsAgent(news, ctx.claude, ctx.store, ctx.settings)

    def competitive(ctx: AgentContext) -> CompetitiveAgent:
        return CompetitiveAgent(xbrl, ctx.claude, ctx.store, ctx.settings, peer_map=peer_map)

    def valuation(ctx: AgentContext) -> ValuationAgent:
        return ValuationAgent(valuation_assembler, ctx.claude, ctx.store, ctx.settings)

    return {
        "financial": financial,
        "filings": filings,
        "news": news_agent,
        "competitive": competitive,
        "valuation": valuation,
    }


@asynccontextmanager
async def open_run_dependencies(settings: Settings) -> AsyncIterator[RunDependencies]:
    """Build every live client a run needs, and close them afterwards.

    WHY exactly ONE SecHttpClient shared by every agent: its rate limiter is
    per instance. The Phase 4 demo already shared one, but now that agents
    genuinely run concurrently, two instances would each allow SEC's full
    10 req/s. That would double our real rate.

    KNOWN GAP, NOW LIVE UNDER CONCURRENCY: edgartools (used by the Filings
    Agent through EdgarClient) runs its own limiter at ~9 req/s, which we
    can't reconfigure at runtime. It only reads the EDGAR_RATE_LIMIT_PER_SEC
    environment variable once, when `edgar` is first imported. So during the
    moments Filings and another SEC-bound agent fetch at the same time, our
    combined rate can briefly exceed 10 req/s. See backend/data/http.py and
    ADR-0001.
    """
    cache = Cache(settings.data_cache_path)
    http = SecHttpClient(settings)
    cik = CikResolver(http, cache)
    xbrl = XBRLClient(http, cik, cache)
    news = NewsClient(http, cik, cache)
    company = CompanyClient(http, cik, cache)
    macro = MacroClient(settings, cache)
    valuation_assembler = ValuationInputAssembler(xbrl, PricesClient(settings), macro, settings)
    edgar = EdgarClient(settings)
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    def claude_factory(on_result: Callable[[LLMCallResult], None]) -> ClaudeClient:
        return ClaudeClient(settings, on_result=on_result)

    def planner_factory(claude: ClaudeClient) -> Planner:
        return ResearchManager(company, claude, settings, PEER_MAP)

    try:
        yield RunDependencies(
            settings=settings,
            agent_factories=_default_agent_factories(
                xbrl=xbrl,
                news=news,
                edgar=edgar,
                session_factory=session_factory,
                peer_map=PEER_MAP,
                valuation_assembler=valuation_assembler,
            ),
            claude_factory=claude_factory,
            planner_factory=planner_factory,
        )
    finally:
        await http.aclose()
        await macro.aclose()
        await engine.dispose()


@dataclass(frozen=True)
class RunResult:
    """The final state of a run, plus the numbers every run prints."""

    state: ResearchState

    @property
    def plan(self) -> ResearchPlan | None:
        return self.state.get("plan")

    @property
    def outcomes(self) -> list[AgentOutcome]:
        return self.state["agent_outcomes"]

    @property
    def completed(self) -> bool:
        return self.state.get("completed", False)

    @property
    def total_cost_usd(self) -> Decimal:
        return total_cost(self.state["llm_calls"])


def _config(run_id: UUID) -> RunnableConfig:
    # WHY thread_id = run_id: one research run is one LangGraph thread, so
    # the run_id a user sees is exactly the handle they resume with.
    return RunnableConfig(configurable={"thread_id": str(run_id)})


async def _drive(
    graph: ResearchGraph,
    graph_input: ResearchState | None,
    run_id: UUID,
    on_event: EventHandler | None,
) -> RunResult:
    async for mode, chunk in graph.astream(graph_input, _config(run_id), stream_mode=_STREAM_MODES):
        if mode == "custom":
            event = {"run_id": str(run_id), **cast(dict[str, Any], chunk)}
        elif mode == "updates" and "collect" in chunk:
            event = {"run_id": str(run_id), "event": "run_collected"}
        else:
            continue
        logger.info("run_progress", extra=event)
        if on_event is not None:
            on_event(event)

    snapshot = await graph.aget_state(_config(run_id))
    return RunResult(state=cast(ResearchState, snapshot.values))


async def start_run(
    graph: ResearchGraph,
    *,
    run_id: UUID,
    ticker: str,
    as_of: date,
    on_event: EventHandler | None = None,
) -> RunResult:
    """Run the graph from the start for (ticker, as_of) under `run_id`."""
    initial: ResearchState = {
        "run_id": run_id,
        "ticker": ticker,
        "as_of": as_of,
        "evidence": [],
        "claims": [],
        "dropped_claims": [],
        "agent_outcomes": [],
        "llm_calls": [],
    }
    return await _drive(graph, initial, run_id, on_event)


async def resume_run(
    graph: ResearchGraph, *, run_id: UUID, on_event: EventHandler | None = None
) -> RunResult:
    """Continue an interrupted run from its last checkpoint.

    Passing `None` as the input is LangGraph's convention for "don't start
    over, continue this thread". Raises `RunNotFoundError` if there's no
    checkpoint for `run_id`. Without that check, LangGraph would treat the
    None input as an empty run and quietly do nothing.
    """
    snapshot = await graph.aget_state(_config(run_id))
    if not snapshot.values:
        raise RunNotFoundError(f"No checkpoint found for run_id={run_id}")
    return await _drive(graph, None, run_id, on_event)


class RunNotFoundError(AgentInvestError):
    """`resume_run` was asked to resume a run with no saved checkpoint."""
