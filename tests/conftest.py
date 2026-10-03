"""Shared test helpers for the whole suite.

WHY this lives at the top level (not duplicated per test file): every test
that constructs a real `Settings()` needs every required field filled in.
As Settings grows required fields across phases, this is the one place
that needs updating — not every test file that happens to construct one.
"""

import inspect
from typing import Any
from unittest.mock import MagicMock

from anthropic.resources.messages import Messages
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


_REAL_CREATE_SIGNATURE = inspect.signature(Messages.create)


def assert_sdk_accepts_every_call(mock_sdk_client: MagicMock) -> None:
    """Bind every `messages.create` call a mocked SDK recorded against the
    REAL installed SDK's signature. Raises TypeError on an unknown keyword.

    WHY this exists: a bare MagicMock accepts any keyword, so a test can
    pass while the real SDK would reject the call. That's exactly how an
    unsupported `temperature` argument (anthropic 1.6.0's `create()` has no
    sampling parameters at all) passed every test and then crashed the
    first live run. Any test that mocks the SDK should call this.
    """
    calls = mock_sdk_client.messages.create.call_args_list
    assert calls, "the mocked SDK recorded no messages.create calls"
    for call in calls:
        kwargs: dict[str, Any] = dict(call.kwargs)
        _REAL_CREATE_SIGNATURE.bind(None, *call.args, **kwargs)
