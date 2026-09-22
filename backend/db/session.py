"""Async SQLAlchemy engine/session factory.

WHY a module-level-constructible-but-not-module-level-instantiated engine
(callers build one from Settings and hold onto it), not a fresh engine per
call: an asyncpg connection pool is expensive to create and meant to be
long-lived per process — the same "construct once from Settings" pattern
`ClaudeClient`/`EdgarClient` already use elsewhere in this codebase.

WHY `session_scope` is the one place transaction boundaries are decided:
callers (backend/rag/indexing.py, backend/rag/retrieval.py) never call
`commit()`/`rollback()` themselves — a single, reviewable place to reason
about "when does a write actually land" beats every caller managing its
own transaction lifecycle.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.core.config import Settings


def make_engine(settings: Settings) -> AsyncEngine:
    """One engine per process. `pool_pre_ping=True` matters concretely for
    a hosted Postgres provider like Supabase, which can silently drop idle
    connections — without this, a stale connection surfaces as an opaque
    asyncpg error instead of SQLAlchemy transparently reconnecting."""
    return create_async_engine(settings.postgres_async_dsn, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One transaction per call — commits on success, rolls back on any
    exception raised inside the `async with` block."""
    async with session_factory() as session, session.begin():
        yield session
