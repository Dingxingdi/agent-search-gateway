import asyncio
import json
from unittest.mock import Mock, create_autospec

import httpx
import pytest

from agent_search_gateway.concurrency import CapacityGate
from agent_search_gateway.config import ResolvedRatePolicy
from agent_search_gateway.credential_pool import CredentialMember, CredentialPool
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.input_limits import PreparedInput
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation, OAResolution, RetryPolicy
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import (
    AcademicSearchProvider,
    KeywordSearchHit,
    KeywordSearchProvider,
    LLMClient,
    OAResolver,
    PaperSearchHit,
    PreparedLLMClient,
    URLFetchCandidate,
    WebAdapter,
)
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.llm.openai_chat import OpenAIChatCompletionsClient
from agent_search_gateway.providers.pooled import (
    PooledAcademicProvider,
    PooledLLMClient,
    PooledOAResolver,
    PooledWebProvider,
)
from agent_search_gateway.providers.web.langsearch import LangSearchAdapter
from agent_search_gateway.rate_limit import RateGate, SelectionBudget, consume_initial_rate_budget
from agent_search_gateway.url_normalization import NormalizedURL, normalize_url


@pytest.mark.parametrize("limit", ["capacity", "rate"])
async def test_web_search_and_fetch_share_one_pool(limit: str) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    rate = RateGate(ResolvedRatePolicy((), 60.0)) if limit == "rate" else None

    class Web:
        name = "web"

        def __init__(self, account: str) -> None:
            self.account = account

        async def search(self, query: str) -> list[KeywordSearchHit]:
            if rate is not None:
                await rate.permit(timeout_seconds=1)
            entered.set()
            await release.wait()
            return []

        async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
            return URLFetchCandidate(self.account)

    first, second = Web("A"), Web("B")
    pool = CredentialPool[WebAdapter](
        "web",
        "web",
        [
            CredentialMember(first, CapacityGate(1) if limit == "capacity" else None, rate),
            CredentialMember(second),
        ],
        strategy="fill_first",
    )
    provider = PooledWebProvider(pool)
    async with asyncio.timeout(2), asyncio.TaskGroup() as group:
        group.create_task(provider.search("query"))
        await entered.wait()
        try:
            result = await provider.fetch(normalize_url("https://example.test/"))
        finally:
            release.set()
    assert result.raw_content == "B"


async def test_web_fetch_consumes_first_reservation_and_budget() -> None:
    budget = SelectionBudget(0.5)
    rate = RateGate(ResolvedRatePolicy((), 1.0))

    class Fetcher:
        name = "web"

        def __init__(self, account: str) -> None:
            self.account = account

        async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
            assert consume_initial_rate_budget(rate) is budget
            return URLFetchCandidate(self.account)

    gate = CapacityGate(1)
    pool = CredentialPool[WebAdapter](
        "web", "web", [CredentialMember(Fetcher("A"), gate, rate), CredentialMember(Fetcher("B"))]
    )
    provider = PooledWebProvider(pool)
    await provider.wait_fetch_available(budget)
    first = await provider.try_reserve_fetch()
    assert first is not None and gate.in_use == 1
    try:
        async with asyncio.timeout(2):
            result = await provider.fetch_reserved(
                normalize_url("https://example.test/"), first, budget
            )
        assert result.raw_content == "A"
        assert gate.in_use == 0
    finally:
        await first.release()


@pytest.mark.parametrize(
    "kind, contract, operation, empty, value",
    [
        ("web", KeywordSearchProvider, "search", [], [KeywordSearchHit("https://example.test/")]),
        ("academic", AcademicSearchProvider, "search", [], [PaperSearchHit("academic", "id", "t")]),
        ("oa", OAResolver, "resolve", None, OAResolution(None, None, True)),
        ("llm", OpenAIChatCompletionsClient, "complete_json", {}, {"ok": True}),
    ],
)
@pytest.mark.parametrize("outcome", ["empty", "value", "fallback", "failure"])
async def test_typed_facades_preserve_success_and_failure_contracts(
    kind: str, contract: type[object], operation: str, empty: object, value: object, outcome: str
) -> None:
    code = ErrorCode.LLM_STAGE_FAILED if kind == "llm" else ErrorCode.ALL_PROVIDERS_FAILED
    failures = [ExecutionFailure(code, message) for message in ("first", "last")]
    physical = [create_autospec(contract, instance=True) for _ in range(2)]
    pool = CredentialPool(
        kind, "logical", [CredentialMember(member) for member in physical], error_code=code
    )
    prepared = PreparedInput(b"{}", 2, False)
    prepare = Mock(return_value=prepared)
    args = (LLMInvocation("logical", "model"), ()) if kind == "llm" else ("query",)
    member_operation = operation + "_prepared" if kind == "llm" else operation
    calls = [getattr(member, member_operation) for member in physical]
    for index, (member, call) in enumerate(zip(physical, calls, strict=True)):
        member.name = "logical"
        call.return_value = empty if outcome == "empty" else value
        if outcome == "failure" or (outcome == "fallback" and index == 0):
            call.side_effect = failures[index]
    if kind == "llm":
        provider = PooledLLMClient(pool, prepare)
        assert isinstance(provider, LLMClient)
    else:
        provider = {
            "web": PooledWebProvider,
            "academic": PooledAcademicProvider,
            "oa": PooledOAResolver,
        }[kind](pool)
    assert provider.name == "logical"
    if outcome == "failure":
        with pytest.raises(ExecutionFailure) as caught:
            await getattr(provider, operation)(*args)
        assert caught.value is failures[1]
    else:
        assert await getattr(provider, operation)(*args) == (empty if outcome == "empty" else value)
    expected_args = (*args, prepared) if kind == "llm" else args
    calls[0].assert_awaited_once_with(*expected_args)
    if outcome in ("failure", "fallback"):
        calls[1].assert_awaited_once_with(*expected_args)
    else:
        calls[1].assert_not_awaited()
    if kind == "llm":
        prepare.assert_called_once_with(*args)


def _langsearch_pages(*items: object) -> dict[str, object]:
    return {"code": 200, "data": {"webPages": {"value": items}}}


@pytest.mark.parametrize(
    "first_status, first_payload, expected_hosts, expected_hits",
    [
        pytest.param(200, _langsearch_pages(), ["a.test"], [], id="empty"),
        pytest.param(
            200,
            _langsearch_pages({}, {"url": "https://first.test/page", "name": "First"}),
            ["a.test"],
            [KeywordSearchHit("https://first.test/page", title="First")],
            id="mixed",
        ),
        pytest.param(200, _langsearch_pages({}, None), ["a.test"], [], id="all-rejected"),
        pytest.param(
            503,
            {},
            ["a.test", "a.test", "b.test"],
            [KeywordSearchHit("https://second.test/page")],
            id="http-retry-exhausted",
        ),
        pytest.param(
            200,
            {"code": 401},
            ["a.test", "b.test"],
            [KeywordSearchHit("https://second.test/page")],
            id="failed-business-envelope",
        ),
    ],
)
async def test_langsearch_pool_falls_back_only_after_member_failure(
    first_status: int,
    first_payload: dict[str, object],
    expected_hosts: list[str],
    expected_hits: list[KeywordSearchHit],
) -> None:
    hosts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "a.test":
            return httpx.Response(first_status, json=first_payload)
        assert request.url.host == "b.test"
        return httpx.Response(200, json=_langsearch_pages({"url": "https://second.test/page"}))

    retry = RetryPolicy(2, 0.01, 0.02, 1.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        members: list[CredentialMember[WebAdapter]] = [
            CredentialMember(
                LangSearchAdapter(
                    name="langsearch",
                    api_url=f"https://{host}",
                    secret=SecretValue("fixture"),
                    http_executor=HttpJsonExecutor(http, retry, provider_name="langsearch"),
                )
            )
            for host in ("a.test", "b.test")
        ]
        pool = CredentialPool[WebAdapter]("web", "langsearch", members, strategy="fill_first")
        assert await PooledWebProvider(pool).search("fixture query") == expected_hits
    assert hosts == expected_hosts


def _physical_client(
    http: httpx.AsyncClient, host: str, *, request_body_bytes: int = 512
) -> OpenAIChatCompletionsClient:
    policy = RetryPolicy(1, 0.01, 0.02, 1.0)
    return OpenAIChatCompletionsClient(
        name="logical",
        api_url=f"https://{host}.example.test",
        secret=SecretValue("fixture"),
        executor=HttpJsonExecutor(http, policy, provider_name="logical"),
        retry_policy=policy,
        request_body_bytes=request_body_bytes,
    )


@pytest.mark.parametrize(
    "invalid, reason",
    [
        (LLMInvocation("other", "model"), "does not match"),
        (LLMInvocation("logical", "model", {"messages": []}), "cannot override"),
        (LLMInvocation("logical", "m" * 1024), "input_budget_unfit"),
    ],
)
async def test_llm_prepare_failure_never_selects_credential(
    invalid: LLMInvocation, reason: str
) -> None:
    hosts = []
    gates = [CapacityGate(1), CapacityGate(1)]

    def respond(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        assert gates[0].in_use == 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        clients = [_physical_client(http, host) for host in ("a", "b")]
        pool = CredentialPool[PreparedLLMClient](
            "llm",
            "logical",
            [CredentialMember(c, g) for c, g in zip(clients, gates, strict=True)],
            error_code=ErrorCode.LLM_STAGE_FAILED,
            request_timeout_seconds=1.0,
        )
        provider = PooledLLMClient(pool, clients[0].prepare_request)
        messages = [{"role": "user", "content": "short"}]
        with pytest.raises(ExecutionFailure, match=reason) as caught:
            await provider.complete_text(invalid, messages)
        assert caught.value.code is ErrorCode.LLM_STAGE_FAILED
        assert hosts == []
        assert [gate.max_observed_in_use for gate in gates] == [0, 0]
        async with asyncio.timeout(2):
            value = await provider.complete_text(LLMInvocation("logical", "model"), messages)
        assert value == "ok"
        assert hosts == ["a.example.test"]
        assert gates[0].in_use == 0


async def test_all_llm_stages_share_alias_pool_and_keep_model_selection() -> None:
    calls = []
    search_started, finish_search = asyncio.Event(), asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append((model, request.url.host))
        if model == "search":
            search_started.set()
            await finish_search.wait()
        content = '{"ok": true}' if model in {"judge", "safety"} else model
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    def invocation(model: str) -> LLMInvocation:
        return LLMInvocation("logical", model)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        clients = [_physical_client(http, host, request_body_bytes=65536) for host in ("a", "b")]
        pool = CredentialPool[PreparedLLMClient](
            "llm",
            "logical",
            [CredentialMember(c, CapacityGate(1)) for c in clients],
            strategy="fill_first",
            error_code=ErrorCode.LLM_STAGE_FAILED,
            request_timeout_seconds=1,
        )
        provider = PooledLLMClient(pool, clients[0].prepare_request)
        stages = LLMStages(
            {"logical": provider},
            judge=invocation("judge"),
            safety=invocation("safety"),
            content_clean=invocation("clean"),
            focus_summary=invocation("focus"),
        )
        async with asyncio.timeout(2), asyncio.TaskGroup() as group:
            search = group.create_task(stages.llm_search_markdown(invocation("search"), "query"))
            await search_started.wait()
            try:
                assert (await stages.judge("candidate")).ok
                assert await stages.content_clean("candidate") == "clean"
                assert (await stages.safety("clean")).ok
                assert await stages.focus_summary("clean", "question") == "focus"
            finally:
                finish_search.set()
        assert search.result() == "search"
    assert calls == [
        ("search", "a.example.test"),
        ("judge", "b.example.test"),
        ("clean", "b.example.test"),
        ("safety", "b.example.test"),
        ("focus", "b.example.test"),
    ]
