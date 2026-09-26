"""The one orchestration test that needs a real Postgres.

Proves a run's state is actually persisted by AsyncPostgresSaver: it's
written through one connection and read back through a second, fresh one,
the same way `scripts/run_research.py --resume` reads a run started by an
earlier process. Every custom type must come back as itself. (The
serializer's allowlist silently turns an unlisted type into a dict; see
backend/orchestration/runner.py's make_serializer.)

Needs POSTGRES_* env vars pointing at a reachable database, like
tests/rag/. CI provides an ephemeral container; locally, a connection error
here means no database is reachable, which is expected and not a bug.
"""

from datetime import date
from uuid import uuid4

from backend.core.config import Settings
from backend.orchestration.graph import build_research_graph
from backend.orchestration.runner import open_checkpointer, start_run
from backend.orchestration.state import ALL_AGENTS, AgentOutcome, ResearchPlan

from tests.orchestration.test_graph import TICKER, _deps

AS_OF = date(2024, 6, 30)


async def test_run_state_round_trips_through_postgres() -> None:
    settings = Settings()  # type: ignore[call-arg]
    run_id = uuid4()
    deps = _deps()

    async with open_checkpointer(settings) as saver:
        result = await start_run(
            build_research_graph(deps, saver), run_id=run_id, ticker=TICKER, as_of=AS_OF
        )
    assert result.completed

    try:
        async with open_checkpointer(settings) as fresh_saver:
            graph = build_research_graph(deps, fresh_saver)
            snapshot = await graph.aget_state({"configurable": {"thread_id": str(run_id)}})
            values = snapshot.values

            assert values["completed"] is True
            assert isinstance(values["plan"], ResearchPlan)
            assert all(isinstance(o, AgentOutcome) for o in values["agent_outcomes"])
            assert {o.agent for o in values["agent_outcomes"]} == set(ALL_AGENTS)
            assert values["claims"] == result.state["claims"]
            assert values["evidence"] == result.state["evidence"]
            assert values["llm_calls"] == result.state["llm_calls"]
    finally:
        async with open_checkpointer(settings) as cleanup:
            await cleanup.adelete_thread(str(run_id))
