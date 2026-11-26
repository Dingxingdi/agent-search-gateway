import asyncio
import logging
import math
from itertools import count

import httpx
import pytest

from agent_search_gateway.concurrency import CapacityGate, make_capacity_gate
from agent_search_gateway.config import ResolvedRatePolicy, ResolvedRateWindow
from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import CredentialIdentity, SecretValue
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.web.baiduqianfan import BaiduQianfanAdapter
from agent_search_gateway.rate_limit import (
    RateGate,
    RateLimitWaitTimeout,
    SelectionBudget,
    consume_initial_rate_budget,
    initial_rate_budget,
    make_rate_gate,
)
from tests.support.controlled import ManualClock


def window_gate(clock: ManualClock, requests: int, period: float) -> RateGate:
    policy = ResolvedRatePolicy((ResolvedRateWindow(requests, period),))
    return RateGate(policy, monotonic=clock.monotonic, sleep=clock.sleep)


@pytest.mark.parametrize("limit", [None, 1])
@pytest.mark.parametrize(
    ("policy", "delay"),
    [
        (ResolvedRatePolicy(), None),
        (ResolvedRatePolicy((ResolvedRateWindow(1, 8),)), 8),
        (ResolvedRatePolicy(min_interval_seconds=2), 2),
        (ResolvedRatePolicy((ResolvedRateWindow(1, 8),), min_interval_seconds=2), 8),
    ],
)
async def test_capacity_and_rate_gates_are_independently_optional(
    limit: int | None, policy: ResolvedRatePolicy, delay: float | None
) -> None:
    clock = ManualClock()
    capacity = make_capacity_gate(limit)
    rate = make_rate_gate(policy, monotonic=clock.monotonic, sleep=clock.sleep)
    assert (capacity is None) == (limit is None)
    assert (rate is None) == (delay is None)
    lease = await capacity.try_lease() if capacity else None
    if capacity is not None:
        assert lease is not None and await capacity.try_lease() is None
    if rate is not None:
        assert rate.next_available_in_seconds() == 0
        assert await rate.permit(timeout_seconds=0) == 0
    if lease is not None:
        await lease.release()
        assert capacity is not None and capacity.in_use == 0
    if rate is not None:
        assert rate.next_available_in_seconds() == delay


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


@pytest.mark.parametrize(
    "policy",
    [
        ResolvedRatePolicy(min_interval_seconds=0.5),
        ResolvedRatePolicy((ResolvedRateWindow(1, 0.5),)),
    ],
    ids=["min-interval", "rolling-window"],
)
async def test_slow_acquisition_logging_preserves_physical_request_spacing(
    policy: ResolvedRatePolicy, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock()
    sends: list[float] = []

    class SlowFirstAcquisition(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if getattr(record, "gateway_event", None) == "rate_gate_acquired" and not sends:
                clock.now += 0.75

    logger = logging.getLogger("agent_search_gateway.rate_limit")
    caplog.set_level(logging.DEBUG, logger=logger.name)
    monkeypatch.setattr(logger, "handlers", [SlowFirstAcquisition()])
    gate = RateGate(
        policy,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        identity=CredentialIdentity("web", "test", 0),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        sends.append(clock.now)
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0, 0, 5), provider_name="test", rate_gate=gate
        )
        await executor.request_text("GET", "https://example.test", stage="fetch")
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(executor.request_text("GET", "https://example.test", stage="fetch"))
            await clock.advance_to(0.75)
            await clock.advance_to(1.25)

    assert sends == [0.75, 1.25]


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


async def test_selection_budget_is_not_reset_by_notifications() -> None:
    clock = ManualClock()
    budget = SelectionBudget(1)
    independent = SelectionBudget(1)
    gate = CapacityGate(1)

    async def wait_for_notifications(started: asyncio.Event) -> None:
        with budget.waiting(clock.monotonic):
            started.set()
            await asyncio.gather(gate.wait_until_available(), gate.wait_until_available())

    for wake_at, expected_wait in [(0.25, 0.25), (10.5, 0.5)]:
        started = asyncio.Event()
        async with gate.lease():
            waiter = asyncio.create_task(wait_for_notifications(started))
            await asyncio.wait_for(started.wait(), 1)
            await clock.advance_to(wake_at)
        await asyncio.wait_for(waiter, 1)
        assert budget.waited_seconds == expected_wait
        assert budget.remaining_seconds == 1 - expected_wait
        # Time spent executing between selection waits is outside this budget.
        await clock.advance_to(wake_at + 10)
    assert independent.waited_seconds == 0 and independent.remaining_seconds == 1


async def test_selection_budget_rejects_late_notification_before_rechecking() -> None:
    clock = ManualClock()
    budget = SelectionBudget(0.5)
    resumed = False

    async def coordinate() -> None:
        nonlocal resumed
        with budget.waiting(clock.monotonic):
            await clock.sleep(budget.remaining_seconds)
        resumed = True

    waiter = asyncio.create_task(coordinate())
    await asyncio.sleep(0)
    await clock.advance_to(1)
    with pytest.raises(RateLimitWaitTimeout) as caught:
        await asyncio.wait_for(waiter, 1)
    assert not resumed
    assert caught.value.waited_seconds == budget.waited_seconds == 1
    assert budget.remaining_seconds == 0


async def test_first_permit_charges_shared_budget_once() -> None:
    clock = ManualClock()
    gate = window_gate(clock, 1, 2)
    await gate.permit(timeout_seconds=0)
    budget = SelectionBudget(2)
    with budget.waiting(clock.monotonic):
        await clock.advance_to(0.5)
    sends: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sends.append(clock.now)
        clock.now += 0.25
        if len(sends) == 1:
            return httpx.Response(503, headers={"Retry-After": "1"})
        return httpx.Response(200, text="done")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(2, 0, 0, 5), provider_name="test", rate_gate=gate, sleep=clock.sleep
        )

        async def fetch() -> str:
            with initial_rate_budget(gate, budget):
                return await executor.request_text("GET", "https://example.test", stage="fetch")

        async with asyncio.timeout(1), asyncio.TaskGroup() as tasks:
            request = tasks.create_task(fetch())
            await clock.advance_to(0.5)
            await clock.advance_to(1, wake_early=True)
            await clock.advance_to(2)
            assert sends == [2]
            await clock.advance_to(3.25)
            await clock.advance_to(4)
        assert request.result() == "done"
    assert sends == [2, 4]
    assert budget.waited_seconds == 2
    assert budget.remaining_seconds == 0


@pytest.mark.parametrize(("cancel", "elapsed"), [(False, 1.25), (True, 0.125)])
async def test_unsent_first_permit_accounts_for_wait_on_timeout_or_cancel(
    cancel: bool, elapsed: float
) -> None:
    clock = ManualClock()
    gate = window_gate(clock, 1, 1)
    await gate.permit(timeout_seconds=0)
    budget = SelectionBudget(0.5, waited_seconds=0.25)

    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("a blocked first permit must not dispatch an HTTP request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(2, 0, 0, 5), provider_name="test", rate_gate=gate
        )

        async def fetch() -> str:
            with initial_rate_budget(gate, budget):
                return await executor.request_text("GET", "https://example.test", stage="fetch")

        request = asyncio.create_task(fetch())
        await asyncio.sleep(0)
        assert list(clock.sleepers.values()) == [0.5]
        await clock.advance_to(elapsed)
        if cancel:
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(request, 1)
        else:
            with pytest.raises(ExecutionFailure) as caught:
                await asyncio.wait_for(request, 1)
            assert caught.value.reason == "rate_limit_wait_timeout"
    assert budget.waited_seconds == 0.25 + elapsed
    assert budget.remaining_seconds == max(0, 0.5 - elapsed)
    assert not clock.sleepers


async def test_initial_rate_budget_is_one_shot_and_bound_to_gate_and_current_task() -> None:
    first = RateGate(ResolvedRatePolicy())
    other = RateGate(ResolvedRatePolicy())

    async def in_child() -> SelectionBudget | None:
        return consume_initial_rate_budget(first)

    with initial_rate_budget(first, SelectionBudget(0, 0.5)):
        assert await asyncio.create_task(in_child()) is None
        assert consume_initial_rate_budget(other) is None
        with initial_rate_budget(other, SelectionBudget(0.25, 0.25)):
            assert consume_initial_rate_budget(first) is None
            assert consume_initial_rate_budget(other) == SelectionBudget(0.25, 0.25)
            assert consume_initial_rate_budget(other) is None
        assert consume_initial_rate_budget(first) == SelectionBudget(0, 0.5)
        assert consume_initial_rate_budget(first) is None
    with initial_rate_budget(first, SelectionBudget(0.5, 0)):
        pass
    assert consume_initial_rate_budget(first) is None


async def test_initial_budget_requires_an_owning_task_even_for_a_loop_callback() -> None:
    gate = RateGate(ResolvedRatePolicy())
    observed: asyncio.Future[SelectionBudget | None] = asyncio.get_running_loop().create_future()

    def callback() -> None:
        try:
            with initial_rate_budget(gate, SelectionBudget(0.1, 0.4)):
                result = consume_initial_rate_budget(gate)
        except Exception as exc:
            observed.set_exception(exc)
        else:
            observed.set_result(result)

    asyncio.get_running_loop().call_soon(callback)
    assert await observed is None


def _baidu_adapter(
    client: httpx.AsyncClient,
    gate: RateGate,
    clock: ManualClock,
    *,
    max_attempts: int = 1,
    timeout: float = 5,
) -> BaiduQianfanAdapter:
    return BaiduQianfanAdapter(
        name="baiduqianfan",
        secret=SecretValue("synthetic"),
        http_executor=HttpJsonExecutor(
            client,
            RetryPolicy(max_attempts, 0, 0, timeout),
            provider_name="baiduqianfan",
            rate_gate=gate,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        ),
    )


async def test_baiduqianfan_two_searches_share_three_request_window() -> None:
    clock = ManualClock()
    gate = window_gate(clock, 3, 1)
    sends: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sends.append(clock.now)
        payload: dict[str, object] = (
            {"references": []}
            if request.url.path.endswith("/web_search")
            else {"web_page_result": {"web_pages": []}}
        )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = _baidu_adapter(client, gate, clock)
        async with asyncio.TaskGroup() as tasks:
            first = tasks.create_task(adapter.search("first query"))
            second = tasks.create_task(adapter.search("second query"))
            await clock.advance_to(0)
            await clock.advance_to(0)
            assert sends == [0, 0, 0]
            assert len(clock.sleepers) == 1
            assert not (first.done() and second.done())
            await clock.advance_to(0.999)
            assert sends == [0, 0, 0]
            await clock.advance_to(1)
        assert first.result() == second.result() == []
    assert sends == [0, 0, 0, 1]


async def test_baiduqianfan_branch_retry_reserves_shared_rate_permit() -> None:
    clock = ManualClock()
    gate = window_gate(clock, 2, 1)
    sends: list[tuple[str, float]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        sends.append((path, clock.now))
        if path.endswith("/web_search") and sum(p == path for p, _ in sends) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        payload: dict[str, object] = (
            {"references": []}
            if path.endswith("/web_search")
            else {"web_page_result": {"web_pages": []}}
        )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = _baidu_adapter(client, gate, clock, max_attempts=2)
        async with asyncio.TaskGroup() as tasks:
            search = tasks.create_task(adapter.search("retry shares physical budget"))
            await clock.advance_to(0)
            await clock.advance_to(0)
            await clock.advance_to(0)
            assert [sent_at for _, sent_at in sends] == [0, 0]
            assert set(clock.sleepers.values()) == {1}
            await clock.advance_to(0.999)
            assert len(sends) == 2
            await clock.advance_to(1)
        assert search.result() == []
    assert [sent_at for _, sent_at in sends] == [0, 0, 1]
    assert [path.rsplit("/", 1)[1] for path, _ in sends].count("web_search") == 2


async def test_baiduqianfan_rate_wait_timeout_does_not_send_blocked_attempt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    clock = ManualClock()
    gate = window_gate(clock, 1, 1)
    sends: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sends.append(request.url.path)
        item = {
            "url": "https://example.test/surviving-branch",
            "title": "Surviving title",
            "content": "Surviving body",
        }
        payload: dict[str, object] = (
            {"references": [item]}
            if request.url.path.endswith("/web_search")
            else {"web_page_result": {"web_pages": [item]}}
        )
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = _baidu_adapter(client, gate, clock, max_attempts=2, timeout=0.5)
        async with asyncio.TaskGroup() as tasks:
            search = tasks.create_task(adapter.search("preserve successful sibling"))
            await clock.advance_to(0)
            await clock.advance_to(0)
            assert len(sends) == 1
            await clock.advance_to(0.499)
            assert len(sends) == 1
            await clock.advance_to(0.5)
        assert [hit.url for hit in search.result()] == ["https://example.test/surviving-branch"]
        await clock.advance_to(1)
        assert len(sends) == 1
    timeouts = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "rate_limit_wait_timeout"
    ]
    assert len(timeouts) == 1
    assert timeouts[0]["reason"] == "rate_limit_wait_timeout"
    [partial] = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_partial_failure"
    ]
    assert partial["branch"] != sends[0].rsplit("/", 1)[1]
    assert partial["error_type"] == "ExecutionFailure"
    assert not clock.sleepers
