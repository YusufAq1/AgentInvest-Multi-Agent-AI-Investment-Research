"""AgentInvest backend package.

Multi-agent equity research system: given a ticker and an as-of date, runs
specialist research agents, builds opposing bull/bear theses, subjects them
to adversarial critique, and produces an auditable, evidence-linked report.

This package is organized by responsibility, not by agent:
  - core/       shared infrastructure (config, LLM client, logging)
  - data/       all external I/O (Phase 1+)
  - calc/       pure, deterministic financial calculations (Phase 6+)
  - rag/        retrieval over filing text (Phase 3+)
  - evidence/   the citation-enforcement layer (Phase 2+)
  - agents/     specialist, debate, and judge agents (Phase 2+)
  - orchestration/  run state and the manager loop (Phase 5+)
  - db/         persistence models and migrations (Phase 1+)
  - api/        FastAPI routes (Phase 8+)

Phase 0 implements only `core/`.
"""
