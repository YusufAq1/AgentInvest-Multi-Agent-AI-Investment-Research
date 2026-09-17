"""Shared test helpers for the whole suite.

WHY this lives at the top level (not duplicated per test file): every test
that constructs a real `Settings()` needs every required field filled in.
As Settings grows required fields across phases, this is the one place
that needs updating — not every test file that happens to construct one.
"""

from backend.core.config import Settings


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "anthropic_api_key": "sk-test",
        "postgres_user": "agentinvest",
        "postgres_password": "changeme",
        "postgres_db": "agentinvest",
        "edgar_identity": "Test Suite test@example.com",
        "fred_api_key": "fred-test-key",
        "data_retry_max_attempts": 3,
        "data_retry_base_delay_s": 0.001,
        "data_retry_max_delay_s": 0.01,
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type,call-arg]
