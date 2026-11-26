"""Integration contracts for the four newly registered Web providers."""

import asyncio
import json
import logging
import tomllib
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import cast

import httpx
import pytest

from agent_search_gateway.config import resolve_config
from agent_search_gateway.errors import ConfigFailure, ErrorCode
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.providers.web.doubao import DoubaoAdapter
from agent_search_gateway.providers.web.keenable import KeenableAdapter
from agent_search_gateway.providers.web.open_websearch import OpenWebSearchAdapter
from agent_search_gateway.providers.web.youcom import YouComAdapter
from agent_search_gateway.rate_limit import make_rate_gate
from agent_search_gateway.runtime import Runtime
from agent_search_gateway.url_normalization import normalize_url
from tests.runtime.conftest import configured_capacity, only_member
from tests.support.controlled import ManualClock

_ADAPTERS: dict[str, type[object]] = {
    "doubao": DoubaoAdapter,
    "youcom": YouComAdapter,
    "keenable": KeenableAdapter,
    "open_websearch": OpenWebSearchAdapter,
}
_ENDPOINTS = {
    "doubao": ("https://open.feedcoopapi.com/search_api", "/web_search", "/fetch"),
    "youcom": ("https://ydc-index.io", "/v1/search", "/v1/contents"),
    "keenable": ("https://api.keenable.ai", "/v1/search", "/v1/fetch"),
    "open_websearch": ("http://127.0.0.1:3210", "/search", "/fetch-web"),
}
_TARGET = normalize_url("https://example.test/article?q=a%26b&lang=zh")
_BODY = "Runtime integration page body."


def _unexpected_request(request: httpx.Request) -> httpx.Response:
    raise AssertionError("Assembly must not issue HTTP requests")


class _CountingAsyncClient(httpx.AsyncClient):
    def __init__(self, transport: httpx.MockTransport) -> None:
        super().__init__(transport=transport)
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        await super().aclose()


class _ClientFactory:
    def __init__(self) -> None:
        self.transport = httpx.MockTransport(_unexpected_request)
        self.clients: list[_CountingAsyncClient] = []

    def __call__(self, **kwargs: object) -> httpx.AsyncClient:
        client = _CountingAsyncClient(self.transport)
        self.clients.append(client)
        return client


@pytest.fixture
def client_factory() -> Iterator[_ClientFactory]:
    factory = _ClientFactory()
    yield factory
    assert all(client.is_closed for client in factory.clients)


def _provider_config(name: str, **options: object) -> dict[str, object]:
    credential: dict[str, object] = {"max_concurrency": 7}
    if name != "open_websearch":
        credential["api_key_env"] = "TEST_WEB"
    for field in (
        "max_concurrency",
        "rate_limits",
        "min_interval_seconds",
        "api_key_env",
        "api_url",
    ):
        if field in options:
            credential[field] = options.pop(field)
    return {"enable_search": True, "enable_fetch": True, "credentials": [credential], **options}


def _config(web: dict[str, object]) -> dict[str, object]:
    return {
        "web_providers": web,
        "llm_providers": {
            name: {
                "protocol": "openai",
                "api_endpoint": "chat_completions",
                "credentials": [
                    {"api_url": f"https://{name}.example.test", "api_key_env": "TEST_LLM"}
                ],
            }
            for name in ("primary", "secondary")
        },
        "global_default_llm": {"provider": "primary", "model": "global"},
        "search_llm": {"providers": [{"provider": "secondary", "model": "search"}]},
        "fetch_llm": {"judge": {}, "safety": {}, "content_clean": {}, "focus_summary": {}},
        "retry": {"max_attempts": 1},
    }


async def _build_runtime(
    tmp_path: Path, web: dict[str, object], client_factory: _ClientFactory
) -> Runtime:
    registry = build_default_registry()
    resolved = resolve_config(_config(web), registry, {"TEST_WEB": "x", "TEST_LLM": "x"})
    return await Runtime.build(
        resolved,
        RuntimePaths.from_home(tmp_path),
        registry=registry,
        http_client_factory=client_factory,
    )


@pytest.mark.parametrize("interval", [60.0, 1e308])
async def test_gate_identity_is_known_before_selection_and_after_release(
    tmp_path: Path,
    client_factory: _ClientFactory,
    caplog: pytest.LogCaptureFixture,
    interval: float,
) -> None:
    caplog.set_level(logging.DEBUG, logger="agent_search_gateway")
    name = "open_websearch"
    config = _provider_config(name)
    config["credentials"] = [{"max_concurrency": 1, "min_interval_seconds": interval}] * 2
    runtime = await _build_runtime(tmp_path, {name: config}, client_factory)
    try:
        provider = runtime.web_fetch_providers[0]
        first = await provider.try_reserve_fetch()
        second = await provider.try_reserve_fetch()
        assert first is not None and second is not None
        assert await provider.try_reserve_fetch() is None
        for reserved in (second, first):
            rate = reserved.member.rate
            assert rate is not None
            await rate.permit(timeout_seconds=1)
            assert rate.next_available_in_seconds() > 0
            await reserved.release()
        blocked = asyncio.create_task(runtime.web_search_providers[0].search("query"))
        try:
            async with asyncio.timeout(1):
                while not any(
                    getattr(record, "gateway_event", None) == "credential_waiting"
                    for record in caplog.records
                ):
                    await asyncio.sleep(0)
        finally:
            blocked.cancel()
            with pytest.raises(asyncio.CancelledError):
                await blocked
    finally:
        await runtime.aclose()

    def indices(event: str) -> list[int]:
        fields = [
            vars(record)["gateway_fields"]
            for record in caplog.records
            if getattr(record, "gateway_event", None) == event
        ]
        assert all(f["provider"] == name and f["namespace"] == "web" for f in fields)
        return [f["credential_index"] for f in fields]

    assert indices("quota_acquired") == [0, 1]
    assert indices("quota_waiting") == [0, 1]
    assert indices("quota_released") == [1, 0]
    assert indices("rate_gate_acquired") == [1, 0]
    assert set(indices("credential_waiting")) == {0, 1}
    cancelled = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "credential_cancelled"
    ]
    assert cancelled == [{"namespace": "web", "provider": name}]


def _response(name: str, *, search: bool) -> httpx.Response:
    if search:
        payloads: dict[str, object] = {
            "doubao": {"Result": {"WebResults": []}},
            "youcom": {"results": {"web": []}},
            "keenable": {"results": []},
            "open_websearch": {"status": "ok", "data": {"results": []}},
        }
    else:
        payloads = {
            "doubao": {"Result": {"Data": {"StatusCode": 200, "ContentText": _BODY}}},
            "youcom": [{"url": str(_TARGET), "markdown": _BODY}],
            "keenable": {"url": str(_TARGET), "content": _BODY},
            "open_websearch": {"status": "ok", "data": {"content": _BODY}},
        }
    return httpx.Response(200, json=payloads[name])


@pytest.mark.parametrize("name", _ADAPTERS)
async def test_new_web_provider_shares_adapter_and_closes_client_once(
    tmp_path: Path, name: str, client_factory: _ClientFactory
) -> None:
    runtime = await _build_runtime(tmp_path, {name: _provider_config(name)}, client_factory)
    try:
        [search] = runtime.web_search_providers
        [fetch] = runtime.web_fetch_providers
        assert search is cast(object, fetch)
        assert isinstance(only_member(search).adapter, _ADAPTERS[name])
        assert configured_capacity(runtime.web_search_providers[0]).limit == 7
        assert len(client_factory.clients) == 3
        assert not any(client.close_calls for client in client_factory.clients)
    finally:
        await runtime.aclose()
        await runtime.aclose()
    assert all(client.close_calls == 1 for client in client_factory.clients)


@pytest.mark.parametrize("name", _ADAPTERS)
@pytest.mark.parametrize("custom", [False, True], ids=["default-root", "custom-prefix-root"])
async def test_new_web_provider_routes_search_and_fetch_through_configured_executor(
    tmp_path: Path, name: str, custom: bool, client_factory: _ClientFactory
) -> None:
    requests: list[httpx.Request] = []
    default_root, search_path, fetch_path = _ENDPOINTS[name]
    root = "https://gateway.example.test/prefix/" if custom else default_root

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response(name, search=request.url.path.endswith(search_path))

    client_factory.transport = httpx.MockTransport(handler)
    options = {"api_url": root} if custom else {}
    runtime = await _build_runtime(
        tmp_path, {name: _provider_config(name, **options)}, client_factory
    )
    try:
        assert await runtime.web_search_providers[0].search("runtime query") == []
        candidate = await runtime.web_fetch_providers[0].fetch(_TARGET)
        assert candidate.content == _BODY
        search_request, fetch_request = requests
        assert search_request.method == "POST"
        assert str(search_request.url) == root.rstrip("/") + search_path
        assert str(fetch_request.url.copy_with(query=None)) == root.rstrip("/") + fetch_path
        if name == "keenable":
            assert fetch_request.method == "GET"
            assert list(fetch_request.url.params.multi_items()) == [("url", str(_TARGET))]
            assert fetch_request.content == b""
        else:
            assert fetch_request.method == "POST"
            assert fetch_request.url.query == b""
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("name", _ADAPTERS)
@pytest.mark.parametrize(
    ("search_enabled", "fetch_enabled"),
    [(True, False), (False, True), (False, False)],
    ids=["search-only", "fetch-only", "disabled"],
)
async def test_new_web_provider_obeys_capability_switches(
    tmp_path: Path,
    name: str,
    search_enabled: bool,
    fetch_enabled: bool,
    client_factory: _ClientFactory,
) -> None:
    runtime = await _build_runtime(
        tmp_path,
        {name: _provider_config(name, enable_search=search_enabled, enable_fetch=fetch_enabled)},
        client_factory,
    )
    try:
        assert [provider.name for provider in runtime.web_search_providers] == (
            [name] if search_enabled else []
        )
        assert [provider.name for provider in runtime.web_fetch_providers] == (
            [name] if fetch_enabled else []
        )
        assert len(client_factory.clients) == 2 + int(search_enabled or fetch_enabled)
        for facade in (*runtime.web_search_providers, *runtime.web_fetch_providers):
            assert configured_capacity(facade).limit == 7
    finally:
        await runtime.aclose()


async def test_new_web_providers_together_own_four_clients_and_close_each_once(
    tmp_path: Path, client_factory: _ClientFactory
) -> None:
    runtime = await _build_runtime(
        tmp_path, {name: _provider_config(name) for name in _ADAPTERS}, client_factory
    )
    try:
        assert [provider.name for provider in runtime.web_search_providers] == list(_ADAPTERS)
        assert [provider.name for provider in runtime.web_fetch_providers] == list(_ADAPTERS)
        for search, fetch in zip(
            runtime.web_search_providers, runtime.web_fetch_providers, strict=True
        ):
            assert search is cast(object, fetch)
        assert len(client_factory.clients) == 6
    finally:
        await runtime.aclose()
        await runtime.aclose()
    assert all(client.close_calls == 1 for client in client_factory.clients)


class _SchedulerWaitingHandler(logging.Handler):
    def __init__(self, waiting: asyncio.Event) -> None:
        super().__init__()
        self.waiting = waiting

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "gateway_event", None) == "scheduler_waiting":
            self.waiting.set()


@pytest.mark.parametrize("name", _ADAPTERS)
async def test_new_web_provider_search_and_fetch_contend_for_runtime_quota(
    tmp_path: Path,
    name: str,
    client_factory: _ClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    search_entered = asyncio.Event()
    release_search = asyncio.Event()
    fetch_entered = asyncio.Event()
    scheduler_waiting = asyncio.Event()
    _, search_path, _ = _ENDPOINTS[name]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary.example.test":
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})
        if request.url.path.endswith(search_path):
            search_entered.set()
            await release_search.wait()
            return _response(name, search=True)
        fetch_entered.set()
        return _response(name, search=False)

    client_factory.transport = httpx.MockTransport(handler)
    runtime = await _build_runtime(
        tmp_path, {name: _provider_config(name, max_concurrency=1)}, client_factory
    )
    logger = logging.getLogger("agent_search_gateway.scheduler.fetch")
    caplog.set_level(logging.DEBUG, logger=logger.name)
    waiting_handler = _SchedulerWaitingHandler(scheduler_waiting)
    logger.addHandler(waiting_handler)
    try:
        async with asyncio.timeout(3), asyncio.TaskGroup() as tasks:
            search = tasks.create_task(
                runtime.search_orchestrator.keyword_search("runtime query", request_id="12345678")
            )
            await search_entered.wait()
            fetch = tasks.create_task(
                runtime.fetch_orchestrator._scheduler.fetch_until_accepted(_TARGET)
            )
            await scheduler_waiting.wait()
            assert not fetch_entered.is_set()
            assert configured_capacity(runtime.web_search_providers[0]).in_use == 1
            release_search.set()
            await fetch_entered.wait()
        assert Path(search.result()).is_file()
        assert fetch.result().kind == "accepted"
        assert configured_capacity(runtime.web_search_providers[0]).in_use == 0
        assert configured_capacity(runtime.web_search_providers[0]).max_observed_in_use == 1
    finally:
        release_search.set()
        logger.removeHandler(waiting_handler)
        await runtime.aclose()


async def test_new_web_provider_forwards_open_websearch_toml_engines(
    tmp_path: Path, client_factory: _ClientFactory
) -> None:
    parsed = tomllib.loads(
        """
[web_providers.open_websearch]
enable_search = true
enable_fetch = true
credentials = [{max_concurrency = 7}]
engines = ["custom_z", "bing", "custom_z", " custom_a "]
"""
    )
    web = parsed["web_providers"]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response("open_websearch", search=request.url.path == "/search")

    client_factory.transport = httpx.MockTransport(handler)
    runtime = await _build_runtime(tmp_path, web, client_factory)
    try:
        [search] = runtime.web_search_providers
        [fetch] = runtime.web_fetch_providers
        assert search is cast(object, fetch)
        await search.search("runtime query")
        await fetch.fetch(_TARGET)
        assert [json.loads(request.content) for request in requests] == [
            {
                "query": "runtime query",
                "engines": ["custom_z", "bing", "custom_z", " custom_a "],
                "limit": 50,
            },
            {"url": str(_TARGET)},
        ]
        assert configured_capacity(runtime.web_search_providers[0]).limit == 7
        assert len(client_factory.clients) == 3
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(
    "engines", [[], "bing,duckduckgo", {}, 1, True, [""], ["   "], ["bing", 1], [None]]
)
async def test_new_web_provider_wraps_invalid_engines_as_config_failure(
    tmp_path: Path, engines: object, client_factory: _ClientFactory
) -> None:
    try:
        with pytest.raises(
            ConfigFailure, match="Invalid configuration for web provider open_websearch"
        ) as caught:
            await _build_runtime(
                tmp_path,
                {"open_websearch": _provider_config("open_websearch", engines=engines)},
                client_factory,
            )
        assert isinstance(caught.value.__cause__, TypeError)
        assert len(client_factory.clients) == 1
    finally:
        assert all(client.close_calls == 1 for client in client_factory.clients)


async def test_new_web_provider_skips_engines_validation_when_disabled(
    tmp_path: Path, client_factory: _ClientFactory
) -> None:
    runtime = await _build_runtime(
        tmp_path,
        {
            "open_websearch": _provider_config(
                "open_websearch", enable_search=False, enable_fetch=False, engines=[], api_url=" "
            )
        },
        client_factory,
    )
    try:
        assert runtime.web_search_providers == ()
        assert runtime.web_fetch_providers == ()
        assert len(client_factory.clients) == 2
        assert not runtime.web_search_providers
    finally:
        await runtime.aclose()


async def test_new_web_provider_rejects_unknown_key_even_when_disabled(
    tmp_path: Path, client_factory: _ClientFactory
) -> None:
    with pytest.raises(ConfigFailure, match="unknown config key"):
        await _build_runtime(
            tmp_path,
            {
                "open_websearch": _provider_config(
                    "open_websearch", enable_search=False, enable_fetch=False, engines=[], limit=50
                )
            },
            client_factory,
        )
    assert client_factory.clients == []


@pytest.mark.parametrize("name", _ADAPTERS)
async def test_new_web_provider_builds_with_shared_rate_fields(
    tmp_path: Path,
    name: str,
    client_factory: _ClientFactory,
) -> None:
    runtime = await _build_runtime(
        tmp_path,
        {
            name: _provider_config(
                name,
                rate_limits=[{"requests": 5, "period_seconds": 1}],
                min_interval_seconds=0.1,
            )
        },
        client_factory,
    )
    try:
        [search] = runtime.web_search_providers
        [fetch] = runtime.web_fetch_providers
        assert search is cast(object, fetch)
        assert isinstance(only_member(search).adapter, _ADAPTERS[name])
        assert configured_capacity(runtime.web_search_providers[0]).limit == 7
        gate = only_member(search).rate
        assert gate is not None
        assert only_member(search).adapter._http._rate_gate is gate
    finally:
        await runtime.aclose()


async def test_baiduqianfan_runtime_owns_one_executor_and_gate(
    tmp_path: Path, client_factory: _ClientFactory
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload: dict[str, object] = (
            {"references": []}
            if request.url.path.endswith("/web_search")
            else {"web_page_result": {"web_pages": []}}
        )
        return httpx.Response(200, json=payload)

    client_factory.transport = httpx.MockTransport(respond)
    runtime = await _build_runtime(
        tmp_path,
        {
            "baiduqianfan": _provider_config(
                "baiduqianfan",
                enable_search=True,
                enable_fetch=False,
                rate_limits=[{"requests": 3, "period_seconds": 1}],
            )
        },
        client_factory,
    )
    try:
        [search] = runtime.web_search_providers
        assert search.name == "baiduqianfan"
        assert runtime.web_fetch_providers == ()
        executor = only_member(search).adapter._http
        gate = only_member(search).rate
        assert gate is not None
        assert executor._rate_gate is gate
        assert len(client_factory.clients) == 3  # One Web client and two LLM clients.
        assert executor._client is client_factory.clients[0]
        assert await search.search("shared runtime executor") == []
        assert {request.url.path for request in requests} == {
            "/v2/ai_search/web_search",
            "/v2/ai_search/agentic_search",
        }
        assert len(requests) == 2
        assert all(request.method == "POST" for request in requests)
    finally:
        await runtime.aclose()
        await runtime.aclose()
    assert all(client.close_calls == 1 for client in client_factory.clients)


async def test_baiduqianfan_without_rate_limits_has_no_hidden_three_request_window(
    tmp_path: Path, client_factory: _ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock()
    sends: list[float] = []
    monkeypatch.setattr(
        "agent_search_gateway.runtime.make_rate_gate",
        partial(make_rate_gate, monotonic=clock.monotonic, sleep=clock.sleep),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        sends.append(clock.now)
        payload: dict[str, object] = (
            {"references": []}
            if request.url.path.endswith("/web_search")
            else {"web_page_result": {"web_pages": []}}
        )
        return httpx.Response(200, json=payload)

    client_factory.transport = httpx.MockTransport(respond)
    runtime = await _build_runtime(
        tmp_path,
        {"baiduqianfan": _provider_config("baiduqianfan", enable_fetch=False)},
        client_factory,
    )
    try:
        [search] = runtime.web_search_providers
        async with asyncio.timeout(1), asyncio.TaskGroup() as tasks:
            first = tasks.create_task(search.search("first unlimited query"))
            second = tasks.create_task(search.search("second unlimited query"))
        assert first.result() == second.result() == []
        assert sends == [0, 0, 0, 0]
        assert not clock.sleepers
    finally:
        await runtime.aclose()


def _youcom_mcp_config(**options: object) -> dict[str, object]:
    options.setdefault("api_url", "https://api.you.com/mcp/free")
    config = _provider_config("youcom", **options)
    if "api_key_env" not in options:
        members = config["credentials"]
        assert isinstance(members, list)
        (member,) = members
        assert isinstance(member, dict)
        member.pop("api_key_env")
    return config


async def test_youcom_mcp_runtime_builds_without_secret(
    tmp_path: Path, client_factory: _ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _youcom_mcp_response(request)

    client_factory.transport = httpx.MockTransport(handler)
    monkeypatch.setenv("YDC_API_KEY", "x")
    registry = build_default_registry()
    resolved = resolve_config(
        _config({"youcom": _youcom_mcp_config()}),
        registry,
        {"TEST_LLM": "x", "YDC_API_KEY": "x"},
    )
    [provider] = resolved.web.providers
    assert provider.credentials[0].api_key is None
    assert provider.credentials[0].api_key_env is None
    runtime = await Runtime.build(
        resolved,
        RuntimePaths.from_home(tmp_path),
        http_client_factory=client_factory,
    )
    try:
        assert isinstance(only_member(runtime.web_search_providers[0]).adapter, YouComAdapter)
        assert len(client_factory.clients) == 3
        assert requests == []
        assert await runtime.web_search_providers[0].search("runtime query") == []
        candidate = await runtime.web_fetch_providers[0].fetch(_TARGET)
        assert candidate.content == _BODY
        assert len(requests) == 2
        for request in requests:
            assert str(request.url) == "https://api.you.com/mcp/free"
            assert request.method == "POST"
            assert not any(key in request.headers for key in ("authorization", "x-api-key"))
        search_request, fetch_request = (json.loads(request.content) for request in requests)
        assert search_request["params"]["arguments"] == {"query": "runtime query", "count": 10}
        assert fetch_request["params"]["arguments"] == {"url": str(_TARGET)}
    finally:
        await runtime.aclose()


async def test_youcom_mcp_runtime_shares_adapter_executor_and_rate_gate(
    tmp_path: Path, client_factory: _ClientFactory
) -> None:
    runtime = await _build_runtime(
        tmp_path,
        {"youcom": _youcom_mcp_config(rate_limits=[{"requests": 5, "period_seconds": 1}])},
        client_factory,
    )
    try:
        [search] = runtime.web_search_providers
        [fetch] = runtime.web_fetch_providers
        assert search is cast(object, fetch)
        assert isinstance(only_member(search).adapter, YouComAdapter)
        assert only_member(search).adapter._secret is None
        executor = only_member(search).adapter._http
        assert only_member(search).adapter._http is executor
        assert len(client_factory.clients) == 3
        assert configured_capacity(runtime.web_search_providers[0]).limit == 7
        gate = only_member(search).rate
        assert gate is not None
        assert executor._rate_gate is gate
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(
    ("options", "environment", "message"),
    [
        ({}, {}, "REST requires an API key"),
        (
            {"api_url": "https://api.you.com/mcp/free", "api_key_env": "TEST_WEB"},
            {"TEST_WEB": "x"},
            "free MCP does not accept an API key",
        ),
        (
            {"api_url": "https://api.you.com/mcp/free", "api_key_env": "TEST_WEB"},
            {},
            "environment variable TEST_WEB is required",
        ),
        (
            {"api_url": "https://api.you.com/mcp/free", "api_key_env": "TEST_WEB"},
            {"TEST_WEB": ""},
            "environment variable TEST_WEB is required",
        ),
        (
            {"api_url": "https://api.you.com/mcp/free", "api_key_env": ""},
            {},
            "api_key_env must be a non-empty string",
        ),
        (
            {"api_url": "https://api.you.com/mcp"},
            {},
            "Unsupported You.com MCP endpoint",
        ),
        (
            {"api_url": "https://api.you.com/mcp/free?unsupported=true"},
            {},
            "Unsupported You.com MCP endpoint",
        ),
    ],
    ids=[
        "rest-without-key",
        "free-with-explicit-key",
        "missing-env-value",
        "empty-env-value",
        "empty-env-name",
        "unsupported-mcp",
        "unsupported-free-query",
    ],
)
async def test_youcom_runtime_rejects_invalid_protocol_credentials_before_http(
    tmp_path: Path,
    client_factory: _ClientFactory,
    options: dict[str, object],
    environment: dict[str, str],
    message: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("Invalid configuration must not issue HTTP requests")

    client_factory.transport = httpx.MockTransport(handler)
    try:
        with pytest.raises(ConfigFailure, match=message) as caught:
            resolved = resolve_config(
                _config(
                    {
                        "youcom": {
                            "enable_search": True,
                            "enable_fetch": True,
                            "credentials": [options],
                        }
                    }
                ),
                build_default_registry(),
                {"TEST_LLM": "x", **environment},
            )
            await Runtime.build(
                resolved,
                RuntimePaths.from_home(tmp_path),
                http_client_factory=client_factory,
            )
        assert caught.value.code is ErrorCode.CONFIG_ERROR
        assert client_factory.clients == []
        assert requests == []
    finally:
        for client in client_factory.clients:
            await client.aclose()


@pytest.mark.parametrize(
    ("search_enabled", "fetch_enabled"),
    [(True, False), (False, True), (True, True), (False, False)],
    ids=["search-only", "fetch-only", "both", "disabled"],
)
async def test_youcom_mcp_runtime_obeys_capability_switches(
    tmp_path: Path,
    client_factory: _ClientFactory,
    search_enabled: bool,
    fetch_enabled: bool,
) -> None:
    options: dict[str, object] = {}
    if not search_enabled and not fetch_enabled:
        options = {"api_key_env": "MISSING", "api_url": "https://api.you.com/mcp/unsupported"}
    runtime = await _build_runtime(
        tmp_path,
        {
            "youcom": _youcom_mcp_config(
                enable_search=search_enabled, enable_fetch=fetch_enabled, **options
            )
        },
        client_factory,
    )
    try:
        assert [provider.name for provider in runtime.web_search_providers] == (
            ["youcom"] if search_enabled else []
        )
        assert [provider.name for provider in runtime.web_fetch_providers] == (
            ["youcom"] if fetch_enabled else []
        )
        assert len(client_factory.clients) == 2 + int(search_enabled or fetch_enabled)
        if not search_enabled and not fetch_enabled:
            assert runtime.web_fetch_providers == ()
    finally:
        await runtime.aclose()


async def test_youcom_mcp_runtime_closes_client_once(
    tmp_path: Path, client_factory: _ClientFactory
) -> None:
    runtime = await _build_runtime(tmp_path, {"youcom": _youcom_mcp_config()}, client_factory)
    await runtime.aclose()
    await runtime.aclose()
    assert len(client_factory.clients) == 3
    assert all(client.close_calls == 1 for client in client_factory.clients)


def _youcom_mcp_response(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    tool = body["params"]["name"]
    if tool == "you-search":
        payload: dict[str, object] = {"results": {"web": []}}
    else:
        assert tool == "you-extract"
        payload = {
            "output": {
                "url": str(_TARGET),
                "contents": {"full_page": {"markdown": _BODY}},
            }
        }
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": body["id"], "result": {"structuredContent": payload}},
    )


async def test_youcom_mcp_search_and_fetch_share_concurrency(
    tmp_path: Path,
    client_factory: _ClientFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    search_entered = asyncio.Event()
    release_search = asyncio.Event()
    fetch_entered = asyncio.Event()
    scheduler_waiting = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary.example.test":
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})
        body = json.loads(request.content)
        if body["params"]["name"] == "you-search":
            search_entered.set()
            await release_search.wait()
        else:
            fetch_entered.set()
        return _youcom_mcp_response(request)

    client_factory.transport = httpx.MockTransport(handler)
    runtime = await _build_runtime(
        tmp_path, {"youcom": _youcom_mcp_config(max_concurrency=1)}, client_factory
    )
    logger = logging.getLogger("agent_search_gateway.scheduler.fetch")
    caplog.set_level(logging.DEBUG, logger=logger.name)
    waiting_handler = _SchedulerWaitingHandler(scheduler_waiting)
    logger.addHandler(waiting_handler)
    try:
        async with asyncio.timeout(3), asyncio.TaskGroup() as tasks:
            search = tasks.create_task(
                runtime.search_orchestrator.keyword_search("runtime query", request_id="12345678")
            )
            await search_entered.wait()
            fetch = tasks.create_task(
                runtime.fetch_orchestrator._scheduler.fetch_until_accepted(_TARGET)
            )
            await scheduler_waiting.wait()
            assert not fetch_entered.is_set()
            assert configured_capacity(runtime.web_search_providers[0]).in_use == 1
            release_search.set()
            await fetch_entered.wait()
        assert Path(search.result()).is_file()
        assert fetch.result().kind == "accepted"
        assert configured_capacity(runtime.web_search_providers[0]).in_use == 0
        assert configured_capacity(runtime.web_search_providers[0]).max_observed_in_use == 1
    finally:
        release_search.set()
        logger.removeHandler(waiting_handler)
        await runtime.aclose()


@pytest.mark.parametrize("name", ["tinyfish", "brightdata", "youcom"])
async def test_provider_credential_endpoints_and_options(tmp_path: Path, name: str) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if name == "tinyfish":
            return httpx.Response(
                200,
                json={"results": [{"url": str(_TARGET), "text": _BODY}]}
                if request.method == "POST"
                else {"results": []},
            )
        if name == "brightdata":
            if "data_format" in json.loads(request.content):
                return httpx.Response(200, text=_BODY)
            return httpx.Response(200, json={"organic": []})
        if request.url.path == "/mcp/free":
            return _youcom_mcp_response(request)
        return _response("youcom", search=request.url.path.endswith("search"))

    members = []
    for index, env in enumerate(("FIRST", "SECOND")):
        if name == "tinyfish":
            member = {
                "api_key_env": env,
                "search_api_url": f"https://search-{index}.test",
                "fetch_api_url": f"https://fetch-{index}.test",
            }
        elif name == "brightdata":
            member = {
                "api_key_env": env,
                "api_url": f"https://account-{index}.test",
                "search_zone": f"search-{index}",
                "fetch_zone": f"fetch-{index}",
            }
        elif index == 0:
            member = {"api_key_env": env, "api_url": "https://rest.test"}
        else:
            member = {"api_url": "https://api.you.com/mcp/free"}
        members.append(member)
    web: dict[str, object] = {
        name: {"enable_search": True, "enable_fetch": True, "credentials": members}
    }
    registry = build_default_registry()
    env_values = {"FIRST": "one", "SECOND": "two", "TEST_LLM": "example"}
    config = resolve_config(_config(web), registry, env_values)
    runtime = await Runtime.build(
        config,
        RuntimePaths.from_home(tmp_path),
        http_client_factory=lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    try:
        (search,) = runtime.web_search_providers
        (fetch,) = runtime.web_fetch_providers
        for _ in members:
            assert await search.search("test query") == []
        for _ in members:
            assert (await fetch.fetch(_TARGET)).content == _BODY
        if name == "tinyfish":
            assert [r.url.host for r in requests] == [
                "search-0.test",
                "search-1.test",
                "fetch-0.test",
                "fetch-1.test",
            ]
            assert [r.headers["x-api-key"] for r in requests] == ["one", "two"] * 2
        elif name == "brightdata":
            assert [r.url.host for r in requests] == ["account-0.test", "account-1.test"] * 2
            assert [json.loads(r.content)["zone"] for r in requests] == [
                "search-0",
                "search-1",
                "fetch-0",
                "fetch-1",
            ]
            assert [r.headers["authorization"].split()[-1] for r in requests] == ["one", "two"] * 2
        else:
            assert [r.url.host for r in requests] == ["rest.test", "api.you.com"] * 2
            assert [r.headers.get("x-api-key") for r in requests] == ["one", None] * 2
    finally:
        await runtime.aclose()
