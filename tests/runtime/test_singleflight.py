import asyncio

import pytest

from agent_search_gateway.concurrency import PerKeyLockPool, SingleflightGroup
from agent_search_gateway.request_ids import bind_request_id, current_request_id


async def test_singleflight_shares_same_key_result_and_exception_but_allows_different_keys() -> (
    None
):
    group: SingleflightGroup[str, str] = SingleflightGroup()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def shared_factory() -> str:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "shared"

    first = asyncio.create_task(group.do("same", shared_factory))
    await entered.wait()
    second = asyncio.create_task(group.do("same", shared_factory))
    await asyncio.sleep(0)
    release.set()
    assert tuple(await asyncio.gather(first, second)) == ("shared", "shared")
    assert calls == 1

    failure_entered = asyncio.Event()
    failure_release = asyncio.Event()
    failure_calls = 0

    async def failing_factory() -> str:
        nonlocal failure_calls
        failure_calls += 1
        failure_entered.set()
        await failure_release.wait()
        raise RuntimeError("shared failure")

    failing_first = asyncio.create_task(group.do("failure", failing_factory))
    await failure_entered.wait()
    failing_second = asyncio.create_task(group.do("failure", failing_factory))
    await asyncio.sleep(0)
    failure_release.set()
    results = await asyncio.gather(failing_first, failing_second, return_exceptions=True)
    assert all(isinstance(result, RuntimeError) for result in results)
    assert failure_calls == 1

    distinct_entered = {"a": asyncio.Event(), "b": asyncio.Event()}
    distinct_release = asyncio.Event()

    async def distinct_factory(key: str) -> str:
        distinct_entered[key].set()
        await distinct_release.wait()
        return key

    task_a = asyncio.create_task(group.do("a", lambda: distinct_factory("a")))
    task_b = asyncio.create_task(group.do("b", lambda: distinct_factory("b")))
    await asyncio.gather(*(event.wait() for event in distinct_entered.values()))
    distinct_release.set()
    assert tuple(await asyncio.gather(task_a, task_b)) == ("a", "b")

    lock_pool: PerKeyLockPool[str] = PerKeyLockPool()
    same_active = 0
    same_max_active = 0
    first_lock_entered = asyncio.Event()
    release_first_lock = asyncio.Event()

    async def locked(key: str, *, hold: bool = False) -> None:
        nonlocal same_active, same_max_active
        async with lock_pool.acquire(key):
            same_active += 1
            same_max_active = max(same_max_active, same_active)
            if hold:
                first_lock_entered.set()
                await release_first_lock.wait()
            same_active -= 1

    lock_first = asyncio.create_task(locked("same-lock", hold=True))
    await first_lock_entered.wait()
    lock_second = asyncio.create_task(locked("same-lock"))
    await asyncio.sleep(0)
    assert same_max_active == 1
    release_first_lock.set()
    await asyncio.gather(lock_first, lock_second)
    assert same_max_active == 1

    different_entered = asyncio.Event()
    release_different = asyncio.Event()

    async def different_lock(key: str) -> None:
        async with lock_pool.acquire(key):
            if key == "one":
                different_entered.set()
                await release_different.wait()
            else:
                release_different.set()

    one = asyncio.create_task(different_lock("one"))
    await different_entered.wait()
    two = asyncio.create_task(different_lock("two"))
    await asyncio.gather(one, two)


async def test_singleflight_role_callbacks_run_in_each_callers_request_context() -> None:
    group: SingleflightGroup[str, str] = SingleflightGroup()
    entered = asyncio.Event()
    release = asyncio.Event()
    roles: list[tuple[str, str | None]] = []
    factory_contexts: list[str | None] = []
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        factory_contexts.append(current_request_id())
        entered.set()
        await release.wait()
        return "shared"

    async def invoke(request_id: str) -> str:
        with bind_request_id(request_id):
            return await group.do(
                "same",
                factory,
                on_leader=lambda: roles.append(("leader", current_request_id())),
                on_follower=lambda: roles.append(("follower", current_request_id())),
            )

    first = asyncio.create_task(invoke("11111111"))
    await entered.wait()
    second = asyncio.create_task(invoke("22222222"))
    await asyncio.sleep(0)
    release.set()

    assert tuple(await asyncio.gather(first, second)) == ("shared", "shared")
    assert calls == 1
    assert factory_contexts == ["11111111"]
    assert roles == [("leader", "11111111"), ("follower", "22222222")]
    assert current_request_id() is None


async def test_singleflight_role_callback_failures_do_not_alter_shared_execution() -> None:
    group: SingleflightGroup[str, str] = SingleflightGroup()
    entered = asyncio.Event()
    release = asyncio.Event()
    leader_called = asyncio.Event()
    follower_called = asyncio.Event()
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "shared"

    def failing_leader() -> None:
        leader_called.set()
        raise RuntimeError("leader callback failure")

    def failing_follower() -> None:
        follower_called.set()
        raise RuntimeError("follower callback failure")

    first = asyncio.create_task(group.do("key", factory, on_leader=failing_leader))
    await leader_called.wait()
    await entered.wait()
    second = asyncio.create_task(group.do("key", factory, on_follower=failing_follower))
    await follower_called.wait()
    release.set()

    assert tuple(await asyncio.gather(first, second)) == ("shared", "shared")
    assert calls == 1
    assert await group.do("key", lambda: _return_value("after")) == "after"


@pytest.mark.parametrize("cancel_leader", [False, True], ids=["follower", "leader"])
async def test_singleflight_cancellation_propagation(cancel_leader: bool) -> None:
    group: SingleflightGroup[str, str] = SingleflightGroup()
    entered = asyncio.Event()
    release = asyncio.Event()
    joined = [asyncio.Event(), asyncio.Event()]
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "shared"

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        leader = tasks.create_task(group.do("key", factory))
        await entered.wait()
        followers = [
            tasks.create_task(group.do("key", factory, on_follower=event.set)) for event in joined
        ]
        await asyncio.gather(*(event.wait() for event in joined))
        if cancel_leader:
            leader.cancel()
            for caller in [leader, *followers]:
                with pytest.raises(asyncio.CancelledError):
                    await caller
        else:
            followers[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await followers[0]
            release.set()
            assert (await leader, await followers[1]) == ("shared", "shared")

    assert calls == 1
    assert await group.do("key", lambda: _return_value("after")) == "after"


async def _return_value(value: str) -> str:
    return value
