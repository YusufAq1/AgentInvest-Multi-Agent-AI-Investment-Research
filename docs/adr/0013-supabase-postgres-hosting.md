# ADR-0013: Supabase-hosted Postgres over self-hosted docker-compose for Phase 3 dev

## Status
Accepted — 2026-09-19

## Context
Phase 3 (CLAUDE.md §15) is the first phase that needs a real Postgres+
pgvector database — Phases 0-2 deliberately deferred it (SQLite cache,
in-memory Evidence Store; see ADR-0012). `docker-compose.yml` already
defines a local `pgvector/pgvector:pg16` service from Phase 0, but Docker
Desktop was not running in this project's dev environment when Phase 3
started (`docker ps` failed with "cannot connect to the Docker daemon").
Rather than block on fixing local Docker, a hosted alternative was
evaluated.

## Decision
**Use a Supabase project's Postgres instance** (free tier; pgvector
ships enabled) as the primary connection target for local dev, accessed
via a **plain, direct Postgres connection** (SQLAlchemy async + asyncpg),
not the `supabase-py`/PostgREST client — PostgREST has no clean way to
express HNSW-ordered vector queries or arbitrary `tsvector` predicates,
both of which hybrid retrieval (ADR-0014) genuinely needs as raw SQL.

**Simplification: one DSN, not two.** Supabase offers both a direct
connection (port 5432, session mode) and a pooled PgBouncer connection
(port 6543, transaction mode). This project uses the direct connection for
everything — Alembic migrations *and* the app's own runtime queries — via
a single `Settings.postgres_async_dsn` built from the existing
`postgres_host/port/user/password/db` fields (no new required config).
PgBouncer's transaction-mode pooling is a real optimization to reach for
once there's an actual concurrency need (e.g. Phase 5's parallel research
agents contending for connections), not before — introducing a second DSN
shape ahead of that need would be complexity without a consumer.

`docker-compose.yml` **stays in the repo unchanged** as a self-host
fallback (and is exactly what CI's ephemeral Postgres service container
uses — see the CI diff in this phase). It just isn't the path this
project's own local dev documents as primary, which is a real, acknowledged
discrepancy from CLAUDE.md §4's original "Local infra: Docker Compose"
line — recorded here rather than silently diverging from it.

## Alternatives considered
- **Fix/require local Docker and use docker-compose.yml as originally
  specified.** Rejected for this environment: the Docker daemon wasn't
  reachable, and requiring it blocks all of Phase 3's DB-touching work on
  an unrelated environment issue rather than the actual retrieval-system
  work the phase is about.
- **`supabase-py` (PostgREST) as the access layer.** Rejected: no clean
  path to HNSW-ordered vector search or raw `tsvector` predicates — the
  two things hybrid retrieval is built around — without dropping to raw
  SQL via Supabase's Postgres function/RPC mechanism, which is more
  complex than just connecting directly.
- **A separate pooled (PgBouncer) DSN alongside the direct one from day
  one.** Rejected as premature: no code in Phase 3 has a concurrency
  profile that needs it, and it doubles the connection-config surface for
  a benefit with no current consumer.

## Consequences
Easy: any Postgres-provider swap (a different host, a self-hosted
instance, a future managed offering) is a `.env` change only —
`Settings.postgres_async_dsn` is the single place that assembles the
connection string, matching the precedent `postgres_dsn` already set in
Phase 0.

Hard: this project's local-dev docs (`.env.example`) now describe two
paths (Supabase or docker-compose) instead of one, which is more to keep
in sync if either changes. Revisit the single-DSN simplification once a
real concurrency bottleneck against Supabase's direct connection actually
shows up — at that point, add the pooled DSN as a second, documented
config field rather than replacing the direct one, since Alembic will
still need the direct connection regardless.
