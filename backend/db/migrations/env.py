"""Alembic environment, wired to run migrations asynchronously via asyncpg.

WHY the async pattern instead of adding a sync driver (psycopg2/psycopg)
just for Alembic: this project's only Postgres driver anywhere is asyncpg
(see backend/db/session.py) — adding a second, sync-only driver purely so
Alembic's default engine_from_config() works would be an extra dependency
for no benefit. Alembic's documented async cookbook pattern (run
migrations inside `connection.run_sync(...)`) lets the same asyncpg-based
engine serve both the app and its migrations.

WHY Settings, not alembic.ini's static `sqlalchemy.url`: Settings is
already the single source of truth for Postgres connection details (env
vars / .env) everywhere else in this codebase (backend/db/session.py,
docker-compose.yml). Duplicating them into alembic.ini would create a
second place they could drift out of sync.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from backend.core.config import Settings
from backend.db.models import Base
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

config.set_main_option(
    "sqlalchemy.url",
    Settings().postgres_async_dsn,  # type: ignore[call-arg]
)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (`alembic upgrade
    head --sql`) — unused in this project's normal flow but kept as
    Alembic's standard entry point."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
