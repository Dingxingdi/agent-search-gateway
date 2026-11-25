import asyncio

from agent_search_gateway.concurrency import CapacityGate, ProviderQuotaManager, make_capacity_gate
from agent_search_gateway.config import ResolvedRatePolicy
from agent_search_gateway.credential_pool import CredentialMember, CredentialPool
from agent_search_gateway.rate_limit import RateGate
from agent_search_gateway.request_ids import bind_request_id
from tests.support.logging import structured_test_logger


def test_omitted_capacity_creates_no_waiting_gate() -> None:
    quotas = ProviderQuotaManager(agent_limits={"unlimited": None, "limited": 2})
    assert quotas.get_agent("unlimited") is None
    limited = quotas.get_agent("limited")
    assert limited is not None and limited.limit == 2


async def test_rate_namespaces_isolate_same_named_providers_and_capacity() -> None:
    policy = ResolvedRatePolicy(min_interval_seconds=10)
    members = {
        namespace: CredentialMember("shared", CapacityGate(1), RateGate(policy))
        for namespace in ("web", "academic", "oa", "llm")
    }
    pools = {
        namespace: CredentialPool(namespace, "shared", [member])
        for namespace, member in members.items()
    }
    web = members["web"].rate
    assert web is not None
    await web.permit(timeout_seconds=0)
    assert await pools["web"].try_reserve(set()) is None
    for namespace in ("academic", "oa", "llm"):
        reservation = await pools[namespace].try_reserve(set())
        assert reservation is not None and reservation.member is members[namespace]
        await reservation.release()


async def test_web_search_and_fetch_share_capacity_while_llm_capacity_is_separate() -> None:
    web_gate = CapacityGate(1)
    llm_gate = CapacityGate(1)

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
    gate = CapacityGate(1, provider="shared", quota_kind="web", logger=logger)
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
    academic_gate = CapacityGate(2, provider="shared", quota_kind="academic", logger=logger)
    web_gate, llm_gate = CapacityGate(1), CapacityGate(1)
    assert academic_gate.limit == 2
    async with academic_gate.lease():
        assert academic_gate.in_use == 1
        assert web_gate.in_use == 0
        assert llm_gate.in_use == 0
    assert "quota_kind=academic" in stream.getvalue()


def test_omitted_credential_limit_has_no_member_gate() -> None:
    member = CredentialMember("shared", make_capacity_gate(None))
    assert member.capacity is None


async def test_agent_quota_is_independent_of_same_named_llm_quota() -> None:
    quotas = ProviderQuotaManager(agent_limits={"same": 1})
    gate = quotas.get_agent("same")
    assert gate is not None
    member = CredentialMember("same", CapacityGate(1))
    pool = CredentialPool("llm", "same", [member])
    async with gate.lease(), asyncio.timeout(1):
        reservation = await pool.try_reserve(set())
        assert reservation is not None
        try:
            assert gate.in_use == 1
            assert member.capacity is not None and member.capacity.in_use == 1
        finally:
            await reservation.release()
    assert gate.in_use == 0


async def test_agent_quota_waiter_cancellation_and_release() -> None:
    quotas = ProviderQuotaManager(agent_limits={"a": 1, "b": 2})
    gate = quotas.get_agent("a")
    assert gate is not None
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
    other_gate = quotas.get_agent("b")
    assert other_gate is not None
    async with other_gate.lease(), other_gate.lease():
        assert other_gate.in_use == 2
