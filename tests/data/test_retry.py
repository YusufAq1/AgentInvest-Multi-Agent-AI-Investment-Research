"""Tests for backend.data.retry — the shared backoff decorator every
client in backend/data builds on.
"""

import pytest
from backend.data.errors import PermanentDataError, TransientDataError
from backend.data.retry import with_retry


async def test_transient_error_is_retried_then_exhausts() -> None:
    calls = 0

    @with_retry(max_attempts=3, base_delay_s=0.001, max_delay_s=0.01)
    async def flaky() -> str:
        nonlocal calls
        calls += 1
        raise TransientDataError("boom")

    with pytest.raises(TransientDataError):
        await flaky()

    assert calls == 3


async def test_transient_error_succeeds_after_retry() -> None:
    calls = 0

    @with_retry(max_attempts=5, base_delay_s=0.001, max_delay_s=0.01)
    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise TransientDataError("boom")
        return "ok"

    result = await flaky()

    assert result == "ok"
    assert calls == 2


async def test_permanent_error_is_never_retried() -> None:
    calls = 0

    @with_retry(max_attempts=5, base_delay_s=0.001, max_delay_s=0.01)
    async def broken() -> str:
        nonlocal calls
        calls += 1
        raise PermanentDataError("nope")

    with pytest.raises(PermanentDataError):
        await broken()

    assert calls == 1
