"""Application settings, loaded once from the environment / .env file.

WHY this module exists at all: CLAUDE.md §16 bans "magic numbers outside
core/config.py" and §5 requires that model choice be "set per agent in
config, never hardcoded at a call site." Phase 0 has no agents yet, but the
*default* model id and the full per-model pricing table already live here,
so no future agent or call site ever needs to know a raw model string or a
dollar figure — it asks Settings for both.
"""

from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelPricing(BaseModel):
    """Per-million-token USD pricing for one Claude model.

    Broken into five components rather than a single input/output pair
    because Anthropic's prompt caching bills cache writes and cache reads
    at different multiples of the base input price:

      - cache reads   bill at 0.1x base input price
      - cache writes  bill at 1.25x base input price (5-minute TTL, the
                      default) or 2x base input price (1-hour TTL)

    WHY: CLAUDE.md §5 mentions the 0.1x read discount but not the write
    premium. Getting this wrong would silently under-count cost the first
    time an agent actually uses a cache_control breakpoint (Phase 2+), so
    the full breakdown is modeled now even though Phase 0's hello-world
    call engages none of it (its prompt is too small to be cached at all;
    see ClaudeClient.call).
    """

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_write_5m_per_mtok: Decimal
    cache_write_1h_per_mtok: Decimal
    cache_read_per_mtok: Decimal


def _default_model_pricing() -> dict[str, ModelPricing]:
    """Pricing verified against Anthropic's published rates as of 2026-09-15.

    WHY the Sonnet 5 figures differ from CLAUDE.md §5: that section listed
    Sonnet 5 as "~$3 / $15 (verify)" and explicitly asked for the numbers to
    be re-checked before relying on them. The verified rate is $2.00 / $10.00
    per Mtok. See ADR-0008 for the full note on this correction.
    """
    return {
        "claude-haiku-4-5-20251001": ModelPricing(
            input_per_mtok=Decimal("1.00"),
            output_per_mtok=Decimal("5.00"),
            cache_write_5m_per_mtok=Decimal("1.25"),
            cache_write_1h_per_mtok=Decimal("2.00"),
            cache_read_per_mtok=Decimal("0.10"),
        ),
        "claude-sonnet-5": ModelPricing(
            input_per_mtok=Decimal("2.00"),
            output_per_mtok=Decimal("10.00"),
            cache_write_5m_per_mtok=Decimal("2.50"),
            cache_write_1h_per_mtok=Decimal("4.00"),
            cache_read_per_mtok=Decimal("0.20"),
        ),
    }


class Settings(BaseSettings):
    """Process-wide configuration, loaded once from environment / .env.

    Every field is either required (fails fast and loudly if missing,
    rather than silently defaulting — see CLAUDE.md C6 "never fabricate on
    failure," which applies to configuration as much as to data) or has an
    explicit, documented default.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Anthropic
    anthropic_api_key: SecretStr

    # WHY: CLAUDE.md §5 — "start every agent on Haiku," escalate only on
    # measured eval failure. This is the one default every agent falls back
    # to unless it's explicitly overridden per-agent in that agent's own
    # config entry (no per-agent config exists yet — Phase 2+).
    default_model: str = "claude-haiku-4-5-20251001"

    model_pricing: dict[str, ModelPricing] = _default_model_pricing()

    # Logging
    log_level: str = "INFO"

    # Postgres (docker-compose target; nothing connects to it yet in Phase 0)
    postgres_user: str
    postgres_password: SecretStr
    postgres_db: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # WHY required with no default: this is sent as the User-Agent on every
    # direct SEC request AND passed to edgartools' set_identity() — SEC asks
    # for a real contact so it can reach a high-volume caller before
    # blocking them. It must never be hardcoded (no real email belongs in
    # code or .env.example) — the user supplies their own via .env.
    edgar_identity: str

    # WHY SecretStr: unlike edgar_identity (a public contact string SEC
    # wants to see), this is a genuine credential to keep out of logs.
    fred_api_key: SecretStr

    # WHY 10, not the "8 req/s" in CLAUDE.md §15's Phase 1 bullet: SEC's
    # currently documented ceiling is 10 req/s per user (see
    # https://www.sec.gov/about/webmaster-frequently-asked-questions,
    # verified 2026-09-16). edgartools' own internal default throttles to
    # ~9/s as a safety margin under that same real ceiling — 10 here
    # matches the verified source of truth rather than the spec's figure.
    sec_requests_per_second: int = 10

    # WHY SQLite, not the Postgres `cache` table from CLAUDE.md §14: Phase 0
    # deliberately deferred writing to Postgres until the Evidence Store
    # forces real persistence (Phase 2). A local SQLite file is zero new
    # dependency (stdlib sqlite3) and sufficient for Phase 1's caching need.
    data_cache_path: Path = Path("./data/cache.sqlite3")

    # WHY a TTL at all: companyfacts/submissions/company_tickers.json have
    # no upstream as_of parameter — SEC always returns the full current
    # snapshot. We cache that raw snapshot keyed by fetch day and re-filter
    # it in Python on every read; this just bounds how stale "today's
    # snapshot" is allowed to get before re-fetching.
    cache_snapshot_ttl_hours: int = 24

    # Retry/backoff parameters (CLAUDE.md §16: exponential backoff with
    # jitter on transient errors; no magic numbers outside this file).
    data_retry_max_attempts: int = 5
    data_retry_base_delay_s: float = 1.0
    data_retry_max_delay_s: float = 30.0

    # WHY None, not a hardcoded model string: §5 says model choice is set
    # per agent in config, never hardcoded at a call site. None means "fall
    # back to default_model" (Haiku) — an override only needs to be set
    # here if/when an eval shows the Financial Agent needs escalating.
    financial_agent_model: str | None = None
    # WHY 4096, not 1024: a real run (multiple claims, each with a
    # statement plus an evidence_ids array) legitimately needs more than
    # 1024 output tokens — confirmed by a live run hitting
    # stop_reason="max_tokens" at the smaller value. This is a genuine
    # output-size need, not the earlier bug where the INPUT prompt was
    # accidentally bloated with unused historical evidence (see
    # backend/agents/financial.py's lazy evidence construction and
    # docs/INTERVIEW_NOTES.md).
    financial_agent_max_tokens: int = 4096

    # WHY 512/64: Phase 3's RAG chunking. 512 tokens keeps a chunk
    # topically coherent (a query like "customer concentration" shouldn't
    # have to compete against one embedding vector covering all of Item
    # 1A) while staying well above the ~50-100 token floor where a chunk
    # becomes too short to carry standalone meaning. 64 tokens (~12.5%
    # overlap) preserves continuity across a boundary that splits a
    # sentence or a claim spanning two chunks, without materially
    # inflating storage/embedding cost. See backend/rag/chunking.py.
    rag_chunk_max_tokens: int = 512
    rag_chunk_overlap_tokens: int = 64

    # WHY 0.6: edgartools' Section.confidence varies by detection_method
    # ('toc' detection is high-confidence; 'pattern' detection is the
    # weakest). Below this floor, chunking.py falls back to recomputing a
    # section's char offsets via substring search rather than trusting
    # edgartools' own start_offset/end_offset — the RAG-layer instance of
    # this project's established "never trust an upstream API's own
    # filtering/claims without a local, testable check" principle (see the
    # as_of rechecks throughout backend/data). See ADR-0015.
    rag_section_confidence_floor: float = 0.6

    # WHY 60: the original Reciprocal Rank Fusion paper's (Cormack et al.
    # 2009) empirically-chosen constant, adopted widely (e.g.
    # Elasticsearch's own hybrid search defaults to it) because it
    # flattens the impact of rank 1 vs rank 2 without needing a
    # metric-specific calibration between cosine distance and ts_rank,
    # which are on incompatible scales. See ADR-0014.
    rag_rrf_k: int = 60
    # WHY 5: Phase 3's literal exit criterion is "recall@5 measured and
    # recorded" (CLAUDE.md §15).
    rag_retrieval_top_k: int = 5
    # WHY 20: each leg (dense, full-text) over-fetches beyond top_k before
    # RRF fusion narrows back down — fusion needs more candidates per leg
    # than the final result size to have anything meaningful to fuse.
    rag_retrieval_candidate_k: int = 20

    # Filings Agent (Phase 4)
    filings_agent_model: str | None = None
    filings_agent_max_tokens: int = 4096
    # WHY 1, not configurable-yet-larger: matches scripts/rag_demo.py's
    # exact, already-verified pattern — the single most recent 10-K. No
    # Manager exists yet (Phase 5) to decide "which/how many filings
    # actually matter for this ticker." A documented first-cut default,
    # not a considered ceiling.
    filings_agent_max_filings: int = 1

    # News Agent (Phase 4)
    news_agent_model: str | None = None
    news_agent_max_tokens: int = 4096
    # WHY duplicated here rather than reading NewsClient.get_8k_events's
    # own lookback_days=365 default: CLAUDE.md §16 bans magic numbers
    # outside core/config.py, and the News Agent — the caller deciding a
    # research window — should own this choice, not NewsClient (a plain
    # data-fetching client). NewsClient's own default stays as a
    # library-level fallback for other callers that don't pass one.
    news_agent_lookback_days: int = 365

    # Competitive Agent (Phase 4)
    competitive_agent_model: str | None = None
    competitive_agent_max_tokens: int = 4096

    @property
    def postgres_dsn(self) -> str:
        """Single source of truth for the plain (sync-style) connection
        string. Used by Alembic, which needs a direct, session-mode
        connection for DDL and its own transactional/advisory-lock
        behavior — see postgres_async_dsn's docstring for why the async
        engine reuses the exact same fields rather than a separate pooled
        DSN.

        WHY a property instead of a stored field: assembling it here means
        the individual POSTGRES_* vars (used directly by docker-compose.yml)
        and the DSN (used by SQLAlchemy/Alembic) can never drift apart into
        two different sources of truth.
        """
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def postgres_async_dsn(self) -> str:
        """The asyncpg-driver DSN SQLAlchemy's async engine needs.

        WHY the same host/port as postgres_dsn, not a separate pooled
        (PgBouncer) connection: Supabase offers both a direct connection
        and a transaction-mode pooled connection on a different port, but
        this project deliberately uses the direct connection for
        everything in Phase 3 — a single DSN shape is simpler, and
        PgBouncer's transaction-mode pooling is a real optimization to
        reach for once there's an actual concurrency need (e.g. Phase 5's
        parallel agents hammering the DB), not before. See ADR-0013.
        """
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
