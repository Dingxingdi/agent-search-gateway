import json
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.config import resolve_config
from agent_search_gateway.errors import ErrorCode, InputFailure
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.runtime import Runtime
from tests.runtime.test_runtime_assembly import _config, _environment


@pytest.mark.parametrize("provider", ["tinyfish", "firecrawl"])
async def test_keyword_selector_excludes_fetch_only_and_disabled_providers(
    tmp_path: Path, provider: str
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    registry = build_default_registry()
    paths = RuntimePaths.from_home(tmp_path)
    runtime = Runtime.build(
        resolve_config(_config(), registry, _environment()),
        paths,
        registry=registry,
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    original = runtime.web_search_providers
    try:
        with pytest.raises(InputFailure) as error:
            await runtime.search_orchestrator.keyword_search(
                "query", request_id="11111111", provider=provider
            )
        assert error.value.code is ErrorCode.BAD_REQUEST
        assert error.value.message == f"No enabled keyword-search provider matches '{provider}'"
        assert calls == []
        assert not list(paths.results_dir.glob("*"))
        assert runtime.web_search_providers is original
        assert [p.name for p in runtime.web_fetch_providers] == ["tavily", "tinyfish", "parallel"]
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("primary", None),
        (None, "global"),
        ("openai", None),
        ("primary", "search"),
    ],
)
async def test_llm_selectors_do_not_match_client_protocol_or_fetch_namespace(
    tmp_path: Path, provider: str | None, model: str | None
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    registry = build_default_registry()
    runtime = Runtime.build(
        resolve_config(_config(), registry, _environment()),
        RuntimePaths.from_home(tmp_path),
        registry=registry,
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        assert set(runtime.llm_clients) == {"primary", "secondary"}
        with pytest.raises(InputFailure) as error:
            await runtime.search_orchestrator.llm_search(
                "prompt", request_id="11111111", provider=provider, model=model
            )
        assert error.value.code is ErrorCode.BAD_REQUEST
        assert calls == []
        assert runtime.quotas.get_llm("primary").max_observed_in_use == 0
        assert runtime.quotas.get_llm("secondary").max_observed_in_use == 0
    finally:
        await runtime.aclose()


async def test_selected_runtime_invocation_keeps_payload_quota_and_assembly(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "## Result\nURL: https://example.com/result\nAbstract: Found"
                        }
                    }
                ]
            },
        )

    registry = build_default_registry()
    raw = _config()
    raw["search_llm"] = {
        "providers": [
            {"provider": "secondary", "model": "search", "extra_body": {"temperature": 0.3}}
        ]
    }
    resolved = resolve_config(raw, registry, _environment())
    runtime = Runtime.build(
        resolved,
        RuntimePaths.from_home(tmp_path),
        registry=registry,
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    web_search, web_fetch = runtime.web_search_providers, runtime.web_fetch_providers
    clients, quotas = runtime.llm_clients, runtime.quotas
    search = runtime.search_orchestrator
    invocations = search._llm_invocations
    stages = search._stages
    fallback = stages._judge
    try:
        path = Path(
            await search.llm_search(
                "prompt", request_id="11111111", provider="secondary", model="search"
            )
        )
        assert path.read_text() == '{"url":"https://example.com/result","abstract":"Found"}\n'
        assert len(calls) == 1 and calls[0].url.host == "llm-secondary.example.test"
        body = json.loads(calls[0].content)
        assert body["model"] == "search" and body["temperature"] == 0.3
        assert body["messages"][-1]["content"] == "prompt"
        assert "provider" not in body and "scope" not in body
        assert "secondary" not in str(body["messages"])
        assert runtime.quotas.get_llm("primary").max_observed_in_use == 0
        assert runtime.quotas.get_llm("secondary").max_observed_in_use == 1
        assert (
            runtime.web_search_providers is web_search and runtime.web_fetch_providers is web_fetch
        )
        assert runtime.llm_clients is clients and runtime.quotas is quotas
        assert search._llm_invocations is invocations and search._stages is stages
        assert stages._judge is fallback and fallback.provider == "primary"
        await search.llm_search("again", request_id="22222222")
        assert len(calls) == 2
    finally:
        await runtime.aclose()
