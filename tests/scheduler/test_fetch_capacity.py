import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager

import httpx
import pytest

from agent_search_gateway.concurrency import CapacityGate
from agent_search_gateway.config import ResolvedRatePolicy, ResolvedRateWindow
from agent_search_gateway.credential_pool import CredentialMember, CredentialPool
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import FetchOutcome, LLMInvocation, RetryPolicy
from agent_search_gateway.providers.contracts import ChatMessage, URLFetchCandidate
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.pooled import PooledWebProvider
from agent_search_gateway.rate_limit import RateGate, SelectionBudget, consume_initial_rate_budget
from agent_search_gateway.scheduler.fetch import FetchScheduler
from agent_search_gateway.url_normalization import NormalizedURL, normalize_url
from tests.support.controlled import ManualClock
from tests.support.fakes import pooled_web


class _AlwaysAcceptJudge:
    name = "judge"

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]:
        return {"ok": True}

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        return "unused"

    async def aclose(self) -> None:
        return None


class _ControlledFetchProvider:
    def __init__(self, name: str, *, hold: bool = False) -> None:
        self.name = name
        self.hold = hold
        self.calls: list[NormalizedURL] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        self.calls.append(url)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            if self.hold:
                await self.release.wait()
            return URLFetchCandidate(raw_content=f"raw:{self.name}:{url}")
        finally:
            self.active -= 1


def _scheduler(
    providers: list[_ControlledFetchProvider],
    capacities: dict[str, CapacityGate],
    clock: ManualClock | None = None,
    rates: dict[str, RateGate] | None = None,
) -> FetchScheduler:
    invocation = LLMInvocation("judge", "model", {})
    stages = LLMStages(
        {"judge": _AlwaysAcceptJudge()},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
    )
    facades = [
        pooled_web(
            provider,
            capacities[provider.name],
            (rates or {}).get(provider.name),
            monotonic=clock.monotonic if clock else time.monotonic,
            sleep=clock.sleep if clock else asyncio.sleep,
        )
        for provider in providers
    ]
    return FetchScheduler(
        facades,
        stages,
        monotonic=clock.monotonic if clock else time.monotonic,
        request_timeout_seconds=0.5 if clock else 30,
    )


async def test_fetch_scheduler_uses_available_provider_without_parallel_attempts_for_one_job() -> (
    None
):
    url = normalize_url("https://example.com/one")

    first = _ControlledFetchProvider("first")
    second = _ControlledFetchProvider("second")
    capacities = {"first": CapacityGate(1), "second": CapacityGate(1)}
    scheduler = _scheduler([first, second], capacities)

    held_first = await capacities["first"].try_lease()
    assert held_first is not None
    try:
        result = await scheduler.fetch_until_accepted(url)
    finally:
        await held_first.release()
    assert result.kind == "accepted"
    assert first.calls == []
    assert second.calls == [url]

    wait_first = _ControlledFetchProvider("first")
    wait_second = _ControlledFetchProvider("second")
    wait_capacities = {"first": CapacityGate(1), "second": CapacityGate(1)}
    wait_scheduler = _scheduler([wait_first, wait_second], wait_capacities)
    held_a = await wait_capacities["first"].try_lease()
    held_b = await wait_capacities["second"].try_lease()
    assert held_a is not None and held_b is not None
    waiting_task = asyncio.create_task(wait_scheduler.fetch_until_accepted(url))
    await asyncio.sleep(0)
    assert wait_first.calls == [] and wait_second.calls == []
    await held_b.release()
    waited_result = await waiting_task
    await held_a.release()
    assert waited_result.kind == "accepted"
    assert wait_first.calls == []
    assert wait_second.calls == [url]

    order_first = _ControlledFetchProvider("first")
    order_second = _ControlledFetchProvider("second")
    order_capacities = {"first": CapacityGate(1), "second": CapacityGate(1)}
    order_result = await _scheduler(
        [order_first, order_second],
        order_capacities,
    ).fetch_until_accepted(url)
    assert order_result.kind == "accepted"
    assert order_first.calls == [url]
    assert order_second.calls == []

    parallel_first = _ControlledFetchProvider("first", hold=True)
    parallel_second = _ControlledFetchProvider("second", hold=True)
    parallel_capacities = {"first": CapacityGate(1), "second": CapacityGate(1)}
    parallel_scheduler = _scheduler([parallel_first, parallel_second], parallel_capacities)
    url_two = normalize_url("https://example.com/two")
    task_one = asyncio.create_task(parallel_scheduler.fetch_until_accepted(url))
    await parallel_first.entered.wait()
    task_two = asyncio.create_task(parallel_scheduler.fetch_until_accepted(url_two))
    await parallel_second.entered.wait()
    assert parallel_first.active == 1
    assert parallel_second.active == 1
    assert parallel_first.max_active == 1
    assert parallel_second.max_active == 1
    parallel_first.release.set()
    parallel_second.release.set()
    results = await asyncio.gather(task_one, task_two)
    assert [result.kind for result in results] == ["accepted", "accepted"]


async def test_fetch_prefers_rate_ready_provider_over_earlier_limited_provider() -> None:
    first = _ControlledFetchProvider("first")
    second = _ControlledFetchProvider("second")
    capacities = {"first": CapacityGate(1), "second": CapacityGate(1)}
    gate = RateGate(ResolvedRatePolicy(min_interval_seconds=3600))
    await gate.permit(timeout_seconds=0)
    rates = {first.name: gate}
    url = normalize_url("https://example.test")

    result = await _scheduler([first, second], capacities, rates=rates).fetch_until_accepted(url)

    assert result.kind == "accepted"
    assert first.calls == []
    assert second.calls == [url]


async def _blocked_gate(
    rates: dict[str, RateGate], name: str, clock: ManualClock, interval: float
) -> RateGate:
    gate = RateGate(
        ResolvedRatePolicy(min_interval_seconds=interval),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    await gate.permit(timeout_seconds=0)
    rates[name] = gate
    return gate


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not condition():
            await asyncio.sleep(0)


@asynccontextmanager
async def _fetching(scheduler: FetchScheduler) -> AsyncIterator[asyncio.Task[FetchOutcome]]:
    task = asyncio.create_task(
        scheduler.fetch_until_accepted(normalize_url("https://example.test"))
    )
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_fetch_wakes_at_observed_rate_opening_after_slow_logging(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock()
    provider = _ControlledFetchProvider("limited")
    capacities = {"limited": CapacityGate(1)}
    rates: dict[str, RateGate] = {}
    await _blocked_gate(rates, provider.name, clock, 0.375)

    def slow_log(record: logging.LogRecord) -> None:
        if getattr(record, "gateway_event", None) == "rate_limit_waiting":
            clock.now += 0.25

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(caplog.handler, "emit", slow_log)
    async with _fetching(_scheduler([provider], capacities, clock, rates)) as task:
        await _until(lambda: bool(clock.sleepers))
        assert list(clock.sleepers.values()) == [0.375]
        await clock.advance_to(0.375)
        assert (await asyncio.wait_for(task, 1)).kind == "accepted"


@pytest.mark.parametrize("wake_at", [0.5, 2.0])
async def test_fetch_all_rate_blocked_expires_even_if_late_wakeup_finds_capacity(
    wake_at: float, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    clock = ManualClock()
    providers = [_ControlledFetchProvider("a"), _ControlledFetchProvider("b")]
    capacities = {"a": CapacityGate(1), "b": CapacityGate(1)}
    rates: dict[str, RateGate] = {}
    for provider in providers:
        await _blocked_gate(rates, provider.name, clock, 1)
    scheduler = _scheduler(providers, capacities, clock, rates)
    async with _fetching(scheduler) as task:
        await _until(lambda: bool(clock.sleepers))
        assert set(clock.sleepers.values()) == {0.5}
        await clock.advance_to(wake_at)
        outcome = await asyncio.wait_for(task, 1)
    assert outcome.kind == "execution_failure"
    assert [f.reason for f in outcome.failures] == ["rate_limit_wait_timeout"] * 2
    assert not any(p.calls for p in providers)
    assert not clock.sleepers
    events = [
        (vars(r)["gateway_event"], vars(r)["gateway_fields"])
        for r in caplog.records
        if getattr(r, "gateway_event", None)
    ]
    for name in ("a", "b"):
        common = {"provider": name, "stage": "fetch", "attempt": 1}
        assert ("rate_limit_waiting", {**common, "estimated_wait_ms": 1000}) in events
        assert (
            "rate_limit_wait_timeout",
            {**common, "elapsed_ms": int(wake_at * 1000), "reason": "rate_limit_wait_timeout"},
        ) in events
    assert not any(event.startswith("http_") or "preview" in event for event, _ in events)
    await clock.advance_to(max(1, wake_at))
    fresh = await scheduler.fetch_until_accepted(normalize_url("https://example.test"))
    assert fresh.kind == "accepted" and fresh.failures == ()


@pytest.mark.parametrize("acquired", [False, True])
async def test_first_http_permit_uses_remaining_selection_budget(
    acquired: bool, caplog: pytest.LogCaptureFixture
) -> None:
    clock = ManualClock()
    caplog.set_level(logging.DEBUG)
    capacities = {"a": CapacityGate(1)}
    rates: dict[str, RateGate] = {}
    if acquired:
        gate = RateGate(
            ResolvedRatePolicy((ResolvedRateWindow(2, 0.5),)),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        await gate.permit(timeout_seconds=0)
        await clock.advance_to(0.1)
        await gate.permit(timeout_seconds=0)
        rates["a"] = gate
    else:
        gate = await _blocked_gate(rates, "a", clock, 0.4)
    started = clock.now
    sends: list[float] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        sends.append(clock.now)
        await clock.sleep(0.125)
        return httpx.Response(200, text="body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(2, 0, 0, 0.5),
            provider_name="a",
            rate_gate=gate,
            monotonic=clock.monotonic,
        )

        class RacedProvider(_ControlledFetchProvider):
            async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
                # Another request wins the permit after the scheduler's read-only estimate.
                await gate.permit(timeout_seconds=0)
                self.entered.set()
                body = await executor.request_text("GET", str(url), stage="fetch")
                return URLFetchCandidate(body)

        provider = RacedProvider("a")
        scheduler = _scheduler([provider], capacities, clock, rates)
        async with _fetching(scheduler) as task:
            await _until(lambda: bool(clock.sleepers))
            await clock.advance_to(started + 0.4)
            async with asyncio.timeout(1):
                await provider.entered.wait()
            assert list(clock.sleepers.values()) == pytest.approx([started + 0.5])
            await clock.advance_to(started + 0.5)
            if acquired:
                assert sends == [clock.now]
                await clock.advance_to(clock.now + 0.125)
            outcome = await asyncio.wait_for(task, 1)

    events = [
        (vars(r)["gateway_event"], vars(r)["gateway_fields"])
        for r in caplog.records
        if getattr(r, "gateway_event", None)
    ]
    if acquired:
        assert outcome.kind == "accepted"
        assert [f["waited_ms"] for e, f in events if e == "rate_limit_acquired"] == [500]
        assert [f["elapsed_ms"] for e, f in events if e == "http_attempt_completed"] == [125]
    else:
        assert not sends
        assert outcome.kind == "execution_failure"
        assert [f.reason for f in outcome.failures] == ["rate_limit_wait_timeout"]
        assert [f["elapsed_ms"] for e, f in events if e == "rate_limit_wait_timeout"] == [500]
        assert not any(e.startswith("http_attempt") for e, _ in events)
    assert not clock.sleepers
    assert capacities["a"].in_use == 0


@pytest.mark.parametrize("signal", ["capacity", "rate", "cancel", "waiter-error"])
async def test_fetch_waits_for_first_relevant_signal_and_joins_all_waiters(
    signal: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = ManualClock()
    first, second = _ControlledFetchProvider("a"), _ControlledFetchProvider("b")
    capacities = {"a": CapacityGate(1), "b": CapacityGate(1)}
    rates: dict[str, RateGate] = {}
    await _blocked_gate(rates, "b", clock, 0.25)
    scheduler = _scheduler([first, second], capacities, clock, rates)
    capacity = capacities["a"]
    fail_waiter = asyncio.Event()
    if signal == "waiter-error":

        async def failing_wait() -> None:
            await fail_waiter.wait()
            raise ValueError("capacity observation failed")

        monkeypatch.setattr(capacity, "wait_until_available", failing_wait)

    caplog.set_level(logging.DEBUG)
    baseline = asyncio.all_tasks()
    async with capacity.lease() as held, _fetching(scheduler) as task:
        await _until(lambda: bool(clock.sleepers))
        assert set(clock.sleepers.values()) == {0.25, 0.5}
        if signal == "capacity":
            await clock.advance_to(0.125)
            await held.release()
        elif signal == "rate":
            await clock.advance_to(0.25)
        elif signal == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            fail_waiter.set()
            with pytest.raises(ValueError, match="capacity observation failed"):
                await asyncio.wait_for(task, 1)
        if signal in {"capacity", "rate"}:
            assert (await asyncio.wait_for(task, 1)).kind == "accepted"
    assert len(first.calls) == int(signal == "capacity")
    assert len(second.calls) == int(signal == "rate")
    assert not clock.sleepers
    assert asyncio.all_tasks() == baseline
    assert capacity.in_use == capacities["b"].in_use == 0
    waits = [r for r in caplog.records if getattr(r, "gateway_event", "") == "scheduler_waiting"]
    assert len(waits) == 1


class _BudgetProvider(_ControlledFetchProvider):
    def __init__(self, name: str, gate: RateGate) -> None:
        super().__init__(name)
        self.gate = gate
        self.budgets: list[SelectionBudget | None] = []

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        self.budgets.append(consume_initial_rate_budget(self.gate))
        return await super().fetch(url)


async def test_rate_and_capacity_waits_spend_one_first_attempt_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = ManualClock()
    capacities = {"a": CapacityGate(1, provider="a", quota_kind="web", logger=logging.getLogger())}
    rates: dict[str, RateGate] = {}
    gate = await _blocked_gate(rates, "a", clock, 0.1)
    provider = _BudgetProvider("a", gate)
    caplog.set_level(logging.DEBUG)
    scheduler = _scheduler([provider], capacities, clock, rates)
    async with capacities["a"].lease() as held, _fetching(scheduler) as task:
        await _until(lambda: bool(clock.sleepers))
        await clock.advance_to(0.1)
        await _until(
            lambda: any(getattr(r, "gateway_event", "") == "quota_waiting" for r in caplog.records)
        )
        await clock.advance_to(0.375)
        await held.release()
        assert (await asyncio.wait_for(task, 1)).kind == "accepted"
    assert provider.budgets == [SelectionBudget(0.125, 0.375)]


async def test_selected_provider_retains_peer_expiry_and_zero_remaining_at_exact_deadline() -> None:
    clock = ManualClock()
    capacities = {"expired": CapacityGate(1), "ready": CapacityGate(1)}
    rates: dict[str, RateGate] = {}
    expired = _ControlledFetchProvider("expired")
    await _blocked_gate(rates, expired.name, clock, 1)
    gate = await _blocked_gate(rates, "ready", clock, 0.5)
    ready = _BudgetProvider("ready", gate)
    scheduler = _scheduler([expired, ready], capacities, clock, rates)
    async with _fetching(scheduler) as task:
        await _until(lambda: bool(clock.sleepers))
        await clock.advance_to(0.5)
        outcome = await asyncio.wait_for(task, 1)
    assert not expired.calls
    assert ready.budgets == [SelectionBudget(0, 0.5)]
    assert outcome.kind == "accepted"
    assert [failure.reason for failure in outcome.failures] == ["rate_limit_wait_timeout"]


async def test_zero_budget_reblocked_during_waiter_cleanup_expires_without_another_timer(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    clock = ManualClock()
    capacities = {"limited": CapacityGate(1), "other": CapacityGate(1)}
    rates: dict[str, RateGate] = {}
    gate = await _blocked_gate(rates, "limited", clock, 0.5)
    limited = _ControlledFetchProvider("limited")
    failure = ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "other failed")

    class FailingProvider(_ControlledFetchProvider):
        async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
            raise failure

    scheduler = _scheduler([limited, FailingProvider("other")], capacities, clock, rates)
    capacity = capacities["other"]
    original_wait = capacity.wait_until_available
    async with capacity.lease() as held:

        async def wait_with_competition() -> None:
            try:
                await original_wait()
            finally:
                await gate.permit(timeout_seconds=0)
                await clock.advance_to(0.75)
                await held.release()

        monkeypatch.setattr(capacity, "wait_until_available", wait_with_competition)
        async with _fetching(scheduler) as task:
            await _until(lambda: bool(clock.sleepers))
            await clock.advance_to(0.5)
            outcome = await asyncio.wait_for(task, 1)
    assert outcome.kind == "execution_failure"
    assert failure in outcome.failures
    assert [f.reason for f in outcome.failures if f is not failure] == ["rate_limit_wait_timeout"]
    assert not limited.calls
    assert not clock.sleepers
    assert [
        vars(r)["gateway_fields"]["elapsed_ms"]
        for r in caplog.records
        if getattr(r, "gateway_event", "") == "rate_limit_wait_timeout"
    ] == [500]


async def test_timer_first_scheduled_at_deadline_rechecks_without_another_wait() -> None:
    clock = ManualClock()
    capacities = {"limited": CapacityGate(1)}
    rates: dict[str, RateGate] = {}
    await _blocked_gate(rates, "limited", clock, 1)
    provider = _ControlledFetchProvider("limited")
    async with _fetching(_scheduler([provider], capacities, clock, rates)) as task:
        # Let selection start the wait, then advance before the timer task gets its first turn.
        await asyncio.sleep(0)
        assert not clock.sleepers
        await clock.advance_to(0.5)
        outcome = await asyncio.wait_for(task, 1)
    assert outcome.kind == "execution_failure"
    assert [f.reason for f in outcome.failures] == ["rate_limit_wait_timeout"]
    assert not provider.calls and not clock.sleepers


@pytest.mark.parametrize("both_blocked", [False, True])
async def test_fetch_uses_first_logical_provider_with_ready_credential(both_blocked: bool) -> None:
    first, second, later = [_ControlledFetchProvider(name) for name in ("a1", "a2", "b")]
    one, two = CapacityGate(1), CapacityGate(1)
    pool = PooledWebProvider(
        CredentialPool(
            "web",
            "a",
            [
                CredentialMember(first, one),
                CredentialMember(second, two),
            ],
        )
    )
    invocation = LLMInvocation("judge", "model", {})
    stages = LLMStages(
        {"judge": _AlwaysAcceptJudge()},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
    )
    scheduler = FetchScheduler([pool, pooled_web(later)], stages)
    url = normalize_url("https://example.test")
    async with one.lease():
        held = await two.try_lease() if both_blocked else None
        try:
            assert (await scheduler.fetch_until_accepted(url)).kind == "accepted"
        finally:
            if held is not None:
                await held.release()
    assert first.calls == []
    assert second.calls == ([] if both_blocked else [url])
    assert later.calls == ([url] if both_blocked else [])


async def test_capacity_only_selection_budget_expires_without_dispatch() -> None:
    clock = ManualClock()
    adapter = _ControlledFetchProvider("a")
    capacity = CapacityGate(1)
    async with capacity.lease(), _fetching(_scheduler([adapter], {"a": capacity}, clock)) as task:
        await _until(lambda: bool(clock.sleepers))
        await clock.advance_to(0.5)
        outcome = await asyncio.wait_for(task, 1)
    assert outcome.kind == "execution_failure"
    assert [failure.reason for failure in outcome.failures] == ["rate_limit_wait_timeout"]
    assert not adapter.calls and not clock.sleepers


async def test_late_pool_timeout_survives_cancellation_during_waiter_cleanup() -> None:
    clock = ManualClock()
    first, second, other = [_ControlledFetchProvider(name) for name in ("a1", "a2", "b")]
    cleanup_started, finish_cleanup = asyncio.Event(), asyncio.Event()
    observers: list[asyncio.Task[None]] = []

    class SlowCleanupGate(CapacityGate):
        async def wait_until_available(self) -> None:
            try:
                await super().wait_until_available()
            finally:
                cleanup_started.set()
                await finish_cleanup.wait()

    class ObservedPool(PooledWebProvider):
        async def wait_fetch_available(self, budget: SelectionBudget) -> None:
            observer = asyncio.current_task()
            assert observer is not None
            observers.append(observer)
            await super().wait_fetch_available(budget)

    rates: dict[str, RateGate] = {}
    await _blocked_gate(rates, "a2", clock, 0.625)
    await _blocked_gate(rates, "b", clock, 2)
    capacity = SlowCleanupGate(1)
    pool = ObservedPool(
        CredentialPool(
            "web",
            "a",
            [
                CredentialMember(first, capacity),
                CredentialMember(second, rate=rates["a2"]),
            ],
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
    )
    later = pooled_web(other, rate=rates["b"], monotonic=clock.monotonic, sleep=clock.sleep)
    invocation = LLMInvocation("judge", "model", {})
    stages = LLMStages(
        {"judge": _AlwaysAcceptJudge()},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
    )
    scheduler = FetchScheduler(
        [pool, later], stages, request_timeout_seconds=0.5, monotonic=clock.monotonic
    )
    async with capacity.lease(), _fetching(scheduler) as task:
        try:
            await _until(lambda: len(clock.sleepers) == 2)
            await clock.advance_to(0.75)
            await asyncio.wait_for(cleanup_started.wait(), 1)
            await _until(lambda: bool(observers[0].cancelling()))
        finally:
            finish_cleanup.set()
        outcome = await asyncio.wait_for(task, 1)
    assert not first.calls and not second.calls and not other.calls
    assert outcome.kind == "execution_failure"
    assert [failure.reason for failure in outcome.failures] == ["rate_limit_wait_timeout"] * 2
