import asyncio
from collections.abc import Awaitable, Callable

import pytest

from agent_search_gateway.config import resolve_retry_policy
from agent_search_gateway.errors import ConfigFailure, ErrorCode, ExecutionFailure
from agent_search_gateway.retry import retry_async


async def test_retry_engine_uses_configured_attempts_and_exponential_delays_without_sleeping() -> (
    None
):
    policy = resolve_retry_policy({})
    assert policy.max_attempts == 3
    assert policy.base_delay_seconds == 0.25
    assert policy.max_delay_seconds == 2.0
    assert policy.request_timeout_seconds == 30.0

    calls = 0
    delays: list[float] = []

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "retryable")
        return "done"

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    result = await retry_async(
        policy,
        operation,
        is_retryable=lambda exc: isinstance(exc, ExecutionFailure),
        sleep=fake_sleep,
    )
    assert result == "done"
    assert calls == 3
    assert delays == [0.25, 0.5]

    non_retry_calls = 0

    async def non_retryable() -> str:
        nonlocal non_retry_calls
        non_retry_calls += 1
        raise ValueError("stop")

    with pytest.raises(ValueError, match="stop"):
        await retry_async(
            policy,
            non_retryable,
            is_retryable=lambda exc: isinstance(exc, ExecutionFailure),
            sleep=fake_sleep,
        )
    assert non_retry_calls == 1

    exhausted_calls = 0
    final_failure = ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "final")

    async def exhausted() -> str:
        nonlocal exhausted_calls
        exhausted_calls += 1
        if exhausted_calls == policy.max_attempts:
            raise final_failure
        raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "earlier")

    with pytest.raises(ExecutionFailure) as caught:
        await retry_async(
            policy,
            exhausted,
            is_retryable=lambda exc: isinstance(exc, ExecutionFailure),
            sleep=_ignore_sleep,
        )
    assert caught.value is final_failure


async def test_retry_engine_optional_hooks_observe_attempt_and_retry_order() -> None:
    policy = resolve_retry_policy({})
    events: list[tuple[object, ...]] = []
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, f"failure-{calls}")
        return "done"

    async def fake_sleep(delay: float) -> None:
        events.append(("sleep", delay))

    result = await retry_async(
        policy,
        operation,
        is_retryable=lambda exc: isinstance(exc, ExecutionFailure),
        sleep=fake_sleep,
        before_attempt=lambda attempt: events.append(("attempt", attempt)),
        on_retry=lambda attempt, exc, delay: events.append(
            ("retry", attempt, type(exc).__name__, delay)
        ),
    )

    assert result == "done"
    assert events == [
        ("attempt", 1),
        ("retry", 1, "ExecutionFailure", 0.25),
        ("sleep", 0.25),
        ("attempt", 2),
        ("retry", 2, "ExecutionFailure", 0.5),
        ("sleep", 0.5),
        ("attempt", 3),
    ]


async def test_retry_hook_failures_do_not_change_retry_execution() -> None:
    policy = resolve_retry_policy({})
    calls = 0
    sleeps: list[float] = []

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "retryable")
        return "done"

    def broken_before_attempt(_attempt: int) -> None:
        raise RuntimeError("before-attempt hook failed")

    def broken_on_retry(_attempt: int, _exc: BaseException, _delay: float) -> None:
        raise RuntimeError("retry hook failed")

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    result = await retry_async(
        policy,
        operation,
        is_retryable=lambda exc: isinstance(exc, ExecutionFailure),
        sleep=fake_sleep,
        before_attempt=broken_before_attempt,
        on_retry=broken_on_retry,
    )

    assert result == "done"
    assert calls == 2
    assert sleeps == [0.25]


async def test_retry_hooks_do_not_reclassify_non_retryable_or_cancelled_failures() -> None:
    policy = resolve_retry_policy({})
    attempts: list[int] = []
    retries: list[tuple[int, str, float]] = []

    async def non_retryable() -> str:
        raise ValueError("stop")

    with pytest.raises(ValueError, match="stop"):
        await retry_async(
            policy,
            non_retryable,
            is_retryable=lambda exc: isinstance(exc, ExecutionFailure),
            before_attempt=attempts.append,
            on_retry=lambda attempt, exc, delay: retries.append(
                (attempt, type(exc).__name__, delay)
            ),
        )
    assert attempts == [1]
    assert retries == []

    attempts.clear()

    async def cancelled() -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await retry_async(
            policy,
            cancelled,
            is_retryable=lambda exc: True,
            before_attempt=attempts.append,
            on_retry=lambda attempt, exc, delay: retries.append(
                (attempt, type(exc).__name__, delay)
            ),
        )
    assert attempts == [1]
    assert retries == []


@pytest.mark.parametrize(
    "retry_table",
    [
        {"max_attempts": 0},
        {"base_delay_seconds": 0},
        {"max_delay_seconds": -1},
        {"request_timeout_seconds": 0},
        {"base_delay_seconds": float("nan")},
        {"max_delay_seconds": float("inf")},
        {"request_timeout_seconds": float("-inf")},
    ],
)
def test_retry_config_rejects_invalid_values(retry_table: dict[str, object]) -> None:
    with pytest.raises(ConfigFailure) as caught:
        resolve_retry_policy({"retry": retry_table})
    assert caught.value.code is ErrorCode.CONFIG_ERROR


async def test_retry_delay_adjuster_updates_sleep_and_retry_hook() -> None:
    failures = [RuntimeError("first"), RuntimeError("second")]
    calls = 0
    adjustments: list[tuple[BaseException, float]] = []
    sleeps: list[float] = []
    retry_delays: list[float] = []

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls <= len(failures):
            raise failures[calls - 1]
        return "done"

    def adjust_delay(exc: BaseException, delay: float) -> float:
        adjustments.append((exc, delay))
        return delay * 0.9

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    result = await retry_async(
        resolve_retry_policy({}),
        operation,
        is_retryable=lambda exc: isinstance(exc, RuntimeError),
        sleep=sleep,
        adjust_delay=adjust_delay,
        on_retry=lambda _attempt, _exc, delay: retry_delays.append(delay),
    )
    assert result == "done"
    assert calls == 3
    assert adjustments == [(failures[0], 0.25), (failures[1], 0.5)]
    assert sleeps == pytest.approx([0.225, 0.45])
    assert retry_delays == sleeps


@pytest.mark.parametrize(
    ("adjusted", "expected"),
    [(-1.0, 0.0), (5.0, 0.1), (float("nan"), 0.1), (float("inf"), 0.1), (-float("inf"), 0.1)],
)
async def test_retry_delay_adjuster_clamps_invalid_delays(adjusted: float, expected: float) -> None:
    calls = 0
    bases: list[float] = []
    sleeps: list[float] = []
    hooks: list[float] = []

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("retry")
        return "done"

    def adjust_delay(_exc: BaseException, delay: float) -> float:
        bases.append(delay)
        return adjusted

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    assert (
        await retry_async(
            resolve_retry_policy({"retry": {"max_delay_seconds": 0.1}}),
            operation,
            is_retryable=lambda _exc: True,
            sleep=sleep,
            adjust_delay=adjust_delay,
            on_retry=lambda _attempt, _exc, delay: hooks.append(delay),
        )
        == "done"
    )
    assert calls == 2
    assert bases == [0.1]
    assert sleeps == [expected]
    assert hooks == sleeps


async def test_retry_delay_adjuster_failure_preserves_retry_execution() -> None:
    calls = 0
    sleeps: list[float] = []

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("retry")
        return "done"

    def broken_adjuster(_exc: BaseException, _delay: float) -> float:
        raise ValueError("adjuster failed")

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    assert (
        await retry_async(
            resolve_retry_policy({}),
            operation,
            is_retryable=lambda _exc: True,
            sleep=sleep,
            adjust_delay=broken_adjuster,
        )
        == "done"
    )
    assert calls == 2
    assert sleeps == [0.25]


@pytest.mark.parametrize(
    ("max_attempts", "retryable", "failure"),
    [
        (1, True, RuntimeError("exhausted")),
        (3, False, ValueError("terminal")),
        (3, True, asyncio.CancelledError()),
    ],
)
async def test_retry_delay_adjuster_is_skipped_when_no_retry_is_scheduled(
    max_attempts: int, retryable: bool, failure: BaseException
) -> None:
    adjustments: list[float] = []
    sleeps: list[float] = []
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise failure

    def adjust_delay(_exc: BaseException, delay: float) -> float:
        adjustments.append(delay)
        return delay

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    with pytest.raises(type(failure)) as caught:
        await retry_async(
            resolve_retry_policy({"retry": {"max_attempts": max_attempts}}),
            operation,
            is_retryable=lambda _exc: retryable,
            sleep=sleep,
            adjust_delay=adjust_delay,
        )
    assert caught.value is failure
    assert calls == 1
    assert adjustments == sleeps == []


@pytest.mark.parametrize("origin", ["adjuster", "sleep"])
async def test_retry_cancellation_during_scheduling_propagates(origin: str) -> None:
    cancellation = asyncio.CancelledError()
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("retry")

    def adjust_delay(_exc: BaseException, delay: float) -> float:
        if origin == "adjuster":
            raise cancellation
        return delay

    async def sleep(_delay: float) -> None:
        raise cancellation

    with pytest.raises(asyncio.CancelledError) as caught:
        await retry_async(
            resolve_retry_policy({}),
            operation,
            is_retryable=lambda _exc: True,
            sleep=sleep,
            adjust_delay=adjust_delay,
        )
    assert caught.value is cancellation
    assert calls == 1


async def _ignore_sleep(_delay: float) -> None:
    return None


Sleep = Callable[[float], Awaitable[None]]
