import asyncio
import math
from itertools import count

import pytest

from agent_search_gateway.config import ResolvedRatePolicy, ResolvedRateWindow
from agent_search_gateway.rate_limit import (
    InitialRateBudget,
    RateGate,
    RateLimitWaitTimeout,
    consume_initial_rate_budget,
    initial_rate_budget,
)
from tests.support.controlled import ManualClock


def window_gate(clock: ManualClock, requests: int, period: float) -> RateGate:
    policy = ResolvedRatePolicy((ResolvedRateWindow(requests, period),))
    return RateGate(policy, monotonic=clock.monotonic, sleep=clock.sleep)


async def test_rate_gate_waits_for_oldest_request_in_sliding_window() -> None:
    clock = ManualClock()
    gate = window_gate(clock, 1, 1)
    assert await gate.permit(timeout_seconds=2) == 0
    second = asyncio.create_task(gate.permit(timeout_seconds=2))
    await asyncio.sleep(0)
    assert not second.done()
    await clock.advance_to(0.875)
    assert not second.done()
    await clock.advance_to(1)
    assert second.done()
    assert second.result() == 1


@pytest.mark.parametrize(("capacity", "period"), [(10, 1), (100, 60)])
async def test_capacity_rolls_from_the_oldest_permit(capacity: int, period: int) -> None:
    clock = ManualClock()
    gate = window_gate(clock, capacity, period)
    # Start just before a fixed-window boundary and stagger the last permit.
    await clock.advance_to(period - 0.25)
    first = [asyncio.create_task(gate.permit(timeout_seconds=period)) for _ in range(capacity - 1)]
    await asyncio.sleep(0)
    assert all(task.done() and task.result() == 0 for task in first)
    await clock.advance_to(period)
    assert await gate.permit(timeout_seconds=0) == 0
    overflow = [asyncio.create_task(gate.permit(timeout_seconds=period)) for _ in range(capacity)]
    await asyncio.sleep(0)
    assert not any(task.done() for task in overflow)
    await clock.advance_to(2 * period - 0.5)
    assert not any(task.done() for task in overflow)
    await clock.advance_to(2 * period - 0.25)
    assert sum(task.done() for task in overflow) == capacity - 1
    await clock.advance_to(2 * period)
    assert all(task.done() for task in overflow)
    assert sorted(task.result() for task in overflow) == [period - 0.25] * (capacity - 1) + [period]


async def test_three_windows_expire_independently() -> None:
    clock = ManualClock()
    windows = (
        ResolvedRateWindow(30, 60),
        ResolvedRateWindow(500, 3600),
        ResolvedRateWindow(1000, 86400),
    )
    gate = RateGate(ResolvedRatePolicy(windows), monotonic=clock.monotonic, sleep=clock.sleep)

    async def send_now(count: int) -> None:
        for _ in range(count):
            assert await gate.permit(timeout_seconds=0) == 0

    async def wait_until(opening: float) -> None:
        started = clock.now
        waiting = asyncio.create_task(gate.permit(timeout_seconds=opening - started))
        await asyncio.sleep(0)
        assert not waiting.done()
        await clock.advance_to(opening - 0.125)
        assert not waiting.done()
        await clock.advance_to(opening)
        assert waiting.done() and waiting.result() == opening - started

    await send_now(1)
    for hour, next_opening in [(0, 3600), (3600, 86400)]:
        # Each hour starts with one permit already used. Fill 16 minutes of 30, then 20.
        await send_now(29)
        await wait_until(hour + 60)
        await send_now(29)
        for minute in range(2, 16):
            await clock.advance_to(hour + minute * 60)
            await send_now(30)
        await clock.advance_to(hour + 16 * 60)
        await send_now(20)
        await wait_until(next_opening)


@pytest.mark.parametrize("with_window", [False, True])
async def test_min_interval_applies_from_clock_zero(with_window: bool) -> None:
    clock = ManualClock()
    windows = (ResolvedRateWindow(2, 10),) if with_window else ()
    gate = RateGate(
        ResolvedRatePolicy(windows, min_interval_seconds=3),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert await gate.permit(timeout_seconds=0) == 0
    for opening in [3, 10 if with_window else 6]:
        started = clock.now
        waiting = asyncio.create_task(gate.permit(timeout_seconds=10))
        await asyncio.sleep(0)
        await clock.advance_to(opening - 0.125)
        assert not waiting.done()
        await clock.advance_to(opening)
        assert waiting.done() and waiting.result() == opening - started


async def test_simultaneous_callers_receive_one_permit_per_second() -> None:
    clock = ManualClock()
    gate = window_gate(clock, 1, 1)
    tasks = [asyncio.create_task(gate.permit(timeout_seconds=24)) for _ in range(25)]
    await asyncio.sleep(0)
    for second in range(25):
        await clock.advance_to(second)
        assert sum(task.done() for task in tasks) == second + 1
    assert sorted(task.result() for task in tasks) == list(range(25))


async def test_empty_policy_returns_zero_and_provider_gates_are_independent() -> None:
    clock = ManualClock()
    ticks = count()
    unlimited = RateGate(ResolvedRatePolicy(), monotonic=ticks.__next__, sleep=clock.sleep)
    assert await unlimited.permit(timeout_seconds=0) == 0

    policy = ResolvedRatePolicy((ResolvedRateWindow(1, 60),))
    first = RateGate(policy, monotonic=clock.monotonic, sleep=clock.sleep)
    other = RateGate(policy, monotonic=clock.monotonic, sleep=clock.sleep)
    assert await first.permit(timeout_seconds=0) == 0
    waiting = asyncio.create_task(first.permit(timeout_seconds=60))
    await asyncio.sleep(0)
    assert await other.permit(timeout_seconds=0) == 0
    assert not waiting.done()
    await clock.advance_to(60)
    assert waiting.done() and waiting.result() == 60


@pytest.mark.parametrize("cancel_after_permit", [False, True])
async def test_cancellation_only_counts_successful_permits(cancel_after_permit: bool) -> None:
    clock = ManualClock()
    gate = window_gate(clock, 1, 1)
    acquired = asyncio.Event()

    async def request() -> None:
        await gate.permit(timeout_seconds=1)
        acquired.set()
        await asyncio.Event().wait()

    if not cancel_after_permit:
        assert await gate.permit(timeout_seconds=0) == 0
    cancelled = asyncio.create_task(request())
    await asyncio.sleep(0)
    assert acquired.is_set() is cancel_after_permit
    await clock.advance_to(0.25)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert not clock.sleepers
    successor = asyncio.create_task(gate.permit(timeout_seconds=1))
    await asyncio.sleep(0)
    assert not successor.done()
    await clock.advance_to(1)
    assert successor.done() and successor.result() == 0.75
    assert not clock.sleepers


async def test_timeout_expires_without_spending_a_permit() -> None:
    clock = ManualClock()
    gate = window_gate(clock, 1, 3600)
    assert await gate.permit(timeout_seconds=0) == 0
    blocked = asyncio.create_task(gate.permit(timeout_seconds=0.5))
    await asyncio.sleep(0)
    await clock.advance_to(0.375)
    assert not blocked.done()
    await clock.advance_to(0.5)
    assert blocked.done()
    with pytest.raises(RateLimitWaitTimeout) as caught:
        blocked.result()
    assert caught.value.waited_seconds == 0.5
    assert not clock.sleepers
    await clock.advance_to(3600)
    assert await gate.permit(timeout_seconds=0) == 0


async def test_ready_estimates_never_reserve_or_postpone_a_permit() -> None:
    clock = ManualClock()
    gate = RateGate(
        ResolvedRatePolicy((ResolvedRateWindow(2, 5),), min_interval_seconds=1),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    for at, expected in [(0, 0), (0.5, 0.5), (1, 0), (4.5, 0.5), (5, 0), (9, 0)]:
        await clock.advance_to(at)
        assert [gate.next_available_in_seconds() for _ in range(3)] == [expected] * 3
        if expected == 0:
            assert await gate.permit(timeout_seconds=0) == 0


@pytest.mark.parametrize(
    ("budget", "wake_at", "allowed"), [(1, 1, True), (0.5, 0.5, False), (0.5, 1.2, False)]
)
async def test_early_and_late_wakeups_respect_the_deadline(
    budget: float, wake_at: float, allowed: bool
) -> None:
    clock = ManualClock()
    gate = window_gate(clock, 1, 1)
    assert await gate.permit(timeout_seconds=0) == 0
    with pytest.raises(RateLimitWaitTimeout) as immediate:
        await gate.permit(timeout_seconds=0)
    assert immediate.value.waited_seconds == 0
    waiting = asyncio.create_task(gate.permit(timeout_seconds=budget))
    await asyncio.sleep(0)
    await clock.advance_to(0.25, wake_early=True)
    assert not waiting.done()
    await clock.advance_to(wake_at)
    assert waiting.done()
    if allowed:
        assert waiting.result() == wake_at
    else:
        with pytest.raises(RateLimitWaitTimeout) as expired:
            waiting.result()
        assert expired.value.waited_seconds == wake_at
        await clock.advance_to(max(1, wake_at))
        assert await gate.permit(timeout_seconds=0) == 0


async def test_fractional_window_opens_at_the_representable_expiry() -> None:
    clock = ManualClock()
    period = 0.2
    gate = window_gate(clock, 1, period)
    await clock.advance_to(0.1)
    assert await gate.permit(timeout_seconds=0) == 0
    opening = clock.now + period
    await clock.advance_to(math.nextafter(opening, -math.inf))
    with pytest.raises(RateLimitWaitTimeout):
        await gate.permit(timeout_seconds=0)
    await clock.advance_to(opening)
    assert gate.next_available_in_seconds() == 0
    assert await gate.permit(timeout_seconds=0) == 0


async def test_initial_rate_budget_is_one_shot_and_bound_to_gate_and_current_task() -> None:
    first = RateGate(ResolvedRatePolicy())
    other = RateGate(ResolvedRatePolicy())

    async def in_child() -> InitialRateBudget | None:
        return consume_initial_rate_budget(first)

    with initial_rate_budget(first, remaining_seconds=0, waited_seconds=0.5):
        assert await asyncio.create_task(in_child()) is None
        assert consume_initial_rate_budget(other) is None
        with initial_rate_budget(other, remaining_seconds=0.25, waited_seconds=0.25):
            assert consume_initial_rate_budget(first) is None
            assert consume_initial_rate_budget(other) == InitialRateBudget(0.25, 0.25)
            assert consume_initial_rate_budget(other) is None
        assert consume_initial_rate_budget(first) == InitialRateBudget(0, 0.5)
        assert consume_initial_rate_budget(first) is None
    with initial_rate_budget(first, remaining_seconds=0.5, waited_seconds=0):
        pass
    assert consume_initial_rate_budget(first) is None


async def test_initial_budget_requires_an_owning_task_even_for_a_loop_callback() -> None:
    gate = RateGate(ResolvedRatePolicy())
    observed: asyncio.Future[InitialRateBudget | None] = asyncio.get_running_loop().create_future()

    def callback() -> None:
        try:
            with initial_rate_budget(gate, remaining_seconds=0.1, waited_seconds=0.4):
                result = consume_initial_rate_budget(gate)
        except Exception as exc:
            observed.set_exception(exc)
        else:
            observed.set_result(result)

    asyncio.get_running_loop().call_soon(callback)
    assert await observed is None
