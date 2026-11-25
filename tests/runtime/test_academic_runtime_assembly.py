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
from tests.runtime.conftest import only_member
from tests.runtime.test_runtime_assembly import _config, _CountingAsyncClient


async def test_runtime_instantiates_only_enabled_academic_providers_in_configured_order(
    tmp_path: Path,
) -> None:
    web_registry = build_default_registry()
    academic_registry = build_default_academic_registry()
    resolver_registry = build_default_oa_resolver_registry()
    raw = _config()
    raw["academic_providers"] = {
        "core": {
            "enabled": True,
            "credentials": [{"api_key_env": "ENV_CORE", "max_concurrency": 4}],
        },
        "arxiv": {"enabled": False},
        "openalex": {
            "enabled": True,
            "credentials": [{"api_key_env": "ENV_A", "max_concurrency": 2}],
        },
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
            "ENV_CORE": "x",
        },
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
        "core",
        "openalex",
    ]
    assert runtime.oa_resolver is None
    assert [only_member(p).capacity.limit for p in runtime.academic_search_providers] == [4, 2]
    assert (
        runtime.search_orchestrator._paper_aggregator
        is runtime.paper_search_orchestrator.aggregator
    )
    assert runtime.search_orchestrator._paper_resolver is runtime.paper_search_orchestrator.resolver
    assert runtime.search_orchestrator._store is runtime.paper_search_orchestrator.store

    await runtime.aclose()
    assert all(client.close_calls == 1 for client in clients)


async def test_academic_and_oa_credential_endpoints_and_contacts(tmp_path: Path) -> None:
    raw = _config()
    raw["web_providers"] = {}
    raw["academic_providers"] = {
        "crossref": {
            "enabled": True,
            "credentials": [
                {"api_url": f"https://crossref-{i}.test", "contact_email_env": env}
                for i, env in enumerate(("FIRST", "SECOND"))
            ],
        }
    }
    raw["oa_resolvers"] = {
        "unpaywall": {
            "enabled": True,
            "credentials": [
                {"api_url": f"https://oa-{i}.test", "contact_email_env": env}
                for i, env in enumerate(("FIRST", "SECOND"))
            ],
        }
    }
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host.startswith("oa-"):
            return httpx.Response(404)
        return httpx.Response(200, json={"message": {"items": []}})

    env = {
        "FIRST": "first@example.test",
        "SECOND": "second@example.test",
        "ENV_D": "example",
        "ENV_E": "example",
    }
    config = resolve_config(
        raw,
        build_default_registry(),
        env,
        academic_registry=build_default_academic_registry(),
        oa_resolver_registry=build_default_oa_resolver_registry(),
    )
    runtime = await Runtime.build(
        config,
        RuntimePaths.from_home(tmp_path),
        http_client_factory=lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    try:
        (provider,) = runtime.academic_search_providers
        assert runtime.oa_resolver is not None
        for _ in range(2):
            assert await provider.search("test query") == []
            assert await runtime.oa_resolver.resolve("10.1234/example") is None
        assert [r.url.host for r in requests] == [
            "crossref-0.test",
            "oa-0.test",
            "crossref-1.test",
            "oa-1.test",
        ]
        assert [r.url.params.get("email") or r.url.params["mailto"] for r in requests] == [
            env["FIRST"],
            env["FIRST"],
            env["SECOND"],
            env["SECOND"],
        ]
    finally:
        await runtime.aclose()
