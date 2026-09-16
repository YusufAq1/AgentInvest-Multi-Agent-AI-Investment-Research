"""Tests for backend.core.config.

WHY `_env_file=None` everywhere: without it, Settings would also read a real
.env file if one happened to exist on disk during the test run, letting the
committed .env.example (or a developer's local .env) leak into what's
supposed to be an isolated, monkeypatched-env-vars test.
"""

import pytest
from backend.core.config import Settings
from pydantic import ValidationError


def test_settings_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.setenv("POSTGRES_USER", "agentinvest")
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret")
    monkeypatch.setenv("POSTGRES_DB", "agentinvest")
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_PORT", "5433")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.anthropic_api_key.get_secret_value() == "sk-test-123"
    assert settings.default_model == "claude-haiku-4-5-20251001"
    assert settings.postgres_dsn == "postgresql://agentinvest:s3cret@db.internal:5433/agentinvest"


def test_settings_missing_required_var_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("POSTGRES_USER", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_DB", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
