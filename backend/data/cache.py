"""Local SQLite cache, keyed (source, args_hash, as_of) per CLAUDE.md §14's
shape.

WHY SQLite instead of the Postgres `cache` table §14 describes: Phase 0
deliberately deferred writing to Postgres until the Evidence Store forces
real persistence (Phase 2). SQLite is stdlib (zero new dependency), a
single file, and entirely sufficient for one developer's local cache.

WHY raw upstream JSON is cached, not parsed Pydantic models: if a parsing
bug is found and fixed later, cached raw payloads can be re-parsed without
re-hitting a rate-limited API. Parsing happens on every read, not just on
cache miss — see each client module's docstring for why this matters for
`as_of` correctness on snapshot-shaped sources (companyfacts, submissions,
company_tickers.json), which have no upstream as_of parameter at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    source TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    as_of TEXT NOT NULL,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (source, args_hash, as_of)
);
"""


def make_args_hash(args: Mapping[str, Any]) -> str:
    """Stable hash of a call's arguments, independent of dict ordering."""
    canonical = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Cache:
    """A tiny SQLite-backed key-value cache.

    WHY sync sqlite3 wrapped in `asyncio.to_thread` at every call site
    rather than an async SQLite library: local disk I/O on a single file is
    fast enough that `aiosqlite` wouldn't meaningfully help, and stdlib
    `sqlite3` is zero new dependency.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _get_sync(self, *, source: str, args_hash: str, as_of: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM cache WHERE source = ? AND args_hash = ? AND as_of = ?",
                (source, args_hash, as_of),
            ).fetchone()
        if row is None:
            return None
        payload: dict[str, Any] = json.loads(row[0])
        return payload

    def _set_sync(
        self, *, source: str, args_hash: str, as_of: str, payload: dict[str, Any]
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cache (source, args_hash, as_of, payload, fetched_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (source, args_hash, as_of)
                DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at
                """,
                (source, args_hash, as_of, json.dumps(payload), datetime.now(UTC).isoformat()),
            )
            conn.commit()

    async def get(
        self, *, source: str, args: Mapping[str, Any], as_of: date
    ) -> dict[str, Any] | None:
        args_hash = make_args_hash(args)
        return await asyncio.to_thread(
            self._get_sync, source=source, args_hash=args_hash, as_of=as_of.isoformat()
        )

    async def set(
        self, *, source: str, args: Mapping[str, Any], as_of: date, payload: dict[str, Any]
    ) -> None:
        args_hash = make_args_hash(args)
        await asyncio.to_thread(
            self._set_sync,
            source=source,
            args_hash=args_hash,
            as_of=as_of.isoformat(),
            payload=payload,
        )
