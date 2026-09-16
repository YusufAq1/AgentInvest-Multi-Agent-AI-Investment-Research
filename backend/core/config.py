"""Application settings, loaded once from the environment / .env file.

WHY this module exists at all: CLAUDE.md §16 bans "magic numbers outside
core/config.py" and §5 requires that model choice be "set per agent in
config, never hardcoded at a call site." Phase 0 has no agents yet, but the
*default* model id and the full per-model pricing table already live here,
so no future agent or call site ever needs to know a raw model string or a
dollar figure — it asks Settings for both.
"""

from decimal import Decimal

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

    @property
    def postgres_dsn(self) -> str:
        """Single source of truth for the connection string.

        WHY a property instead of a stored field: assembling it here means
        the individual POSTGRES_* vars (used directly by docker-compose.yml)
        and the DSN (used by a future SQLAlchemy engine) can never drift
        apart into two different sources of truth.
        """
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
