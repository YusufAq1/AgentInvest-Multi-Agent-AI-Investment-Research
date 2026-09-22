"""Fixtures for tests needing a real Postgres+pgvector database.

WHY this lives here, not in the shared tests/conftest.py: `tests/rag/` is
the first (and, as of Phase 3, only) test suite that needs a real
database. Every other suite (tests/data/*, tests/evidence/*, tests/calc/*,
tests/agents/*) mocks all external I/O and stays fully DB-free — scoping
this fixture here keeps that property visible rather than letting an
unrelated test accidentally depend on Postgres by importing a shared
fixture.

WHY `alembic upgrade head` runs once per session but the `AsyncEngine`
itself is created fresh per test: `pytest-asyncio` gives each async test
function its own event loop by default. A session-scoped `AsyncEngine`
(the first version of this fixture) survives past the event loop its
first connection was opened on — the second test to touch it fails with
`RuntimeError: Event loop is closed` deep inside asyncpg, confirmed
directly by running the full suite (one test passed in isolation, then
failed only when run after another test). Migrations are a genuinely
session-scoped, loop-independent side effect (`alembic.command.upgrade`
runs its own sync-wrapped connection internally); the engine that serves
actual test queries is not.

Per-test transaction rollback: one connection-level transaction per test,
joined via `join_transaction_mode="create_savepoint"` so that code under
test calling `session.begin()` (backend/db/session.py's `session_scope`)
transparently gets a SAVEPOINT instead of a real transaction — means every
test starts from a clean slate regardless of what a previous test wrote,
without truncating tables between tests. See SQLAlchemy's own docs on
"joining a session into an external transaction, such as for test suites."

These fixtures require a real Postgres+pgvector reachable via the
POSTGRES_* env vars Settings reads (CI provides its own ephemeral
container — see .github/workflows/ci.yml; locally, point them at your own
Supabase project or a local docker-compose Postgres). Tests that use them
will fail with a connection error if no such database is reachable — that
failure is expected and informative outside CI, not a bug to silence.
"""

from collections.abc import AsyncIterator
from typing import cast

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from backend.core.config import Settings
from backend.db.session import make_engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

_REPO_ROOT_ALEMBIC_INI = "alembic.ini"


@pytest.fixture(scope="session")
def _migrated_database() -> None:
    """Runs `alembic upgrade head` once per test session — a loop
    -independent side effect, safe to share across every test's own
    event loop (unlike an `AsyncEngine`, see module docstring)."""
    alembic_cfg = Config(_REPO_ROOT_ALEMBIC_INI)
    command.upgrade(alembic_cfg, "head")


@pytest_asyncio.fixture
async def db_engine(_migrated_database: None) -> AsyncIterator[AsyncEngine]:
    """A fresh engine per test, tied to that test's own event loop.

    WHY real `Settings()` (reads `.env`/environment), not the shared
    `tests/conftest.py::make_settings()` test-isolation helper: that
    helper deliberately hardcodes fake, unreachable Postgres credentials
    so ordinary mocked tests never depend on a developer's real
    environment. These fixtures are the opposite case — they explicitly
    need whatever real, reachable Postgres is configured (CI's service
    container via real env vars, or a developer's own Supabase/
    docker-compose instance via `.env`), matching what
    `backend/db/migrations/env.py` already does for Alembic.
    """
    engine = make_engine(Settings())  # type: ignore[call-arg]
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session joined to its own connection-level transaction, rolled
    back at teardown regardless of what code under test committed."""
    async with db_engine.connect() as connection:
        await connection.begin()
        session = AsyncSession(
            bind=connection,
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,
        )
        try:
            yield session
        finally:
            await session.close()
            await connection.rollback()


class _PerCallSessionContext:
    """Wraps the shared per-test `db_session` so each `session_scope()`
    call gets a fresh transaction on the SAME session, closing it on exit.

    WHY close(), not a no-op: `session_scope()` calls `session.begin()`
    explicitly (backend/db/session.py). A plain `AsyncSession.begin()`
    raises `InvalidRequestError: A transaction is already begun on this
    Session` if called again before the previous transaction ended —
    confirmed directly against a real Supabase database the first time
    `backend/rag/indexing.py` and `backend/rag/retrieval.py` were both
    exercised against the same shared test session in one test.
    `session.close()` ends the Session's OWN transactional state (so the
    next `session_scope()` call can `begin()` again) without closing the
    externally-supplied `connection` the `db_session` fixture owns — an
    externally-bound Session never closes a connection it didn't create.
    `join_transaction_mode="create_savepoint"` (set on `db_session`)
    re-joins a fresh SAVEPOINT against that same outer transaction each
    time, so the whole test still rolls back atomically at teardown
    regardless of how many `session_scope()` calls happened inside it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> None:
        await self._session.close()


class _ReusableSessionFactory:
    """Adapts one fixed `AsyncSession` into the callable shape
    `async_sessionmaker[AsyncSession]` has, for code under test that
    expects to call `session_factory()` itself."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __call__(self) -> _PerCallSessionContext:
        return _PerCallSessionContext(self._session)


@pytest.fixture
def session_factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    # WHY the cast: _ReusableSessionFactory is a deliberate test-only stand
    # -in, structurally compatible with how session_scope() calls
    # session_factory() but not a real async_sessionmaker instance.
    return cast("async_sessionmaker[AsyncSession]", _ReusableSessionFactory(db_session))
