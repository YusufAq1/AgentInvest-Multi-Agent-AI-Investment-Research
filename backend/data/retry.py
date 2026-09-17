"""Shared async retry/backoff decorator.

WHY this exists as one function instead of copy-pasted per module: CLAUDE.md
§16 requires "exponential backoff with jitter on transient errors" and to
"distinguish transient from permanent errors and don't retry permanent
failures" — five modules (edgar, xbrl, prices, macro, news) all need this,
and hand-rolling it five times would be exactly the kind of duplication the
project's working agreement (§0) warns against ("three similar lines is
better than a premature abstraction" cuts the other way once it's five).

Design split: *this* module only knows the generic policy (how many times,
how long to wait, which exception types count as retryable). Each source
module (e.g. http.py, edgar.py) is the only place that knows *source-specific*
signals — an HTTP status code, a particular library exception — and maps
those onto `TransientDataError`/`PermanentDataError` before this decorator
ever sees them. That keeps "what counts as transient" out of this file
entirely.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from backend.data.errors import TransientDataError

logger = logging.getLogger("agentinvest.data.retry")

P = ParamSpec("P")
T = TypeVar("T")


def with_retry(
    *,
    max_attempts: int,
    base_delay_s: float,
    max_delay_s: float,
    retry_on: tuple[type[Exception], ...] = (TransientDataError,),
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Decorate an async function with exponential backoff + full jitter.

    `delay = min(max_delay_s, base_delay_s * 2**attempt) * uniform(0.5, 1.5)`

    Only exceptions in `retry_on` are retried; anything else (in particular
    `PermanentDataError`, which is never in `retry_on`) propagates on its
    first occurrence — this is the "don't retry permanent failures" half of
    §16's requirement. After `max_attempts` retryable failures, the last
    exception propagates to the caller, which is expected to catch it and
    return a `DataUnavailable` (see errors.py's module docstring).
    """

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            attempt = 0
            while True:
                try:
                    return await func(*args, **kwargs)
                except retry_on as exc:
                    attempt += 1
                    if attempt >= max_attempts:
                        logger.warning(
                            "retry_exhausted",
                            extra={
                                "function": func.__qualname__,
                                "attempts": attempt,
                                "error": str(exc),
                            },
                        )
                        raise
                    delay = min(max_delay_s, base_delay_s * (2 ** (attempt - 1)))
                    delay *= random.uniform(0.5, 1.5)
                    logger.info(
                        "retrying",
                        extra={
                            "function": func.__qualname__,
                            "attempt": attempt,
                            "delay_s": round(delay, 2),
                            "error": str(exc),
                        },
                    )
                    await asyncio.sleep(delay)

        return wrapper

    return decorator
