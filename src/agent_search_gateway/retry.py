"""Generic configurable exponential retry engine."""

import asyncio
import math
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import TypeVar

from .models import RetryPolicy

T = TypeVar("T")
BeforeAttempt = Callable[[int], None]
OnRetry = Callable[[int, BaseException, float], None]
DelayAdjuster = Callable[[BaseException, float], float]


async def retry_async(
    policy: RetryPolicy,
    operation: Callable[[], Awaitable[T]],
    *,
    is_retryable: Callable[[BaseException], bool],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    before_attempt: BeforeAttempt | None = None,
    on_retry: OnRetry | None = None,
    adjust_delay: DelayAdjuster | None = None,
) -> T:
    for attempt in range(1, policy.max_attempts + 1):
        if before_attempt is not None:
            with suppress(Exception):
                before_attempt(attempt)
        try:
            return await operation()
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            if attempt == policy.max_attempts or not is_retryable(exc):
                raise
            delay = min(
                policy.base_delay_seconds * (2 ** (attempt - 1)),
                policy.max_delay_seconds,
            )
            if adjust_delay is not None:
                with suppress(Exception):
                    adjusted = adjust_delay(exc, delay)
                    if math.isfinite(adjusted):
                        delay = min(max(0.0, adjusted), policy.max_delay_seconds)
            if on_retry is not None:
                with suppress(Exception):
                    on_retry(attempt, exc, delay)
            await sleep(delay)
    raise RuntimeError("retry loop exhausted unexpectedly")
