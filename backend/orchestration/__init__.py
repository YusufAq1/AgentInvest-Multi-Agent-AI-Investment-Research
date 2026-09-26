"""Orchestration: the research run's state, graph, and runner (Phase 5).

- state.py    the typed graph state, plan, and per-agent outcome models
- planning.py the deterministic plan (5a's plan; 5b's recorded fallback)
- graph.py    plan -> parallel agents -> collect, as a LangGraph graph
- runner.py   live clients, the Postgres checkpointer, start/resume

See ADR-0001 for why this is LangGraph rather than hand-rolled asyncio.
"""
