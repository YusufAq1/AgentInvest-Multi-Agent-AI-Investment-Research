"""Shared fixtures for backend.data tests."""

from pathlib import Path

import pytest
from backend.core.config import Settings
from backend.data.cache import Cache

from tests.conftest import make_settings

__all__ = ["make_settings"]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    return Cache(tmp_path / "test_cache.sqlite3")
