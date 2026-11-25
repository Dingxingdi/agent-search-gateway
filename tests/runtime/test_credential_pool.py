import asyncio

import pytest

from agent_search_gateway.concurrency import CapacityGate
from agent_search_gateway.config import ResolvedRatePolicy
from agent_search_gateway.credential_pool import CredentialMember, CredentialPool
from agent_search_gateway.errors import ConfigFailure, ErrorCode, ExecutionFailure, InputFailure
from agent_search_gateway.rate_limit import RateGate, SelectionBudget, consume_initial_rate_budget
from tests.support.controlled import ManualClock


@pytest.mark.parametrize("failure", [401, 402, 403, 408, 429, 503, "network"])
async def test_http_retries_stay_on_member_before_fallback(failure: int | str) -> None:
    from contextlib import AsyncExitStack

    import httpx

    from agent_search_gateway.config import ResolvedRateWindow
    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.providers.http import HttpJsonExecutor

    clock = ManualClock()
    attempts = 1 if failure in (401, 402, 403) else 3
    sends: list[tuple[str, float]] = []

    async def sleep(delay: float) -> None:
        await clock.advance_to(clock.now + delay)

    def respond(request: httpx.Request) -> httpx.Response:
        member = request.url.path
        sends.append((member, clock.now))
        if member == "/b/":
            return httpx.Response(200, json={"ok": True})
        if failure == "network":
            raise httpx.ConnectError("offline", request=request)
        assert isinstance(failure, int)
        return httpx.Response(failure, headers={"Retry-After": "5"})

    async with AsyncExitStack() as stack:
        members = []
        gates = []
        for member, limit in (("a", attempts), ("b", 1)):
            gate = RateGate(
                ResolvedRatePolicy(windows=(ResolvedRateWindow(limit, 60),)),
                monotonic=clock.monotonic,
                sleep=sleep,
            )
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=f"https://example.test/{member}/",
                    transport=httpx.MockTransport(respond),
                )
            )
            executor = HttpJsonExecutor(
                client,
                RetryPolicy(3, 1, 1, 2),
                provider_name="example",
                rate_gate=gate,
                sleep=sleep,
                monotonic=clock.monotonic,
            )
            members.append(CredentialMember(executor, rate=gate))
            gates.append(gate)
        pool = CredentialPool("web", "example", members)

        async def request(executor: HttpJsonExecutor) -> object:
            return await executor.request_json("GET", "", stage="search")

        async with asyncio.timeout(2):
            assert await pool.invoke(request) == {"ok": True}
    delay = 1 if failure == "network" else 5
    expected = [("/a/", i * delay) for i in range(attempts)]
    assert sends == [*expected, ("/b/", expected[-1][1])]
    assert [gate.next_available_in_seconds() for gate in gates] == [60 - clock.now, 60]


async def test_selection_and_first_http_permit_share_budget_but_retry_has_its_own() -> None:
    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.providers.http import HttpJsonExecutor

    clock = ManualClock()
    sends: list[float] = []

    async def sleep(delay: float) -> None:
        await clock.advance_to(clock.now + delay)

    def respond(request: httpx.Request) -> httpx.Response:
        sends.append(clock.now)
        return httpx.Response(503 if len(sends) == 1 else 200, json={})

    gate = RateGate(
        ResolvedRatePolicy(min_interval_seconds=1), monotonic=clock.monotonic, sleep=sleep
    )
    await gate.permit(timeout_seconds=0)
    budget = SelectionBudget(2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(2, 0, 0, 1),
            provider_name="example",
            rate_gate=gate,
            monotonic=clock.monotonic,
            sleep=sleep,
        )
        pool = CredentialPool(
            "web",
            "example",
            [CredentialMember(executor, rate=gate)],
            monotonic=clock.monotonic,
            sleep=sleep,
        )

        async def fetch(executor: HttpJsonExecutor) -> object:
            assert budget == SelectionBudget(1, 1)
            # Another operation takes the available slot between selection and dispatch.
            await gate.permit(timeout_seconds=0)
            return await executor.request_json("GET", "https://example.test", stage="fetch")

        async with asyncio.timeout(2):
            assert await pool.invoke(fetch, budget=budget) == {}
    assert sends == [2, 3]
    assert budget == SelectionBudget(0, 2)


async def test_round_robin_advances_at_selection() -> None:
    pool = CredentialPool("web", "example", [CredentialMember(label) for label in "ABC"])
    selected: list[str] = []
    completed: list[str] = []
    ends = [asyncio.Event() for _ in range(3)]

    async def run(started: asyncio.Event, end: asyncio.Event) -> None:
        reservation = await pool.try_reserve(set())
        assert reservation is not None
        try:
            selected.append(reservation.member.adapter)
            started.set()
            await end.wait()
            completed.append(reservation.member.adapter)
        finally:
            await reservation.release()

    async with asyncio.timeout(3), asyncio.TaskGroup() as group:
        tasks = []
        for end in ends:
            started = asyncio.Event()
            tasks.append(group.create_task(run(started, end)))
            await started.wait()
        assert selected == ["A", "B", "C"]
        for task, end in reversed(list(zip(tasks, ends, strict=True))):
            end.set()
            await task
        assert completed == ["C", "B", "A"]
        await run(asyncio.Event(), ends[0])
        assert selected == ["A", "B", "C", "A"]


async def select_and_release(pool: CredentialPool[str], attempted: set[int]) -> tuple[int, str]:
    reservation = await pool.try_reserve(attempted)
    assert reservation is not None
    await reservation.release()
    return reservation.index, reservation.member.adapter


async def test_round_robin_keeps_array_position_when_member_is_blocked() -> None:
    gate = CapacityGate(1)
    pool = CredentialPool(
        "web",
        "example",
        [CredentialMember("A"), CredentialMember("B", gate), CredentialMember("C")],
    )
    async with gate.lease():
        assert await select_and_release(pool, set()) == (0, "A")
        assert await select_and_release(pool, set()) == (2, "C")
    assert await select_and_release(pool, set()) == (0, "A")
    assert await select_and_release(pool, set()) == (1, "B")
    assert gate.in_use == 0


async def test_fill_first_returns_to_first_ready_member() -> None:
    gate = CapacityGate(1)
    pool = CredentialPool(
        "web",
        "example",
        [CredentialMember("A", gate), CredentialMember("B")],
        strategy="fill_first",
    )
    assert await select_and_release(pool, set()) == (0, "A")
    first = await pool.try_reserve(set())
    assert first is not None and first.member.adapter == "A"
    assert gate.in_use == 1
    assert await select_and_release(pool, set()) == (1, "B")
    await first.release()
    assert await select_and_release(pool, set()) == (0, "A")


async def test_selection_peeks_at_each_members_own_rate_gate() -> None:
    clock = ManualClock()
    policy = ResolvedRatePolicy(min_interval_seconds=8)
    first = RateGate(policy, monotonic=clock.monotonic, sleep=clock.sleep)
    second = RateGate(policy, monotonic=clock.monotonic, sleep=clock.sleep)
    pool = CredentialPool(
        "web",
        "example",
        [CredentialMember("A", rate=first), CredentialMember("B", rate=second)],
        strategy="fill_first",
    )
    await first.permit(timeout_seconds=0)
    assert await select_and_release(pool, set()) == (1, "B")
    assert second.next_available_in_seconds() == 0
    await second.permit(timeout_seconds=0)
    assert await pool.try_reserve(set()) is None
    await clock.advance_to(8)
    assert await select_and_release(pool, set()) == (0, "A")
    assert first.next_available_in_seconds() == 0


async def test_attempted_indexes_belong_to_the_caller() -> None:
    pool = CredentialPool("web", "example", [CredentialMember(label) for label in "AB"])
    attempted = {0}
    assert await select_and_release(pool, attempted) == (1, "B")
    assert attempted == {0}
    assert await pool.try_reserve({0, 1}) is None
    assert await select_and_release(pool, set()) == (0, "A")


async def test_unlimited_128_operations_enter_before_release() -> None:
    pool = CredentialPool("web", "example", [CredentialMember("A")])
    entered: list[int] = []
    completed: list[int] = []
    all_entered = asyncio.Event()
    finish = asyncio.Event()

    async def run(number: int) -> None:
        reservation = await pool.try_reserve(set())
        assert reservation is not None and reservation.member.adapter == "A"
        try:
            entered.append(number)
            if len(entered) == 128:
                all_entered.set()
            await finish.wait()
            completed.append(number)
        finally:
            await reservation.release()

    async with asyncio.timeout(3), asyncio.TaskGroup() as group:
        for number in range(128):
            group.create_task(run(number))
        await all_entered.wait()
        assert set(entered) == set(range(128))
        assert completed == []
        finish.set()
    assert sorted(completed) == list(range(128))


async def test_reservation_transfers_ownership_once() -> None:
    gate = CapacityGate(1)
    pool = CredentialPool("web", "example", [CredentialMember("A", gate)])
    reservation = await pool.try_reserve(set())
    assert reservation is not None
    async with reservation.consume() as member:
        assert member.adapter == "A"
        await reservation.release()
        assert gate.in_use == 1
        with pytest.raises(RuntimeError):
            async with reservation.consume():
                pytest.fail("duplicate consumer entered")
    assert gate.in_use == 0
    await reservation.release()
    with pytest.raises(RuntimeError):
        async with reservation.consume():
            pytest.fail("released reservation reused")


async def test_repeated_cancellation_during_handoff_cleanup_restores_capacity() -> None:
    gate = CapacityGate(1)
    pool = CredentialPool("web", "example", [CredentialMember("A", gate)])
    reservation = await pool.try_reserve(set())
    assert reservation is not None
    entered = asyncio.Event()

    async def run() -> None:
        async with reservation.consume():
            entered.set()
            await asyncio.Event().wait()

    async with asyncio.timeout(3), asyncio.TaskGroup() as group:
        task = group.create_task(run())
        await entered.wait()
        # Contend the real gate lock so that cancellation lands inside lease release.
        async with gate._condition:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await task
    assert gate.in_use == 0
    assert await select_and_release(pool, set()) == (0, "A")


async def test_each_member_owns_its_capacity_slots() -> None:
    first, second = CapacityGate(1), CapacityGate(2)
    members = [CredentialMember("A", first), CredentialMember("B", second)]
    pool = CredentialPool("web", "example", members, strategy="fill_first")
    members.reverse()
    snapshots = await asyncio.gather(*(pool.try_reserve(set()) for _ in range(4)))
    reservations = [reservation for reservation in snapshots if reservation is not None]
    assert [(r.index, r.member.adapter) for r in reservations] == [(0, "A"), (1, "B"), (1, "B")]
    assert (first.in_use, second.in_use) == (1, 2)
    await asyncio.gather(*(reservation.release() for reservation in reservations))
    assert (first.in_use, second.in_use) == (0, 0)


async def test_cancel_before_consumer_enters_discards_the_unused_reservation() -> None:
    gate = CapacityGate(1)
    pool = CredentialPool("web", "example", [CredentialMember("A", gate)])
    reservation = await pool.try_reserve(set())
    assert reservation is not None

    async def run() -> None:
        async with reservation.consume():
            pytest.fail("cancelled operation entered")

    async with asyncio.timeout(3), asyncio.TaskGroup() as group:
        try:
            task = group.create_task(run())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await asyncio.gather(reservation.release(), reservation.release())
    assert gate.in_use == 0
    with pytest.raises(RuntimeError):
        async with reservation.consume():
            pytest.fail("discarded reservation reused")


@pytest.mark.parametrize("result", [[], None, ["partial"]])
async def test_empty_result_and_no_match_do_not_fallback(result: object) -> None:
    pool = CredentialPool("web", "example", [CredentialMember(label) for label in "AB"])
    calls: list[str] = []

    async def run(label: str) -> object:
        calls.append(label)
        return result

    assert await pool.invoke(run) is result
    assert calls == ["A"]


@pytest.mark.parametrize("exhaust", [False, True])
async def test_terminal_execution_failure_tries_each_member_once(exhaust: bool) -> None:
    gates = [CapacityGate(1) for _ in "ABC"]
    members = [CredentialMember(label, gate) for label, gate in zip("ABC", gates, strict=True)]
    pool = CredentialPool("web", "example", members)
    calls: list[str] = []
    last = ExecutionFailure(ErrorCode.PROTOCOL_ERROR, "synthetic terminal result")

    async def run(label: str) -> str:
        calls.append(label)
        assert sum(gate.in_use for gate in gates) == 1
        if label == "C":
            if exhaust:
                raise last
            return label
        raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "synthetic provider failure")

    if exhaust:
        with pytest.raises(ExecutionFailure) as caught:
            await pool.invoke(run)
        assert caught.value is last
    else:
        assert await pool.invoke(run) == "C"
    assert calls == ["A", "B", "C"]
    assert all(gate.in_use == 0 for gate in gates)


async def test_waits_for_unattempted_ready_member() -> None:
    gate = CapacityGate(1)
    pool = CredentialPool("web", "example", [CredentialMember("A"), CredentialMember("B", gate)])
    failed = asyncio.Event()
    calls: list[str] = []

    async def run(label: str) -> str:
        calls.append(label)
        if label == "A":
            failed.set()
            raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "synthetic failure")
        return label

    async with asyncio.timeout(3), asyncio.TaskGroup() as group:
        async with gate.lease():
            task = group.create_task(pool.invoke(run))
            await failed.wait()
            assert not task.done()
        assert await task == "B"
    assert gate.in_use == 0
    assert calls == ["A", "B"]


async def test_first_reservation_and_initial_permit_share_the_callers_task_and_budget() -> None:
    clock = ManualClock()
    gate = CapacityGate(1)
    rate = RateGate(ResolvedRatePolicy(min_interval_seconds=1), monotonic=clock.monotonic)
    member = CredentialMember("A", gate, rate)
    pool = CredentialPool("web", "example", [member, CredentialMember("B")])
    other = CredentialPool("web", "other", [member])
    first = await pool.try_reserve(set())
    assert first is not None
    owner = asyncio.current_task()
    budget = SelectionBudget(2, 3)
    calls: list[str] = []

    async def run(label: str) -> str:
        calls.append(label)
        assert owner is asyncio.current_task()
        assert gate.in_use == 1
        assert consume_initial_rate_budget(rate) is budget
        assert consume_initial_rate_budget(rate) is None
        return label

    try:
        with pytest.raises(ValueError):
            await other.invoke(run, first=first, budget=budget)
        assert gate.in_use == 1
        async with asyncio.timeout(3):
            assert await pool.invoke(run, first=first, budget=budget) == "A"
        with pytest.raises(RuntimeError):
            await pool.invoke(run, first=first, budget=budget)
    finally:
        await first.release()
    assert calls == ["A"]
    assert gate.in_use == 0
    assert await select_and_release(pool, set()) == (1, "B")


async def test_each_selected_member_gets_the_same_initial_budget() -> None:
    rates = {label: RateGate(ResolvedRatePolicy(min_interval_seconds=1)) for label in "AB"}
    pool = CredentialPool(
        "web", "example", [CredentialMember(label, rate=rate) for label, rate in rates.items()]
    )
    budget = SelectionBudget(2.0)

    async def run(label: str) -> str:
        assert consume_initial_rate_budget(rates[label]) is budget
        if label == "A":
            raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "synthetic failure")
        return label

    assert await pool.invoke(run, budget=budget) == "B"


@pytest.mark.parametrize(
    "error",
    [
        InputFailure(ErrorCode.BAD_REQUEST, "synthetic input"),
        ConfigFailure(ErrorCode.CONFIG_ERROR, "synthetic config"),
        asyncio.CancelledError(),
        ArithmeticError("synthetic defect"),
    ],
)
async def test_non_provider_failure_does_not_fallback(error: BaseException) -> None:
    gate = CapacityGate(1)
    pool = CredentialPool("web", "example", [CredentialMember("A", gate), CredentialMember("B")])
    calls: list[str] = []

    async def run(label: str) -> str:
        calls.append(label)
        raise error

    with pytest.raises(type(error)) as caught:
        await pool.invoke(run)
    assert caught.value is error
    assert calls == ["A"]
    assert gate.in_use == 0


async def test_failed_member_has_no_memory_across_overlapping_or_later_calls() -> None:
    pool = CredentialPool(
        "web", "example", [CredentialMember(label) for label in "AB"], strategy="fill_first"
    )
    both_entered = asyncio.Barrier(2)

    async def invoke() -> list[str]:
        calls: list[str] = []

        async def run(label: str) -> str:
            calls.append(label)
            if label == "A":
                await both_entered.wait()
                raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "synthetic failure")
            return label

        assert await pool.invoke(run) == "B"
        return calls

    async with asyncio.timeout(3):
        first, second = await asyncio.gather(invoke(), invoke())
        assert first == second == ["A", "B"]

    async def succeed(label: str) -> str:
        return label

    assert await pool.invoke(succeed) == "A"


@pytest.mark.parametrize("wake", [5, 6])
async def test_wait_only_observes_ready_at_deadline_and_rejects_late_wake(wake: int) -> None:
    clock = ManualClock()
    calls: list[float] = []

    async def sleep(delay: float) -> None:
        calls.append(delay)
        clock.now += wake

    rate = RateGate(ResolvedRatePolicy(min_interval_seconds=5), monotonic=clock.monotonic)
    await rate.permit(timeout_seconds=0)
    gate = CapacityGate(1)
    pool = CredentialPool(
        "llm",
        "example",
        [CredentialMember("A", gate, rate), CredentialMember("B")],
        error_code=ErrorCode.LLM_STAGE_FAILED,
        monotonic=clock.monotonic,
        sleep=sleep,
    )
    budget = SelectionBudget(5)
    if wake == 5:
        await pool.wait_available({1}, budget)
        await pool.wait_available({1}, budget)
    else:
        with pytest.raises(ExecutionFailure) as caught:
            await pool.wait_available({1}, budget)
        assert caught.value.code == ErrorCode.LLM_STAGE_FAILED
        assert caught.value.reason == "rate_limit_wait_timeout"
    assert budget == SelectionBudget(0, wake)
    assert calls == [5]
    assert gate.in_use == 0
    assert rate.next_available_in_seconds() == 0
    assert await select_and_release(pool, set()) == (0, "A")


@pytest.mark.parametrize("executed", [False, True])
async def test_spurious_wakes_spend_one_budget_and_preserve_the_typed_failure(
    executed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock()
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        clock.now += 1

    async def unexpected_capacity_wait() -> None:
        pytest.fail("rate-blocked member registered a capacity waiter")

    gate = CapacityGate(1)
    monkeypatch.setattr(gate, "wait_until_available", unexpected_capacity_wait)
    rate = RateGate(ResolvedRatePolicy(min_interval_seconds=100), monotonic=clock.monotonic)
    await rate.permit(timeout_seconds=0)
    members = [CredentialMember("A")] if executed else []
    members.append(CredentialMember("B", gate, rate))
    pool = CredentialPool(
        "llm",
        "example",
        members,
        request_timeout_seconds=3,
        error_code=ErrorCode.LLM_STAGE_FAILED,
        monotonic=clock.monotonic,
        sleep=sleep,
    )
    failure = ExecutionFailure(ErrorCode.PROTOCOL_ERROR, "synthetic provider failure")
    calls: list[str] = []

    async def run(label: str) -> None:
        calls.append(label)
        clock.now += 10
        raise failure

    async with asyncio.timeout(3):
        with pytest.raises(ExecutionFailure) as caught:
            await pool.invoke(run)
    assert delays == [3, 2, 1]
    assert calls == (["A"] if executed else [])
    if executed:
        assert caught.value is failure
    else:
        assert caught.value.code == ErrorCode.LLM_STAGE_FAILED
        assert caught.value.reason == "rate_limit_wait_timeout"


@pytest.mark.parametrize("cancel", [True, False])
async def test_cancelled_or_failing_wait_joins_its_capacity_and_timer_tasks(
    cancel: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock()
    gate = CapacityGate(1)
    entered = asyncio.Event()
    alarm = asyncio.Event()
    finished: set[str] = set()
    fail = RuntimeError("synthetic wait failure")
    original_wait = gate.wait_until_available

    async def track_capacity() -> None:
        entered.set()
        try:
            await original_wait()
        finally:
            finished.add("capacity")

    async def timer(delay: float) -> None:
        assert delay == 3
        alarm.set()
        try:
            await entered.wait()
            if not cancel:
                clock.now = 2
                raise fail
            await asyncio.Event().wait()
        finally:
            finished.add("timer")

    monkeypatch.setattr(gate, "wait_until_available", track_capacity)
    pool = CredentialPool(
        "web", "example", [CredentialMember("A", gate)], monotonic=clock.monotonic, sleep=timer
    )
    budget = SelectionBudget(3)

    async def run(label: str) -> str:
        pytest.fail("operation entered while gate was full")

    async with asyncio.timeout(3), gate.lease():
        task = asyncio.create_task(pool.invoke(run, budget=budget))
        if cancel:
            await entered.wait()
            await alarm.wait()
            clock.now = 2
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(RuntimeError) as caught:
                await task
            assert caught.value is fail
        assert finished == {"capacity", "timer"}
        assert budget == SelectionBudget(1, 2)
        assert gate.in_use == 1
    assert await select_and_release(pool, set()) == (0, "A")


@pytest.mark.parametrize("failed", [True, False])
async def test_cancelled_operation_end_releases_before_fallback_or_return(
    failed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = CapacityGate(1)
    pool = CredentialPool(
        "web",
        "example",
        [CredentialMember("A", gate), CredentialMember("B")],
        strategy="fill_first",
    )
    release_started = asyncio.Event()
    original_release = gate._release
    calls: list[str] = []

    async def track_release() -> None:
        release_started.set()
        await original_release()

    monkeypatch.setattr(gate, "_release", track_release)

    async def run(label: str) -> str:
        calls.append(label)
        if failed:
            raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "synthetic failure")
        return label

    first = await pool.try_reserve(set())
    assert first is not None
    try:
        async with asyncio.timeout(3), asyncio.TaskGroup() as group:
            async with gate._condition:
                task = group.create_task(pool.invoke(run, first=first))
                await release_started.wait()
                task.cancel()
                await asyncio.sleep(0)
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        await first.release()
    assert calls == ["A"]
    assert gate.in_use == 0
    assert await select_and_release(pool, set()) == (0, "A")


@pytest.mark.parametrize("at_handoff", [False, True])
async def test_pending_cancel_stops_a_synchronous_operation_boundary(at_handoff: bool) -> None:
    gate = CapacityGate(1) if at_handoff else None
    pool = CredentialPool("web", "example", [CredentialMember("A", gate), CredentialMember("B")])
    first = await pool.try_reserve(set())
    assert first is not None
    calls: list[str] = []

    def cancel_owner() -> None:
        owner = asyncio.current_task()
        assert owner is not None
        owner.cancel()

    async def run(label: str) -> str:
        calls.append(label)
        if label == "A":
            cancel_owner()
            raise ExecutionFailure(ErrorCode.PROTOCOL_ERROR, "synthetic terminal failure")
        return label

    async def invoke() -> str:
        if at_handoff:
            cancel_owner()
        return await pool.invoke(run, first=first)

    try:
        async with asyncio.timeout(3):
            with pytest.raises(asyncio.CancelledError):
                await asyncio.create_task(invoke())
    finally:
        await first.release()
    assert calls == ([] if at_handoff else ["A"])
    assert gate is None or gate.in_use == 0
    assert await select_and_release(pool, set()) == (1, "B")


async def test_capacity_wakes_before_earliest_rate_and_wait_releases_every_loser() -> None:
    clock = ManualClock()
    capacities = [CapacityGate(1), CapacityGate(1)]
    rate = RateGate(ResolvedRatePolicy(min_interval_seconds=4), monotonic=clock.monotonic)
    await rate.permit(timeout_seconds=0)
    pool = CredentialPool(
        "web",
        "example",
        [
            CredentialMember("A", capacities[0]),
            CredentialMember("B", capacities[1]),
            CredentialMember("C", rate=rate),
        ],
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    budget = SelectionBudget(10)
    baseline = asyncio.all_tasks()
    async with asyncio.timeout(3), asyncio.TaskGroup() as group, capacities[1].lease():
        async with capacities[0].lease():
            waiting = group.create_task(pool.wait_available(set(), budget))
            while not clock.sleepers:
                await asyncio.sleep(0)
            assert list(clock.sleepers.values()) == [4]
            await clock.advance_to(2)
            assert not waiting.done()
        await waiting
        assert capacities[0].in_use == 0
        assert not (asyncio.all_tasks() - baseline)
        assert rate.next_available_in_seconds() == 2
    assert not clock.sleepers
    assert budget == SelectionBudget(8, 2)
    assert await select_and_release(pool, set()) == (0, "A")


async def test_cancelled_ungated_success_cannot_publish_its_result() -> None:
    pool = CredentialPool("web", "example", [CredentialMember(label) for label in "AB"])
    calls: list[str] = []
    published: list[str] = []

    async def run(label: str) -> str:
        calls.append(label)
        owner = asyncio.current_task()
        assert owner is not None
        owner.cancel()
        return label

    async def caller() -> None:
        published.append(await pool.invoke(run))

    async with asyncio.timeout(3):
        with pytest.raises(asyncio.CancelledError):
            await asyncio.create_task(caller())
    assert published == []
    assert calls == ["A"]
    assert await select_and_release(pool, set()) == (1, "B")
