"""Phase 5's "one command": run the full research stage for a ticker.

Run with:
    uv run alembic upgrade head   # once, before the first run
    uv run python scripts/run_research.py --ticker AAPL --as-of 2024-06-30
    uv run python scripts/run_research.py --resume <run_id>

What it does: plan -> Financial/Filings/News/Competitive agents in parallel
-> collect (re-validates every citation and the as_of cutoff), all as one
LangGraph graph checkpointed to Postgres after every step. It prints
progress as agents start and finish, then a per-agent outcome table and the
run's total Claude cost (CLAUDE.md §5).

If a run is interrupted (Ctrl-C, crash, laptop sleep), resume it with the
run_id printed at the start. Agents that already finished are not re-run
or re-paid.

Needs your own ANTHROPIC_API_KEY, EDGAR_IDENTITY, FRED_API_KEY and
POSTGRES_* values in .env, like every earlier demo script.
"""

import argparse
import asyncio
import sys
import time
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from backend.core.config import Settings
from backend.core.disclaimer import DISCLAIMER
from backend.core.logging import configure_logging
from backend.orchestration.graph import build_research_graph
from backend.orchestration.runner import (
    RunResult,
    open_checkpointer,
    open_run_dependencies,
    resume_run,
    start_run,
)


def _print_event(event: dict[str, Any], started: float) -> None:
    elapsed = f"[{time.monotonic() - started:6.1f}s]"
    kind = event["event"]
    if kind == "plan_ready":
        print(f"{elapsed} plan ({event['source']}): run {event['routed']}, skip {event['skipped']}")
        if event.get("fallback_reason"):
            print(f"{elapsed}   fallback because: {event['fallback_reason']}")
    elif kind == "agent_started":
        print(f"{elapsed} {event['agent']:<12} started")
    elif kind == "agent_finished":
        print(
            f"{elapsed} {event['agent']:<12} {event['status']}: "
            f"{event['claims_count']} claim(s), {event['dropped_count']} dropped"
            + (f" ({event['detail']})" if event.get("detail") else "")
        )
    elif kind == "run_collected":
        print(f"{elapsed} collect      all citations and the as_of cutoff re-verified")


def _print_summary(result: RunResult) -> None:
    if result.plan is not None:
        print(f"\n=== Plan ({result.plan.source}) ===")
        for route in result.plan.routes:
            print(f"  run  {route.agent:<12} {route.rationale}")
        for skip in result.plan.skipped:
            print(f"  skip {skip.agent:<12} {skip.reason}")
        for question in result.plan.filings_questions:
            print(f"  filings question: {question}")

    print("\n=== Outcomes ===")
    for outcome in sorted(result.outcomes, key=lambda o: o.agent):
        print(
            f"  {outcome.agent:<12} {outcome.status:<10} claims={outcome.claims_count:<3} "
            f"dropped={outcome.dropped_count:<3} {outcome.duration_ms / 1000:6.1f}s"
            + (f"  {outcome.detail}" if outcome.detail else "")
        )

    print("\n=== Claims ===")
    for claim in result.state["claims"]:
        print(f"  [{claim.agent}/{claim.claim_type}/{claim.materiality}] {claim.statement}")

    calls = result.state["llm_calls"]
    print("\n=== Run total ===")
    print(f"Completed: {result.completed}")
    print(f"Claude calls: {len(calls)}   Total Claude cost: ${result.total_cost_usd:.6f}")
    print(f"\n{DISCLAIMER}")


async def main(ticker: str | None, as_of: date | None, resume: UUID | None) -> None:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)
    started = time.monotonic()

    async with open_run_dependencies(settings) as deps, open_checkpointer(settings) as saver:
        graph = build_research_graph(deps, saver)

        def on_event(event: dict[str, Any]) -> None:
            _print_event(event, started)

        if resume is not None:
            print(f"=== Resuming run {resume} ===")
            result = await resume_run(graph, run_id=resume, on_event=on_event)
        else:
            assert ticker is not None and as_of is not None
            run_id = uuid4()
            print(f"=== Research run {run_id}: {ticker} as of {as_of.isoformat()} ===")
            print(f"(resume with: --resume {run_id})\n")
            result = await start_run(
                graph, run_id=run_id, ticker=ticker, as_of=as_of, on_event=on_event
            )

    _print_summary(result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--as-of", default="2024-06-30", help="YYYY-MM-DD")
    parser.add_argument("--resume", type=UUID, default=None, help="run_id of a run to continue")
    args = parser.parse_args()

    # WHY a SelectorEventLoop on Windows: the Postgres checkpointer uses
    # psycopg 3, whose async mode refuses to run on Windows' default
    # ProactorEventLoop. asyncpg, httpx and asyncio.to_thread all work on a
    # selector loop too, so switching costs nothing else.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(
        main(
            None if args.resume else args.ticker,
            None if args.resume else date.fromisoformat(args.as_of),
            args.resume,
        ),
        loop_factory=loop_factory,
    )
