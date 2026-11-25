import asyncio
import json
from collections.abc import Callable
from functools import partial
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.config import resolve_config
from agent_search_gateway.credential_pool import CredentialPool
from agent_search_gateway.errors import ConfigFailure
from agent_search_gateway.llm.prompts import (
    content_clean_messages,
    focus_summary_messages,
    judge_messages,
    llm_paper_search_messages,
    llm_search_messages,
    safety_messages,
)
from agent_search_gateway.models import LLMInvocation
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.academic.defaults import (
    build_default_academic_registry,
    build_default_oa_resolver_registry,
)
from agent_search_gateway.providers.contracts import ChatMessage, ProviderCapabilities
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.registry import WebProviderRegistration
from agent_search_gateway.providers.web.brightdata import BrightDataAdapter
from agent_search_gateway.providers.web.jina import JinaReaderAdapter
from agent_search_gateway.providers.web.parallel import ParallelAdapter
from agent_search_gateway.providers.web.yuanbao import YuanbaoAdapter
from agent_search_gateway.providers.web.zhihu import ZhihuAdapter
from agent_search_gateway.rate_limit import make_rate_gate
from agent_search_gateway.runtime import Runtime
from agent_search_gateway.url_normalization import normalize_url
from tests.runtime.conftest import configured_capacity, only_member
from tests.support.agent_cli import write_fake_cli
from tests.support.agent_search import web_markdown
from tests.support.controlled import ManualClock
from tests.support.logging import structured_test_logger


class _CountingAsyncClient(httpx.AsyncClient):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(
            transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request))
        )
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        await super().aclose()


def _config() -> dict[str, object]:
    return {
        "web_providers": {
            "tavily": {
                "enable_search": True,
                "enable_fetch": True,
                "credentials": [
                    {
                        "api_key_env": "ENV_A",
                        "api_url": "https://tavily.example.test",
                        "max_concurrency": 3,
                    }
                ],
            },
            "brave": {
                "enable_search": True,
                "enable_fetch": False,
                "credentials": [
                    {
                        "api_key_env": "ENV_B",
                        "api_url": "https://brave.example.test",
                        "max_concurrency": 3,
                    }
                ],
            },
            "tinyfish": {
                "enable_search": False,
                "enable_fetch": True,
                "credentials": [
                    {
                        "api_key_env": "ENV_C",
                        "search_api_url": "https://search.tiny.example.test",
                        "fetch_api_url": "https://fetch.tiny.example.test",
                        "max_concurrency": 4,
                    }
                ],
            },
            "firecrawl": {"enable_search": False, "enable_fetch": False},
            "parallel": {
                "enable_search": True,
                "enable_fetch": True,
                "mode": "turbo",
                "search_fetch_policy": {"max_age_seconds": 3600},
                "extract_fetch_policy": {
                    "max_age_seconds": 600,
                    "timeout_seconds": 30,
                    "disable_cache_fallback": True,
                },
                "credentials": [
                    {
                        "api_key_env": "ENV_F",
                        "api_url": "https://parallel.example.test",
                        "max_concurrency": 6,
                    }
                ],
            },
        },
        "llm_providers": {
            "primary": {
                "protocol": "openai",
                "api_endpoint": "chat_completions",
                "credentials": [
                    {
                        "api_url": "https://llm-primary.example.test",
                        "api_key_env": "ENV_D",
                        "max_concurrency": 2,
                    }
                ],
            },
            "secondary": {
                "protocol": "openai",
                "api_endpoint": "chat_completions",
                "credentials": [
                    {
                        "api_url": "https://llm-secondary.example.test",
                        "api_key_env": "ENV_E",
                        "max_concurrency": 5,
                    }
                ],
            },
        },
        "global_default_llm": {"provider": "primary", "model": "global"},
        "search_llm": {"providers": [{"provider": "secondary", "model": "search"}]},
        "fetch_llm": {
            "judge": {},
            "safety": {},
            "content_clean": {},
            "focus_summary": {},
        },
        "retry": {"max_attempts": 2},
    }


def _environment() -> dict[str, str]:
    return {
        "ENV_A": "RUNTIME_CREDENTIAL_SENTINEL",
        "ENV_B": "x",
        "ENV_C": "x",
        "ENV_D": "x",
        "ENV_E": "x",
        "ENV_F": "PARALLEL_CREDENTIAL_SENTINEL",
    }


@pytest.mark.parametrize(
    "build_messages",
    [
        content_clean_messages,
        judge_messages,
        safety_messages,
        partial(focus_summary_messages, focus="完整🙂 focus"),
        llm_search_messages,
        llm_paper_search_messages,
    ],
)
async def test_runtime_llm_caps_each_stage_request(
    tmp_path: Path, build_messages: Callable[[str], tuple[ChatMessage, ...]]
) -> None:
    cap = 1400
    raw = _config()
    raw["input_limits"] = {"llm_request_body_bytes": cap}
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    registry = build_default_registry()
    runtime = await Runtime.build(
        resolve_config(raw, registry, _environment()),
        RuntimePaths.from_home(tmp_path),
        registry=registry,
        http_client_factory=lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    body = "HEAD" + "前🙂" * 2000 + "OMITTED" + "后" * 2000 + "TAIL"
    messages = build_messages(body)
    instruction, separator, _ = messages[1]["content"].partition(body)
    assert separator
    try:
        for name, client in runtime.llm_clients.items():
            await client.complete_text(LLMInvocation(name, "model", {"temperature": 0.2}), messages)
    finally:
        await runtime.aclose()

    assert len(requests) == 2
    for request in requests:
        sent_bytes = len(request.content)
        assert sent_bytes <= cap
        payload = json.loads(request.content)
        assert set(payload) == {"model", "messages", "temperature"}
        system, user = payload["messages"]
        assert system == messages[0]
        assert set(user) == {"role", "content"} and user["role"] == "user"
        assert user["content"].startswith(instruction + "HEAD")
        assert user["content"].endswith("TAIL")
        assert "[... truncated ...]" in user["content"]
        assert "OMITTED" not in user["content"]


async def test_runtime_search_and_fetch_share_one_rate_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock()
    monkeypatch.setattr(
        "agent_search_gateway.runtime.CredentialPool",
        partial(CredentialPool, monotonic=clock.monotonic, sleep=clock.sleep),
    )
    monkeypatch.setattr(
        "agent_search_gateway.runtime.make_rate_gate",
        partial(make_rate_gate, monotonic=clock.monotonic, sleep=clock.sleep),
    )
    raw = _config()
    web = raw["web_providers"]
    assert isinstance(web, dict)
    web["tavily"]["credentials"][0]["rate_limits"] = [{"requests": 1, "period_seconds": 1}]
    target = normalize_url("https://example.test/paper")
    sends: list[tuple[str, float]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        sends.append((path, clock.now))
        if path == "/extract":
            return httpx.Response(
                200, json={"results": [{"url": str(target), "raw_content": "ok"}]}
            )
        return httpx.Response(200, json={"results": [], "web": {"results": []}})

    registry = build_default_registry()
    runtime = await Runtime.build(
        resolve_config(raw, registry, _environment()),
        RuntimePaths.from_home(tmp_path),
        registry=registry,
        http_client_factory=lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    try:
        tavily, brave, _ = runtime.web_search_providers
        assert await tavily.search("query") == []
        fetch = asyncio.create_task(runtime.web_fetch_providers[0].fetch(target))
        try:
            await asyncio.sleep(0)
            assert await brave.search("query") == []
            assert sends == [("/search", 0), ("/res/v1/web/search", 0)]
            await clock.advance_to(1)
            assert (await asyncio.wait_for(fetch, 1)).raw_content == "ok"
            assert sends[-1] == ("/extract", 1)
        finally:
            fetch.cancel()
            await asyncio.gather(fetch, return_exceptions=True)
    finally:
        await runtime.aclose()


async def test_runtime_exhausted_web_rate_allows_same_named_llm_and_agent(tmp_path: Path) -> None:
    raw = _config()
    web = raw["web_providers"]
    llm = raw["llm_providers"]
    assert isinstance(web, dict) and isinstance(llm, dict)
    web["tavily"]["credentials"][0]["min_interval_seconds"] = 3600
    llm["tavily"] = llm.pop("primary")
    raw["global_default_llm"] = {"provider": "tavily", "model": "global"}
    command = write_fake_cli(
        tmp_path,
        f'print(json.dumps({{"status": "SUCCESS", "response": {web_markdown()!r}}}))',
    )
    raw["agent_providers"] = {"tavily": {"harness": "antigravity", "command": str(command)}}
    raw["search_agent"] = {"providers": [{"provider": "tavily"}]}
    registry = build_default_registry()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": []})
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    runtime = await Runtime.build(
        resolve_config(raw, registry, _environment()),
        RuntimePaths.from_home(tmp_path),
        registry=registry,
        http_client_factory=lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    try:
        await runtime.web_search_providers[0].search("query")
        async with asyncio.timeout(3):
            assert (
                await runtime.llm_clients["tavily"].complete_text(LLMInvocation("tavily", "m"), [])
                == "ok"
            )
            result = await runtime.search_orchestrator.agent_search(
                "q", request_id="11111111", provider="tavily"
            )
            assert Path(result).is_file()
        gate = only_member(runtime.web_search_providers[0]).rate
        assert gate is not None and gate.next_available_in_seconds() > 0
    finally:
        await runtime.aclose()


async def test_runtime_assembly_builds_enabled_adapters_with_shared_quotas_and_closes_clients(
    tmp_path: Path,
) -> None:
    registry = build_default_registry()
    assert [
        (item.name, item.capabilities.search, item.capabilities.fetch)
        for item in registry.list_in_registration_order()
    ] == [
        ("tavily", True, True),
        ("firecrawl", True, True),
        ("exa", True, True),
        ("linkup", True, True),
        ("brave", True, False),
        ("anysearch", True, False),
        ("tinyfish", True, True),
        ("parallel", True, True),
        ("brightdata", True, True),
        ("scrape_do", True, True),
        ("zenrows", False, True),
        ("decodo", True, True),
        ("scrapingdog", True, True),
        ("scrapegraphai", True, True),
        ("scraperapi", True, True),
        ("scrapingant", False, True),
        ("serpapi", True, False),
        ("jina", False, True),
        ("yuanbao", True, False),
        ("zhihu", True, False),
        ("doubao", True, True),
        ("youcom", True, True),
        ("keenable", True, True),
        ("open_websearch", True, True),
        ("baiduqianfan", True, False),
    ]
    parallel_registration = registry.get("parallel")
    assert parallel_registration is not None
    assert parallel_registration.allowed_config_keys == frozenset(
        {"api_url", "mode", "search_fetch_policy", "extract_fetch_policy"}
    )
    environment = _environment()
    resolved = resolve_config(_config(), registry, environment)
    clients: list[_CountingAsyncClient] = []

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        client = _CountingAsyncClient()
        clients.append(client)
        return client

    _logger, stream = structured_test_logger("agent_search_gateway.runtime")
    runtime = await Runtime.build(
        resolved,
        RuntimePaths.from_home(tmp_path),
        registry=registry,
        http_client_factory=client_factory,
    )
    assert [provider.name for provider in runtime.web_search_providers] == [
        "tavily",
        "brave",
        "parallel",
    ]
    assert [provider.name for provider in runtime.web_fetch_providers] == [
        "tavily",
        "tinyfish",
        "parallel",
    ]
    assert id(runtime.web_search_providers[0]) == id(runtime.web_fetch_providers[0])
    parallel_search = runtime.web_search_providers[-1]
    parallel_fetch = runtime.web_fetch_providers[-1]
    assert isinstance(only_member(parallel_search).adapter, ParallelAdapter)
    assert id(parallel_search) == id(parallel_fetch)
    assert set(runtime.llm_clients) == {"primary", "secondary"}
    assert [configured_capacity(p).limit for p in runtime.web_fetch_providers] == [3, 4, 6]
    assert [configured_capacity(p).limit for p in runtime.llm_clients.values()] == [2, 5]
    assert "RUNTIME_CREDENTIAL_SENTINEL" not in repr(runtime)
    assert "PARALLEL_CREDENTIAL_SENTINEL" not in repr(runtime)

    logged = stream.getvalue()
    assert "event=runtime_built" in logged
    assert "web_provider_count=4" in logged
    assert "web_providers=tavily,brave,tinyfish,parallel" in logged
    assert "llm_provider_count=2" in logged
    assert "llm_providers=primary,secondary" in logged
    assert "RUNTIME_CREDENTIAL_SENTINEL" not in logged
    assert "PARALLEL_CREDENTIAL_SENTINEL" not in logged
    assert "ENV_A" not in logged
    assert "ENV_F" not in logged

    await runtime.aclose()
    assert len(clients) == 6
    assert all(client.close_calls == 1 for client in clients)

    bad = _config()
    web = bad["web_providers"]
    assert isinstance(web, dict)
    brave = web["brave"]
    assert isinstance(brave, dict)
    brave["enable_fetch"] = True
    with pytest.raises(ConfigFailure):
        resolve_config(bad, registry, environment)


@pytest.mark.parametrize(
    ("provider_name", "adapter_type", "default_api_url"),
    [
        ("yuanbao", YuanbaoAdapter, "https://api.wsa.cloud.tencent.com"),
        ("zhihu", ZhihuAdapter, "https://developer.zhihu.com"),
    ],
)
@pytest.mark.parametrize("api_url", [None, "https://service.example.test/gateway"])
async def test_runtime_assembles_service_key_search_provider_with_owned_resources(
    tmp_path: Path,
    provider_name: str,
    adapter_type: type[YuanbaoAdapter] | type[ZhihuAdapter],
    default_api_url: str,
    api_url: str | None,
) -> None:
    registry = build_default_registry()
    raw = _config()
    credential: dict[str, object] = {"api_key_env": "SERVICE_KEY", "max_concurrency": 7}
    provider_data: dict[str, object] = {
        "enable_search": True,
        "enable_fetch": False,
        "credentials": [credential],
    }
    if api_url is not None:
        credential["api_url"] = api_url
    raw["web_providers"] = {provider_name: provider_data}
    environment = _environment()
    environment["SERVICE_KEY"] = "synthetic-service-value"
    resolved = resolve_config(raw, registry, environment)
    clients: list[_CountingAsyncClient] = []

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        client = _CountingAsyncClient()
        clients.append(client)
        return client

    runtime = await Runtime.build(
        resolved,
        RuntimePaths.from_home(tmp_path),
        registry=registry,
        http_client_factory=client_factory,
    )
    try:
        [provider] = runtime.web_search_providers
        member = only_member(provider)
        assert isinstance(member.adapter, adapter_type)
        assert provider.name == provider_name
        assert member.adapter._api_url == (default_api_url if api_url is None else api_url)
        assert runtime.web_fetch_providers == ()
        assert isinstance(member.adapter._http, HttpJsonExecutor)
        assert member.adapter._http._client is clients[0]
        assert member.capacity is not None and member.capacity.limit == 7
        assert set(runtime.llm_clients) == {"primary", "secondary"}
        assert len(clients) == 1 + len(runtime.llm_clients)
        assert len({id(client) for client in clients}) == len(clients)
        assert all(not client.is_closed for client in clients)
    finally:
        await runtime.aclose()

    await runtime.aclose()
    assert all(client.is_closed and client.close_calls == 1 for client in clients)


async def test_runtime_assembles_credentialless_jina_fetch_provider(
    tmp_path: Path,
) -> None:
    registry = build_default_registry()
    raw = _config()
    raw["web_providers"] = {
        "jina": {
            "enable_search": False,
            "enable_fetch": True,
            "credentials": [{"api_url": "https://reader.example.test/", "max_concurrency": 5}],
        },
    }
    resolved = resolve_config(raw, registry, _environment())
    [jina_config] = resolved.web.providers
    assert jina_config.credentials[0].api_key is None
    clients: list[_CountingAsyncClient] = []

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        client = _CountingAsyncClient()
        clients.append(client)
        return client

    runtime = await Runtime.build(
        resolved,
        RuntimePaths.from_home(tmp_path),
        registry=registry,
        http_client_factory=client_factory,
    )

    assert runtime.web_search_providers == ()
    [provider] = runtime.web_fetch_providers
    member = only_member(provider)
    assert isinstance(member.adapter, JinaReaderAdapter)
    assert provider.name == "jina"
    assert member.adapter._api_url == "https://reader.example.test"
    assert member.capacity is not None and member.capacity.limit == 5
    assert isinstance(member.adapter._http, HttpJsonExecutor)
    assert member.adapter._http._client is clients[0]

    await runtime.aclose()
    assert len(clients) == 3
    assert all(client.close_calls == 1 for client in clients)


async def test_runtime_assembles_brightdata_with_one_shared_dual_adapter(
    tmp_path: Path,
) -> None:
    registry = build_default_registry()
    raw = _config()
    raw["web_providers"] = {
        "brightdata": {
            "enable_search": True,
            "enable_fetch": True,
            "credentials": [
                {
                    "api_key_env": "ENV_BRIGHT",
                    "api_url": "https://bright.example.test",
                    "max_concurrency": 7,
                }
            ],
            "search_zone": "search-zone",
            "fetch_zone": "fetch-zone",
        },
    }
    environment = _environment()
    environment["ENV_BRIGHT"] = "BRIGHT_CREDENTIAL_SENTINEL"
    resolved = resolve_config(raw, registry, environment)
    clients: list[_CountingAsyncClient] = []

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        client = _CountingAsyncClient()
        clients.append(client)
        return client

    _logger, stream = structured_test_logger("agent_search_gateway.runtime")
    runtime = await Runtime.build(
        resolved,
        RuntimePaths.from_home(tmp_path),
        registry=registry,
        http_client_factory=client_factory,
    )

    [search_provider] = runtime.web_search_providers
    [fetch_provider] = runtime.web_fetch_providers
    member = only_member(search_provider)
    assert isinstance(member.adapter, BrightDataAdapter)
    assert id(search_provider) == id(fetch_provider)
    assert member.capacity is not None and member.capacity.limit == 7
    assert member.adapter._api_url == "https://bright.example.test"
    assert member.adapter._search_zone == "search-zone"
    assert member.adapter._fetch_zone == "fetch-zone"
    assert isinstance(member.adapter._http, HttpJsonExecutor)
    assert member.adapter._http._client is clients[0]
    assert "BRIGHT_CREDENTIAL_SENTINEL" not in repr(runtime)

    logged = stream.getvalue()
    assert "web_providers=brightdata" in logged
    assert "BRIGHT_CREDENTIAL_SENTINEL" not in logged
    assert "ENV_BRIGHT" not in logged

    await runtime.aclose()
    assert len(clients) == 3
    assert all(client.close_calls == 1 for client in clients)


async def test_runtime_maps_invalid_brightdata_zone_to_config_failure(
    tmp_path: Path,
) -> None:
    registry = build_default_registry()
    raw = _config()
    raw["web_providers"] = {
        "brightdata": {
            "enable_search": True,
            "enable_fetch": True,
            "credentials": [
                {
                    "api_key_env": "ENV_BRIGHT",
                    "api_url": "https://bright.example.test",
                    "max_concurrency": 7,
                }
            ],
            "search_zone": "   ",
            "fetch_zone": "fetch-zone",
        }
    }
    environment = _environment()
    environment["ENV_BRIGHT"] = "x"
    with pytest.raises(ConfigFailure, match="search_zone must be a non-empty string"):
        resolve_config(raw, registry, environment)


async def test_runtime_maps_invalid_parallel_adapter_config_to_config_failure(
    tmp_path: Path,
) -> None:
    registry = build_default_registry()
    raw = _config()
    web = raw["web_providers"]
    assert isinstance(web, dict)
    parallel = web["parallel"]
    assert isinstance(parallel, dict)
    parallel["mode"] = "invalid"
    resolved = resolve_config(raw, registry, _environment())
    clients: list[_CountingAsyncClient] = []

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        client = _CountingAsyncClient()
        clients.append(client)
        return client

    with pytest.raises(
        ConfigFailure,
        match="Invalid configuration for web provider parallel",
    ):
        await Runtime.build(
            resolved,
            RuntimePaths.from_home(tmp_path),
            registry=registry,
            http_client_factory=client_factory,
        )

    assert clients and all(client.close_calls == 1 for client in clients)


@pytest.mark.parametrize("reserved_key", ["name", "http_executor", "secret"])
async def test_runtime_rejects_reserved_web_adapter_kwargs(
    tmp_path: Path,
    reserved_key: str,
) -> None:
    registry = build_default_registry()
    registry.register(
        WebProviderRegistration(
            name="custom",
            capabilities=ProviderCapabilities(search=True, fetch=False),
            factory=lambda **kwargs: kwargs,
            allowed_config_keys=frozenset({"name", "http_executor", "secret"}),
        )
    )
    raw = _config()
    raw["web_providers"] = {
        "custom": {
            "enable_search": True,
            "credentials": [{"api_key_env": "ENV_CUSTOM"}],
            reserved_key: "override",
        },
    }
    resolved = resolve_config(
        raw,
        registry,
        {"ENV_CUSTOM": "x", "ENV_D": "x", "ENV_E": "x"},
    )

    with pytest.raises(ConfigFailure, match="Reserved config key"):
        await Runtime.build(resolved, RuntimePaths.from_home(tmp_path), registry=registry)


async def test_runtime_assembles_all_academic_providers_resolver_and_closes_once(
    tmp_path: Path,
) -> None:
    web_registry = build_default_registry()
    academic_registry = build_default_academic_registry()
    resolver_registry = build_default_oa_resolver_registry()
    raw = _config()
    raw["academic_providers"] = {
        "arxiv": {"enabled": True, "credentials": [{}]},
        "semantic_scholar": {"enabled": True, "credentials": [{}]},
        "openalex": {"enabled": True, "credentials": [{"api_key_env": "ENV_A"}]},
        "dblp": {"enabled": True, "credentials": [{}]},
        "crossref": {"enabled": True, "credentials": [{}]},
        "core": {
            "enabled": True,
            "credentials": [{"api_key_env": "ENV_CORE", "max_concurrency": 4}],
        },
    }
    raw["oa_resolvers"] = {
        "unpaywall": {"enabled": True, "credentials": [{"contact_email_env": "OA_CONTACT"}]}
    }
    environment = {
        "ENV_A": "x",
        "ENV_B": "x",
        "ENV_C": "x",
        "ENV_D": "x",
        "ENV_E": "x",
        "ENV_F": "x",
        "ENV_CORE": "x",
        "OA_CONTACT": "tester@example.test",
    }
    resolved = resolve_config(
        raw,
        web_registry,
        environment,
        academic_registry=academic_registry,
        oa_resolver_registry=resolver_registry,
    )
    clients: list[_CountingAsyncClient] = []

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        client = _CountingAsyncClient()
        clients.append(client)
        return client

    runtime = await Runtime.build(
        resolved,
        RuntimePaths.from_home(tmp_path),
        registry=web_registry,
        academic_registry=academic_registry,
        oa_resolver_registry=resolver_registry,
        http_client_factory=client_factory,
    )
    assert [provider.name for provider in runtime.academic_search_providers] == [
        "arxiv",
        "semantic_scholar",
        "openalex",
        "dblp",
        "crossref",
        "core",
    ]
    assert runtime.oa_resolver is not None
    assert runtime.oa_resolver.name == "unpaywall"
    assert runtime.paper_search_orchestrator.providers == runtime.academic_search_providers
    assert runtime.paper_search_orchestrator.resolver is runtime.oa_resolver
    assert runtime.paper_search_orchestrator.store is runtime.store
    for provider in (*runtime.academic_search_providers, runtime.oa_resolver):
        member = only_member(provider)
        assert member.rate is None
        assert member.adapter._executor._rate_gate is member.rate
    assert only_member(runtime.academic_search_providers[0]).capacity is None
    assert configured_capacity(runtime.academic_search_providers[-1]).limit == 4
    assert "tester@example.test" not in repr(runtime)

    await runtime.aclose()
    await runtime.aclose()
    assert len(clients) == 13
    assert all(client.close_calls == 1 for client in clients)


async def test_runtime_builds_one_pool_per_logical_provider(tmp_path: Path) -> None:
    raw = _config()
    web = raw["web_providers"]
    llm = raw["llm_providers"]
    assert isinstance(web, dict) and isinstance(llm, dict)
    web["tavily"]["credentials"].append({"api_key_env": "SECOND", "api_url": "https://second.test"})
    llm["primary"]["credentials"].append(
        {"api_key_env": "SECOND", "api_url": "https://second.test"}
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/extract":
            return httpx.Response(
                200, json={"results": [{"url": "https://target.test/", "raw_content": "body"}]}
            )
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    registry = build_default_registry()
    env = {**_environment(), "SECOND": "example"}
    resolved = resolve_config(raw, registry, env)
    runtime = await Runtime.build(
        resolved,
        RuntimePaths.from_home(tmp_path),
        http_client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ),
    )
    try:
        await runtime.web_search_providers[0].search("query")
        await runtime.web_fetch_providers[0].fetch(normalize_url("https://target.test/"))
        client = runtime.llm_clients["primary"]
        await client.complete_text(LLMInvocation("primary", "model-a"), [])
        await client.complete_json(LLMInvocation("primary", "model-b"), [])
        assert [request.url.host for request in requests] == [
            "tavily.example.test",
            "second.test",
            "llm-primary.example.test",
            "second.test",
        ]
        assert [request.headers["authorization"] for request in requests] == [
            "Bearer " + _environment()["ENV_A"],
            "Bearer " + env["SECOND"],
            "Bearer " + _environment()["ENV_D"],
            "Bearer " + env["SECOND"],
        ]
    finally:
        await runtime.aclose()


async def test_runtime_sorts_fetch_only_without_changing_search_order(tmp_path: Path) -> None:
    raw = _config()
    web = raw["web_providers"]
    assert isinstance(web, dict)
    web["fetch_order"] = ["firecrawl", "parallel"]
    registry = build_default_registry()
    runtime = await Runtime.build(
        resolve_config(raw, registry, _environment()),
        RuntimePaths.from_home(tmp_path),
        http_client_factory=lambda **_: _CountingAsyncClient(),
    )
    try:
        assert [p.name for p in runtime.web_fetch_providers] == ["parallel", "tavily", "tinyfish"]
        assert [p.name for p in runtime.web_search_providers] == ["tavily", "brave", "parallel"]
    finally:
        await runtime.aclose()
