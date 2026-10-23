import asyncio

from agent_search_gateway.concurrency import CapacityGate, ProviderQuotaManager
from agent_search_gateway.request_ids import bind_request_id
from tests.support.logging import structured_test_logger


async def test_web_search_and_fetch_share_capacity_while_llm_capacity_is_separate() -> None:
    quotas = ProviderQuotaManager(web_limits={"shared": 1}, llm_limits={"shared": 1})
    web_gate = quotas.get_web("shared")
    llm_gate = quotas.get_llm("shared")

    first_web_entered = asyncio.Event()
    release_first_web = asyncio.Event()
    second_web_entered = asyncio.Event()
    llm_entered = asyncio.Event()

    async def hold_web() -> None:
        async with web_gate.lease():
            first_web_entered.set()
            await release_first_web.wait()

    async def wait_for_web() -> None:
        async with web_gate.lease():
            second_web_entered.set()

    async def use_llm() -> None:
        async with llm_gate.lease():
            llm_entered.set()

    first_task = asyncio.create_task(hold_web())
    await first_web_entered.wait()
    second_task = asyncio.create_task(wait_for_web())
    llm_task = asyncio.create_task(use_llm())
    await llm_entered.wait()
    await asyncio.sleep(0)

    assert not second_web_entered.is_set()
    assert web_gate.in_use == 1
    assert llm_gate.in_use == 0

    release_first_web.set()
    await second_web_entered.wait()
    await asyncio.gather(first_task, second_task, llm_task)

    assert web_gate.in_use == 0
    assert llm_gate.in_use == 0
    assert web_gate.max_observed_in_use == 1
    assert llm_gate.max_observed_in_use == 1


async def test_quota_debug_events_cover_acquire_wait_try_release_and_cancellation() -> None:
    logger, stream = structured_test_logger("tests.quota.events")
    quotas = ProviderQuotaManager(
        web_limits={"shared": 1},
        llm_limits={},
        logger=logger,
    )
    gate = quotas.get_web("shared")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def leader() -> None:
        with bind_request_id("11111111"):
            async with gate.lease():
                entered.set()
                await release.wait()

    async def waiter() -> None:
        with bind_request_id("22222222"):
            async with gate.lease():
                return None

    first = asyncio.create_task(leader())
    await entered.wait()
    with bind_request_id("33333333"):
        assert await gate.try_lease() is None
    second = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    assert gate.in_use == 1
    release.set()
    await asyncio.gather(first, second)

    logged = stream.getvalue().splitlines()
    assert any(
        "request=11111111" in line
        and "event=quota_acquired" in line
        and "provider=shared" in line
        and "quota_kind=web" in line
        and "in_use=1" in line
        and "limit=1" in line
        for line in logged
    )
    assert any("request=33333333" in line and "event=quota_waiting" in line for line in logged)
    assert any("request=22222222" in line and "event=quota_waiting" in line for line in logged)
    assert any(
        "request=22222222" in line and "event=quota_acquired" in line and "waited_ms=" in line
        for line in logged
    )
    assert sum("event=quota_released" in line for line in logged) == 2
    assert gate.in_use == 0
    assert gate.max_observed_in_use == 1

    cancellation_entered = asyncio.Event()

    async def cancelled_user() -> None:
        with bind_request_id("44444444"):
            async with gate.lease():
                cancellation_entered.set()
                await asyncio.Event().wait()

    cancelled = asyncio.create_task(cancelled_user())
    await cancellation_entered.wait()
    cancelled.cancel()
    await asyncio.gather(cancelled, return_exceptions=True)
    assert gate.in_use == 0
    assert "request=44444444" in stream.getvalue()

    direct = CapacityGate(1)
    async with direct.lease():
        assert direct.in_use == 1
    assert direct.in_use == 0


async def test_academic_capacity_is_separate_and_logs_academic_quota_kind() -> None:
    logger, stream = structured_test_logger("tests.quota.academic")
    quotas = ProviderQuotaManager(
        web_limits={"shared": 1},
        llm_limits={"shared": 1},
        academic_limits={"shared": 2},
        logger=logger,
    )
    academic_gate = quotas.get_academic("shared")
    assert academic_gate.limit == 2
    async with academic_gate.lease():
        assert academic_gate.in_use == 1
        assert quotas.get_web("shared").in_use == 0
        assert quotas.get_llm("shared").in_use == 0
    assert "quota_kind=academic" in stream.getvalue()


def test_omitted_academic_limits_preserve_existing_constructor_behavior() -> None:
    quotas = ProviderQuotaManager(web_limits={}, llm_limits={})
    try:
        quotas.get_academic("missing")
    except KeyError:
        pass
    else:
        raise AssertionError("missing academic quota must not be synthesized")


async def test_agent_quota_is_independent_of_same_named_llm_quota() -> None:
    assert hasattr(ProviderQuotaManager, "get_agent"), "Agent requires its own quota namespace"
    quotas = ProviderQuotaManager(web_limits={}, llm_limits={"same": 1}, agent_limits={"same": 1})
    gate = quotas.get_agent("same")
    async with gate.lease(), asyncio.timeout(1), quotas.get_llm("same").lease():
        assert gate.in_use == 1
        assert quotas.get_llm("same").in_use == 1
    assert gate.in_use == 0


async def test_agent_quota_waiter_cancellation_and_release() -> None:
    quotas = ProviderQuotaManager(web_limits={}, llm_limits={}, agent_limits={"a": 1, "b": 2})
    gate = quotas.get_agent("a")
    attempted, entered = asyncio.Event(), asyncio.Event()

    async def waiter() -> None:
        attempted.set()
        async with gate.lease():
            entered.set()

    async with gate.lease():
        waiting = asyncio.create_task(waiter())
        await attempted.wait()
        assert not entered.is_set()
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        assert gate.in_use == 1
    async with asyncio.timeout(1):
        await waiter()
    assert entered.is_set()
    assert gate.in_use == 0
    async with quotas.get_agent("b").lease(), quotas.get_agent("b").lease():
        assert quotas.get_agent("b").in_use == 2
