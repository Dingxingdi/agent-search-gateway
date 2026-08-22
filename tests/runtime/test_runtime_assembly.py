from pathlib import Path

import httpx
import pytest

from agent_search_gateway.config import resolve_config
from agent_search_gateway.errors import ConfigFailure
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.academic.defaults import (
    build_default_academic_registry,
    build_default_oa_resolver_registry,
)
from agent_search_gateway.providers.contracts import ProviderCapabilities
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.providers.registry import WebProviderRegistration
from agent_search_gateway.providers.web.brightdata import BrightDataAdapter
from agent_search_gateway.providers.web.parallel import ParallelAdapter
from agent_search_gateway.runtime import Runtime
from tests.support.logging import structured_test_logger


class _CountingAsyncClient(httpx.AsyncClient):
    def __init__(self) -> None:
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
            "default_max_concurrency": 3,
            "tavily": {
                "enable_search": True,
                "enable_fetch": True,
                "api_key_env": "ENV_A",
                "api_url": "https://tavily.example.test",
            },
            "brave": {
                "enable_search": True,
                "enable_fetch": False,
                "api_key_env": "ENV_B",
                "api_url": "https://brave.example.test",
            },
            "tinyfish": {
                "enable_search": False,
                "enable_fetch": True,
                "api_key_env": "ENV_C",
                "search_api_url": "https://search.tiny.example.test",
                "fetch_api_url": "https://fetch.tiny.example.test",
                "max_concurrency": 4,
            },
            "firecrawl": {
                "enable_search": False,
                "enable_fetch": False,
                "api_url": "https://disabled.example.test",
            },
            "parallel": {
                "enable_search": True,
                "enable_fetch": True,
                "api_key_env": "ENV_F",
                "api_url": "https://parallel.example.test",
                "mode": "turbo",
                "search_fetch_policy": {"max_age_seconds": 3600},
                "extract_fetch_policy": {
                    "max_age_seconds": 600,
                    "timeout_seconds": 30,
                    "disable_cache_fallback": True,
                },
                "max_concurrency": 6,
            },
        },
        "llm_providers": {
            "default_max_concurrency": 2,
            "primary": {
                "protocol": "openai",
                "api_endpoint": "chat_completions",
                "api_url": "https://llm-primary.example.test",
                "api_key_env": "ENV_D",
            },
            "secondary": {
                "protocol": "openai",
                "api_endpoint": "chat_completions",
                "api_url": "https://llm-secondary.example.test",
                "api_key_env": "ENV_E",
                "max_concurrency": 5,
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
    ]
    parallel_registration = registry.get("parallel")
    assert parallel_registration is not None
    assert parallel_registration.allowed_config_keys == frozenset(
        {"api_url", "mode", "search_fetch_policy", "extract_fetch_policy"}
    )
    environment = _environment()
    resolved = resolve_config(_config(), registry, environment)
    clients: list[_CountingAsyncClient] = []

    def client_factory() -> httpx.AsyncClient:
        client = _CountingAsyncClient()
        clients.append(client)
        return client

    _logger, stream = structured_test_logger("agent_search_gateway.runtime")
    runtime = Runtime.build(
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
    assert isinstance(parallel_search, ParallelAdapter)
    assert id(parallel_search) == id(parallel_fetch)
    assert set(runtime.llm_clients) == {"primary", "secondary"}
    assert runtime.quotas.get_web("tavily").limit == 3
    assert runtime.quotas.get_web("tinyfish").limit == 4
    assert runtime.quotas.get_web("parallel").limit == 6
    assert runtime.quotas.get_llm("primary").limit == 2
    assert runtime.quotas.get_llm("secondary").limit == 5
    assert "RUNTIME_CREDENTIAL_SENTINEL" not in repr(runtime)
    assert "PARALLEL_CREDENTIAL_SENTINEL" not in repr(runtime)

    logged = stream.getvalue()
    assert "event=runtime_built" in logged
    assert "web_provider_count=4" in logged
    assert "web_providers=tavily,brave,tinyfish,parallel" in logged
    assert "llm_provider_count=2" in logged
    assert "llm_providers=primary,secondary" in logged
    assert "web_limits=tavily:3,brave:3,tinyfish:4,parallel:6" in logged
    assert "llm_limits=primary:2,secondary:5" in logged
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


async def test_runtime_assembles_brightdata_with_one_shared_dual_adapter(
    tmp_path: Path,
) -> None:
    registry = build_default_registry()
    raw = _config()
    raw["web_providers"] = {
        "default_max_concurrency": 3,
        "brightdata": {
            "enable_search": True,
            "enable_fetch": True,
            "api_key_env": "ENV_BRIGHT",
            "api_url": "https://bright.example.test",
            "search_zone": "search-zone",
            "fetch_zone": "fetch-zone",
            "max_concurrency": 7,
        },
    }
    environment = _environment()
    environment["ENV_BRIGHT"] = "BRIGHT_CREDENTIAL_SENTINEL"
    resolved = resolve_config(raw, registry, environment)
    clients: list[_CountingAsyncClient] = []

    def client_factory() -> httpx.AsyncClient:
        client = _CountingAsyncClient()
        clients.append(client)
        return client

    _logger, stream = structured_test_logger("agent_search_gateway.runtime")
    runtime = Runtime.build(
        resolved,
        RuntimePaths.from_home(tmp_path),
        registry=registry,
        http_client_factory=client_factory,
    )

    [search_provider] = runtime.web_search_providers
    [fetch_provider] = runtime.web_fetch_providers
    assert isinstance(search_provider, BrightDataAdapter)
    assert search_provider is fetch_provider
    assert runtime.quotas.get_web("brightdata").limit == 7
    assert search_provider._api_url == "https://bright.example.test"
    assert search_provider._search_zone == "search-zone"
    assert search_provider._fetch_zone == "fetch-zone"

    [executor] = runtime._web_http_executors
    assert callable(executor.request_json)
    assert callable(executor.request_text)
    assert executor._client is clients[0]
    assert "BRIGHT_CREDENTIAL_SENTINEL" not in repr(runtime)

    logged = stream.getvalue()
    assert "web_providers=brightdata" in logged
    assert "web_limits=brightdata:7" in logged
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
            "api_key_env": "ENV_BRIGHT",
            "api_url": "https://bright.example.test",
            "search_zone": "   ",
            "fetch_zone": "fetch-zone",
        }
    }
    environment = _environment()
    environment["ENV_BRIGHT"] = "x"
    resolved = resolve_config(raw, registry, environment)
    clients: list[_CountingAsyncClient] = []

    def client_factory() -> httpx.AsyncClient:
        client = _CountingAsyncClient()
        clients.append(client)
        return client

    with pytest.raises(
        ConfigFailure,
        match="Invalid configuration for web provider brightdata",
    ):
        Runtime.build(
            resolved,
            RuntimePaths.from_home(tmp_path),
            registry=registry,
            http_client_factory=client_factory,
        )

    for client in clients:
        await client.aclose()


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

    def client_factory() -> httpx.AsyncClient:
        client = _CountingAsyncClient()
        clients.append(client)
        return client

    with pytest.raises(
        ConfigFailure,
        match="Invalid configuration for web provider parallel",
    ):
        Runtime.build(
            resolved,
            RuntimePaths.from_home(tmp_path),
            registry=registry,
            http_client_factory=client_factory,
        )

    for client in clients:
        await client.aclose()


@pytest.mark.parametrize("reserved_key", ["name", "http_executor", "secret"])
def test_runtime_rejects_reserved_web_adapter_kwargs(
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
        "default_max_concurrency": 3,
        "custom": {
            "enable_search": True,
            "api_key_env": "ENV_CUSTOM",
            reserved_key: "override",
        },
    }
    resolved = resolve_config(
        raw,
        registry,
        {"ENV_CUSTOM": "x", "ENV_D": "x", "ENV_E": "x"},
    )

    with pytest.raises(ConfigFailure, match="Reserved config key"):
        Runtime.build(resolved, RuntimePaths.from_home(tmp_path), registry=registry)


async def test_runtime_assembles_all_academic_providers_resolver_and_closes_once(
    tmp_path: Path,
) -> None:
    web_registry = build_default_registry()
    academic_registry = build_default_academic_registry()
    resolver_registry = build_default_oa_resolver_registry()
    raw = _config()
    raw["academic_providers"] = {
        "default_max_concurrency": 2,
        "arxiv": {"enabled": True},
        "semantic_scholar": {"enabled": True},
        "openalex": {"enabled": True},
        "dblp": {"enabled": True},
        "crossref": {"enabled": True},
        "core": {
            "enabled": True,
            "api_key_env": "CORE_API_KEY",
            "max_concurrency": 4,
        },
    }
    raw["oa_resolvers"] = {
        "unpaywall": {"enabled": True, "contact_email_env": "OA_CONTACT"}
    }
    environment = {
        "ENV_A": "x",
        "ENV_B": "x",
        "ENV_C": "x",
        "ENV_D": "x",
        "ENV_E": "x",
        "ENV_F": "x",
        "CORE_API_KEY": "x",
        "OA_CONTACT": "[REDACTED_SECRET]",
    }
    resolved = resolve_config(
        raw,
        web_registry,
        environment,
        academic_registry=academic_registry,
        oa_resolver_registry=resolver_registry,
    )
    clients: list[_CountingAsyncClient] = []

    def client_factory() -> httpx.AsyncClient:
        client = _CountingAsyncClient()
        clients.append(client)
        return client

    runtime = Runtime.build(
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
    assert runtime.quotas.get_academic("arxiv").limit == 2
    assert runtime.quotas.get_academic("core").limit == 4
    assert "[REDACTED_SECRET]" not in repr(runtime)

    await runtime.aclose()
    await runtime.aclose()
    assert len(clients) == 13
    assert all(client.close_calls == 1 for client in clients)
