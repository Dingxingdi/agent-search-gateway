from pathlib import Path

import httpx

from agent_search_gateway.config import resolve_config
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.academic.defaults import (
    build_default_academic_registry,
    build_default_oa_resolver_registry,
)
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.runtime import Runtime
from tests.runtime.test_runtime_assembly import _config, _CountingAsyncClient


async def test_runtime_instantiates_only_enabled_academic_providers_in_configured_order(
    tmp_path: Path,
) -> None:
    web_registry = build_default_registry()
    academic_registry = build_default_academic_registry()
    resolver_registry = build_default_oa_resolver_registry()
    raw = _config()
    raw["academic_providers"] = {
        "default_max_concurrency": 2,
        "core": {
            "enabled": True,
            "api_key_env": "CORE_API_KEY",
            "max_concurrency": 4,
        },
        "arxiv": {"enabled": False},
        "openalex": {"enabled": True, "api_key_env": "ENV_A"},
    }
    resolved = resolve_config(
        raw,
        web_registry,
        {
            "ENV_A": "x",
            "ENV_B": "x",
            "ENV_C": "x",
            "ENV_D": "x",
            "ENV_E": "x",
            "ENV_F": "x",
            "CORE_API_KEY": "x",
        },
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
        "core",
        "openalex",
    ]
    assert runtime.quotas.get_rate("academic", "arxiv") is None
    assert runtime.quotas.get_rate("oa", "unpaywall") is None
    assert runtime.oa_resolver is None
    assert runtime.quotas.get_academic("core").limit == 4
    assert runtime.quotas.get_academic("openalex").limit == 2
    assert (
        runtime.search_orchestrator._paper_aggregator
        is runtime.paper_search_orchestrator.aggregator
    )
    assert runtime.search_orchestrator._paper_resolver is runtime.paper_search_orchestrator.resolver
    assert runtime.search_orchestrator._store is runtime.paper_search_orchestrator.store

    await runtime.aclose()
    assert all(client.close_calls == 1 for client in clients)
