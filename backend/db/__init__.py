"""Postgres access layer: SQLAlchemy async engine/session, ORM models,
and Alembic migrations (backend/db/migrations/).

WHY this exists starting Phase 3, not earlier: Phases 0-2 deliberately used
SQLite (the data cache) and an in-memory Evidence Store (see ADR-0012) —
nothing needed cross-run Postgres durability yet. Phase 3's retrieval
infrastructure (document_chunks, pgvector HNSW, tsvector full-text) is the
first thing that genuinely needs a real database. See ADR-0013.
"""
