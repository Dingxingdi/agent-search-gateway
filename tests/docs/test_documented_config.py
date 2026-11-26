import argparse
import json
import re
import shlex
import ssl
import tomllib
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.cli import build_parser
from agent_search_gateway.config import (
    ResolvedAcademicProviderConfig,
    ResolvedWebProviderConfig,
    load_toml,
    resolve_config,
    resolve_retry_policy,
)
from agent_search_gateway.models import LLMInvocation
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.academic.defaults import (
    build_default_academic_registry,
    build_default_oa_resolver_registry,
)
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.runtime import Runtime
from agent_search_gateway.url_normalization import normalize_url

_ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize("enable_templates", [False, True])
async def test_example_config_resolves_each_credential_with_stub_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enable_templates: bool
) -> None:
    from agent_search_gateway.doctor import run_doctor

    raw = load_toml(_ROOT / "config.example.toml")
    registry = build_default_registry()
    # Validate the unchanged source before exercising opt-in template variants.
    resolve_config(
        raw,
        registry,
        _stub_environment(raw),
        academic_registry=build_default_academic_registry(),
        oa_resolver_registry=build_default_oa_resolver_registry(),
    )
    if enable_templates:
        web = raw["web_providers"]
        assert isinstance(web, dict)
        for name, table in web.items():
            if not isinstance(table, dict):
                continue
            registration = registry.require(name)
            table["enable_search"] = registration.capabilities.search
            table["enable_fetch"] = registration.capabilities.fetch
        resolvers = raw["oa_resolvers"]
        assert isinstance(resolvers, dict)
        resolver = resolvers["unpaywall"]
        resolver["enabled"] = True
        resolver["credentials"][0]["contact_email_env"] = "DOCS_CONTACT_EMAIL"
    config = resolve_config(
        raw,
        registry,
        _stub_environment(raw),
        academic_registry=build_default_academic_registry(),
        oa_resolver_registry=build_default_oa_resolver_registry(),
    )
    enabled = [p for p in config.web.providers if p.enable_search or p.enable_fetch]
    assert all(provider.credentials for provider in enabled)
    assert all(provider.credentials for provider in config.academic.providers)
    assert all(provider.credentials for provider in config.llm.providers)
    if enable_templates:
        assert config.oa_resolver is not None and config.oa_resolver.credentials

    # Diagnose the on-disk example and an opt-in open-webSearch variant.
    # All applicable network traffic is local mock, with an explicit call count.
    paths = RuntimePaths.from_home(tmp_path)
    paths.config_file.parent.mkdir(parents=True)
    example = (_ROOT / "config.example.toml").read_text(encoding="utf-8")
    if enable_templates:
        disabled = "[web_providers.open_websearch]\nenable_search = false\nenable_fetch = false"
        enabled_block = "[web_providers.open_websearch]\nenable_search = true\nenable_fetch = true"
        assert example.count(disabled) == 1
        example = example.replace(disabled, enabled_block)
    paths.config_file.write_text(example, encoding="utf-8")
    observed: list[httpx.Request] = []

    def health(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        assert request.method == "GET" and request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok", "data": {"daemon": "running"}})

    from functools import partial

    monkeypatch.setattr(
        httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(health))
    )
    report = await run_doctor(paths, environ=_stub_environment(load_toml(paths.config_file)))
    assert report.exit_code == 0
    assert len(observed) == int(enable_templates)
    assert all(request.url.path == "/health" for request in observed)


async def test_free_fragment_doctor_and_unlimited_second_member(tmp_path: Path) -> None:
    from agent_search_gateway.doctor import run_doctor
    from tests.doctor._support import environment_name, write_valid_config

    paths = RuntimePaths.from_home(tmp_path)
    write_valid_config(paths.config_file)
    fragment = (_ROOT / "config.youcom-free.fragment.toml").read_text(encoding="utf-8")
    with paths.config_file.open("a", encoding="utf-8") as target:
        target.write("\n" + fragment)
        target.write(
            "\nmax_concurrency = 2\nmin_interval_seconds = 0.5\n"
            "rate_limits = [{ requests = 10, period_seconds = 1 }]\n"
        )
        target.write(
            '\n[[web_providers.youcom.credentials]]\napi_url = "https://api.you.com/mcp/free"\n'
        )
    raw = load_toml(paths.config_file)
    config = resolve_config(raw, build_default_registry(), {environment_name(): "present"})
    [provider] = config.web.providers
    assert len(provider.credentials) == 2
    first, second = provider.credentials
    assert first.api_key is second.api_key is None
    assert first.max_concurrency == 2
    assert first.rate_policy.min_interval_seconds == 0.5
    assert [(w.requests, w.period_seconds) for w in first.rate_policy.windows] == [(10, 1)]
    assert second.max_concurrency is None
    assert second.rate_policy.windows == ()
    assert second.rate_policy.min_interval_seconds is None
    report = await run_doctor(paths, environ={environment_name(): "present"})
    assert report.exit_code == 0
    for index in range(2):
        assert any(
            f"web_providers.youcom.credentials[{index}]" in check.message for check in report.checks
        )


def test_documented_rate_reference_covers_registered_non_llm_providers() -> None:
    registries = (
        build_default_registry(),
        build_default_academic_registry(),
        build_default_oa_resolver_registry(),
    )
    expected = {item.name for reg in registries for item in reg.list_in_registration_order()}
    reference = _ROOT / "docs/provider-rate-limits.md"
    assert reference.is_file()
    rows = [
        line.strip("|").split("|")
        for line in reference.read_text(encoding="utf-8").splitlines()
        if line.startswith("| `")
    ]
    names = [cells[0].strip().strip("`") for cells in rows]
    assert set(names) == expected
    assert len(rows) == len(expected) == 32
    for name, product, mode, upstream, local, sources, checked in rows:
        assert all(cell.strip() for cell in (product, mode, upstream, local)), name
        assert re.search(r"\]\(https://[^)]+\)", sources), name
        expected_date = "2026-11-21" if name.strip() == "`baiduqianfan`" else "2026-11-19"
        assert checked.strip() == expected_date, name


def test_example_input_limits_and_provider_rate_policies() -> None:
    raw = load_toml(_ROOT / "config.example.toml")
    assert raw.get("input_limits") == {
        "llm_request_body_bytes": 131072,
        "agent_prompt_bytes": 98304,
    }
    web = build_default_registry()
    academic = build_default_academic_registry()
    oa = build_default_oa_resolver_registry()
    resolved = resolve_config(
        raw, web, _stub_environment(raw), academic_registry=academic, oa_resolver_registry=oa
    )
    assert resolved.input_limits.llm_request_body_bytes == 131072
    assert resolved.input_limits.agent_prompt_bytes == 98304
    expected_windows = {
        "anysearch": [(10, 1)],
        "baiduqianfan": [(3, 1)],
        "brave": [(50, 1)],
        "brightdata": [(1000, 60)],
        "core": [(1, 10)],
        "crossref": [(1, 1)],
        "doubao": [(10, 1)],
        "exa": [(10, 1)],
        "firecrawl": [(10, 60)],
        "jina": [(20, 60)],
        "keenable": [(10, 1)],
        "linkup": [(10, 1)],
        "openalex": [(30, 1)],
        "parallel": [(600, 60)],
        "semantic_scholar": [(1, 1)],
        "serpapi": [(50, 3600)],
        "tavily": [(100, 60)],
        "tinyfish": [(30, 60), (500, 3600), (1000, 86400)],
        "youcom": [(10, 1)],
        "yuanbao": [(5, 1)],
    }
    intervals = {"arxiv": 3, "dblp": 2, "openalex": 0.5}
    capacities = {"arxiv": 1, "crossref": 1, "scrape_do": 5, "scraperapi": 5, "scrapingant": 1}
    providers: list[ResolvedWebProviderConfig | ResolvedAcademicProviderConfig] = [
        *resolved.web.providers,
        *resolved.academic.providers,
    ]
    for provider in providers:
        if not provider.credentials:
            continue
        policy = provider.credentials[0].rate_policy
        assert [(w.requests, w.period_seconds) for w in policy.windows] == expected_windows.get(
            provider.name, []
        ), provider.name
        assert policy.min_interval_seconds == intervals.get(provider.name), provider.name
        source = (
            raw["web_providers"]
            if provider in resolved.web.providers
            else raw["academic_providers"]
        )
        assert isinstance(source, dict)
        member = source[provider.name]["credentials"][0]
        expected_capacity = capacities.get(provider.name, member.get("max_concurrency"))
        assert provider.credentials[0].max_concurrency == expected_capacity, provider.name
        assert {"rate_limits", "min_interval_seconds", "max_concurrency"}.isdisjoint(
            provider.options
        )

    openalex = next(p for p in resolved.academic.providers if p.name == "openalex")
    assert openalex.credentials[0].api_key_env == "OPENALEX_API_KEY"
    assert openalex.credentials[0].api_key is not None
    assert resolved.oa_resolver is None
    resolvers = raw["oa_resolvers"]
    assert isinstance(resolvers, dict)
    unpaywall = resolvers["unpaywall"]
    assert unpaywall["enabled"] is False
    unpaywall["enabled"] = True
    unpaywall["credentials"][0]["contact_email_env"] = "UNPAYWALL_CONTACT_EMAIL"
    enabled = resolve_config(
        raw, web, _stub_environment(raw), academic_registry=academic, oa_resolver_registry=oa
    ).oa_resolver
    assert enabled is not None
    assert [(w.requests, w.period_seconds) for w in enabled.credentials[0].rate_policy.windows] == [
        (100000, 86400)
    ]
    assert "rate_limits" not in enabled.options


def test_agent_example_config_matches_documented_command() -> None:
    from agent_search_gateway.models import AgentInvocation

    raw = load_toml(_ROOT / "config.example.toml")
    resolved = resolve_config(raw, build_default_registry(), _stub_environment(raw))
    assert resolved.agent.search_invocations == (AgentInvocation("antigravity_main"),)
    [provider] = resolved.agent.providers
    assert (provider.name, provider.harness, provider.command) == (
        "antigravity_main",
        "antigravity",
        "agy",
    )
    assert (provider.max_concurrency, provider.timeout_seconds, provider.pass_env) == (1, 300.0, ())
    search = raw["search_agent"]
    assert isinstance(search, dict)
    entries = search["providers"]
    assert isinstance(entries, list) and len(entries) == 1
    entries[0]["model"] = "MODEL"
    pinned = resolve_config(raw, build_default_registry(), _stub_environment(raw))
    assert pinned.agent.search_invocations == (AgentInvocation("antigravity_main", "MODEL"),)

    readme = (_ROOT / "README.md").read_text()
    section = readme.split("## Agent search", 1)[1].split("\n## ", 1)[0]
    commands = re.findall(r"^agent-search-gateway agent-search .+$", section, re.MULTILINE)
    parsed = [build_parser().parse_args(shlex.split(command)[1:]) for command in commands]
    assert parsed[0].scope == "web" and parsed[0].model is None
    assert {args.scope for args in parsed} == {"web", "paper", "all"}
    assert any(args.provider == "antigravity_main" and args.model == "MODEL" for args in parsed)
    for document in ("docs/public-interface.md", "CHANGELOG.md"):
        text = (_ROOT / document).read_text()
        for token in (
            "agent-search",
            "--scope",
            "--provider",
            "--model",
            "agent-",
            "agent:",
            "fetch_llm",
            "search_agent",
        ):
            assert token in text


def test_documented_agent_timeout_policy_matches_config() -> None:
    raw = load_toml(_ROOT / "config.example.toml")
    resolved = resolve_config(raw, build_default_registry(), _stub_environment(raw))
    [default_provider] = resolved.agent.providers
    assert default_provider.timeout_seconds == 300.0
    providers = raw["agent_providers"]
    assert isinstance(providers, dict)
    provider = providers["antigravity_main"]
    assert isinstance(provider, dict)
    provider["timeout_seconds"] = 7200
    extended = resolve_config(raw, build_default_registry(), _stub_environment(raw))
    assert extended.agent.providers[0].timeout_seconds == 7200.0

    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Agent search", 1)[1].split("\n## ", 1)[0]
    rows = {
        cells[0].strip().strip("`"): cells[1:]
        for line in section.splitlines()
        if line.startswith("|")
        for cells in [[cell.strip() for cell in line.strip("|").split("|")]]
    }
    timeout_row = " ".join(rows["timeout_seconds"])
    assert set(re.findall(r"\b\d+\b", timeout_row)) == {
        str(int(default_provider.timeout_seconds))
    }, "Current Agent configuration must not document a numeric timeout cap"
    for constraint in ("positive", "finite"):
        assert re.search(rf"\b{constraint}\b", timeout_row, re.IGNORECASE)
    assert not re.search(r"\b(?:540|1140|1200)\b", section), (
        "Current Agent documentation must not retain the old fixed budgets"
    )


def test_readme_documents_request_scoped_provider_and_model_filters() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    heading = "### Request-scoped provider and model filters"
    assert heading in readme
    section = readme.split(heading, 1)[1].split("\n## ", 1)[0]
    examples = re.findall(r"^agent-search-gateway .+$", section, flags=re.MULTILINE)
    parser = build_parser()
    parsed = [parser.parse_args(shlex.split(example)[1:]) for example in examples]
    selections = {
        (args.command, args.provider, getattr(args, "model", None), getattr(args, "scope", None))
        for args in parsed
    }
    assert selections == {
        ("keyword-search", "tavily", None, None),
        ("paper-search", "arxiv", None, None),
        ("llm-search", "openai_main", None, "web"),
        ("llm-search", None, "gpt-5", "web"),
        ("llm-search", "openai_main", "gpt-5", "web"),
        ("llm-search", "openai_main", "gpt-5", "paper"),
        ("llm-search", "openai_main", "gpt-5", "all"),
    }


def test_request_selectors_are_documented_in_public_policy_and_changelog() -> None:
    public = (_ROOT / "docs/public-interface.md").read_text(encoding="utf-8")
    changelog = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "### Added" in changelog
    for flag in ("--provider", "--model"):
        assert flag in public and flag in changelog
    for command in ("keyword-search", "paper-search", "llm-search"):
        assert command in public and command in changelog


def _stub_environment(data: dict[str, object]) -> dict[str, str]:
    names: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.endswith("_env") and isinstance(child, str):
                    names.add(child)
                else:
                    collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(data)
    return {name: "docs@example.test" if "EMAIL" in name else "x" for name in names}


@pytest.mark.parametrize("custom", [False, True], ids=["example", "custom-direct"])
async def test_example_http_options_reach_runtime_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, custom: bool
) -> None:
    data = load_toml(_ROOT / "config.example.toml")
    web, academic, oa, llm = (
        data["web_providers"],
        data["academic_providers"],
        data["oa_resolvers"],
        data["llm_providers"],
    )
    assert all(isinstance(table, dict) for table in (web, academic, oa, llm))
    assert isinstance(web, dict) and isinstance(academic, dict)
    assert isinstance(oa, dict) and isinstance(llm, dict)
    firecrawl, zhihu, arxiv, unpaywall, chat = (
        web["firecrawl"],
        web["zhihu"],
        academic["arxiv"],
        oa["unpaywall"],
        llm["openai_main"],
    )
    assert firecrawl["use_proxy"] is True
    assert zhihu["use_proxy"] is False
    assert arxiv["use_proxy"] is unpaywall["use_proxy"] is chat["use_proxy"] is True
    assert {
        key: firecrawl[key]
        for key in ("search_limit", "search_highlights", "scrape_formats", "scrape_parsers")
    } == {
        "search_limit": 10,
        "search_highlights": True,
        "scrape_formats": ["markdown"],
        "scrape_parsers": [],
    }
    assert {
        key: arxiv[key] for key in ("query_fields", "max_results", "sort_by", "sort_order")
    } == {
        "query_fields": ["ti", "abs"],
        "max_results": 10,
        "sort_by": "relevance",
        "sort_order": "descending",
    }
    # Retain the example's policies; only one arXiv request is made per case.
    data["web_providers"] = {"firecrawl": firecrawl, "zhihu": zhihu}
    data["academic_providers"] = {"arxiv": arxiv}
    unpaywall["enabled"] = True
    unpaywall["credentials"][0]["contact_email_env"] = "TEST_CONTACT"
    if custom:
        for table in (firecrawl, arxiv, unpaywall, chat):
            table["use_proxy"] = False
        zhihu["use_proxy"] = True
        firecrawl.update(
            search_limit=7,
            search_highlights=False,
            scrape_formats=["rawHtml", "markdown"],
            scrape_parsers=["pdf", "image"],
        )
        arxiv.update(
            query_fields=["abs"],
            max_results=8,
            sort_by="lastUpdatedDate",
            sort_order="ascending",
        )
    config = resolve_config(
        data,
        build_default_registry(),
        _stub_environment(data),
        academic_registry=build_default_academic_registry(),
        oa_resolver_registry=build_default_oa_resolver_registry(),
    )
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    requests: list[tuple[httpx.Request, bool]] = []
    clients: list[httpx.AsyncClient] = []
    atom = (_ROOT / "tests/fixtures/providers/academic/arxiv/search.xml").read_text()
    target = normalize_url("https://example.test/article")

    def client_factory(
        *, limits: httpx.Limits, trust_env: bool = True, verify: ssl.SSLContext | bool = True
    ) -> httpx.AsyncClient:
        assert limits.max_connections is None
        if not trust_env:
            assert isinstance(verify, ssl.SSLContext)
            assert verify.verify_mode == ssl.CERT_REQUIRED and verify.check_hostname

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request, trust_env))
            if request.url.host == "api.firecrawl.dev":
                payload = (
                    {"web": [{"url": str(target), "description": "Abstract"}]}
                    if request.url.path == "/v2/search"
                    else {"markdown": "Article"}
                )
                return httpx.Response(200, json={"success": True, "data": payload})
            if request.url.host == "developer.zhihu.com":
                return httpx.Response(200, json={"Code": 0, "Data": {"Items": []}})
            if request.url.host == "export.arxiv.org":
                return httpx.Response(200, text=atom)
            if request.url.host == "api.unpaywall.org":
                return httpx.Response(404)
            assert request.url.host == "api.openai.com"
            return httpx.Response(200, json={"choices": [{"message": {"content": "Reply"}}]})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=trust_env, verify=verify
        )
        clients.append(client)
        return client

    runtime = await Runtime.build(
        config, RuntimePaths.from_home(tmp_path), http_client_factory=client_factory
    )
    try:
        providers = {provider.name: provider for provider in runtime.web_search_providers}
        hits = await providers["firecrawl"].search("graph transformers")
        assert [hit.url for hit in hits] == [str(target)]
        assert await providers["zhihu"].search("graph transformers") == []
        [fetch] = runtime.web_fetch_providers
        assert (await fetch.fetch(target)).content == "Article"
        [paper] = runtime.academic_search_providers
        assert len(await paper.search("graph transformers")) == 1
        assert runtime.oa_resolver is not None
        assert await runtime.oa_resolver.resolve("10.1000/example") is None
        assert (
            await runtime.llm_clients["openai_main"].complete_text(
                LLMInvocation("openai_main", "example-model"),
                [{"role": "user", "content": "Hello"}],
            )
            == "Reply"
        )
    finally:
        await runtime.aclose()
    assert len(clients) == 5 and all(client.is_closed for client in clients)
    assert len(requests) == 7
    assert all(
        mode is (custom if request.url.host == "developer.zhihu.com" else not custom)
        for request, mode in requests
    )
    by_path = {request.url.path: request for request, _ in requests}
    assert json.loads(by_path["/v2/search"].content) == {
        "query": "graph transformers",
        "sources": ["web"],
        "limit": 7 if custom else 10,
        "highlights": not custom,
    }
    assert json.loads(by_path["/v2/scrape"].content) == {
        "url": str(target),
        "formats": ["rawHtml", "markdown"] if custom else ["markdown"],
        "parsers": ["pdf", "image"] if custom else [],
    }
    assert dict(by_path["/api/query"].url.params) == {
        "search_query": "(abs:graph) AND (abs:transformers)"
        if custom
        else "(ti:graph OR abs:graph) AND (ti:transformers OR abs:transformers)",
        "max_results": "8" if custom else "10",
        "sortBy": "lastUpdatedDate" if custom else "relevance",
        "sortOrder": "ascending" if custom else "descending",
    }
    assert {"/api/v1/content/zhihu_search", "/api/v1/content/global_search"} <= by_path.keys()
    assert by_path["/v2/10.1000/example"].url.params["email"] == "x"
    assert json.loads(by_path["/v1/chat/completions"].content)["model"] == "example-model"


def test_example_config_loads_with_stub_secrets_and_readme_commands_match_cli_help() -> None:
    config_path = _ROOT / "config.example.toml"
    readme_path = _ROOT / "README.md"
    data = load_toml(config_path)
    registry = build_default_registry()
    academic_registry = build_default_academic_registry()
    resolver_registry = build_default_oa_resolver_registry()
    resolved = resolve_config(
        data,
        registry,
        _stub_environment(data),
        academic_registry=academic_registry,
        oa_resolver_registry=resolver_registry,
    )

    assert resolved.web.providers
    for configured in resolved.web.providers:
        registration = registry.get(configured.name)
        assert registration is not None
        if configured.enable_search:
            assert registration.capabilities.search
        if configured.enable_fetch:
            assert registration.capabilities.fetch

    parallel = next(item for item in resolved.web.providers if item.name == "parallel")
    assert parallel.enable_search is True
    assert parallel.enable_fetch is True
    assert dict(parallel.credentials[0].options) == {
        "api_url": "https://api.parallel.ai",
        "mode": "turbo",
        "search_fetch_policy": {
            "max_age_seconds": 3600,
            "timeout_seconds": 15,
            "disable_cache_fallback": False,
        },
        "extract_fetch_policy": {
            "max_age_seconds": 600,
            "timeout_seconds": 30,
            "disable_cache_fallback": True,
        },
    }

    readme = readme_path.read_text(encoding="utf-8")
    documented = set(
        re.findall(
            (
                r"^agent-search-gateway "
                r"(start|stop|doctor|keyword-search|paper-search|llm-search|agent-search|url-fetch)\b"
            ),
            readme,
            flags=re.MULTILINE,
        )
    )
    assert documented == {
        "start",
        "stop",
        "doctor",
        "keyword-search",
        "paper-search",
        "llm-search",
        "agent-search",
        "url-fetch",
    }

    parser = build_parser()
    action = next(item for item in parser._actions if isinstance(item, argparse._SubParsersAction))
    assert set(action.choices) == documented


def test_new_web_provider_examples_are_disabled_and_complete() -> None:
    data = load_toml(_ROOT / "config.example.toml")
    web = data["web_providers"]
    assert isinstance(web, dict)
    expected = {
        "baiduqianfan": ("https://qianfan.baidubce.com", "BAIDUQIANFAN_API_KEY"),
        "doubao": ("https://open.feedcoopapi.com/search_api", "DOUBAO_" + "API_KEY"),
        "youcom": ("https://ydc-index.io", "YDC_" + "API_KEY"),
        "keenable": ("https://api.keenable.ai", "KEENABLE_" + "API_KEY"),
        "open_websearch": ("http://127.0.0.1:3210", None),
    }
    assert set(expected) <= set(web)
    for name, (root, key_env) in expected.items():
        table = web[name]
        assert isinstance(table, dict)
        assert table["enable_search"] is False
        assert table["enable_fetch"] is False
        assert table["credentials"][0]["api_url"] == root
        if key_env is None:
            assert "api_key_env" not in table["credentials"][0]
        else:
            assert table["credentials"][0]["api_key_env"] == key_env


@pytest.mark.parametrize("name", ["doubao", "youcom", "keenable", "open_websearch"])
def test_new_web_example_resolves_without_new_credentials_and_can_be_enabled(name: str) -> None:
    data = load_toml(_ROOT / "config.example.toml")
    web = data["web_providers"]
    assert isinstance(web, dict)
    assert name in web
    environment = _stub_environment(data)
    for variable in ("DOUBAO_" + "API_KEY", "YDC_" + "API_KEY", "KEENABLE_" + "API_KEY"):
        environment.pop(variable, None)
    resolved = resolve_config(data, build_default_registry(), environment)
    configured = next(item for item in resolved.web.providers if item.name == name)
    assert configured.credentials == ()
    table = web[name]
    assert isinstance(table, dict)
    table["enable_search"] = True
    table["enable_fetch"] = True
    data["web_providers"] = {name: table}
    key_env = table["credentials"][0].get("api_key_env")
    if isinstance(key_env, str):
        environment[key_env] = "x"
    enabled = resolve_config(data, build_default_registry(), environment)
    [configured] = enabled.web.providers
    assert configured.enable_search and configured.enable_fetch
    assert (configured.credentials[0].api_key is not None) is (name != "open_websearch")
    assert configured.credentials[0].api_key_env == key_env


def test_zhihu_example_explains_account_quotas_and_explicit_limits() -> None:
    example = (_ROOT / "config.example.toml").read_text(encoding="utf-8")
    section = example.split("[web_providers.zhihu]", 1)[1].split("\n[", 1)[0]
    assert "QPS" in section and "account quotas" in section
    assert "default_max_concurrency" not in section
    web = load_toml(_ROOT / "config.example.toml")["web_providers"]
    assert isinstance(web, dict)
    assert {"max_concurrency", "rate_limits", "min_interval_seconds"}.isdisjoint(web["zhihu"])
    [member] = web["zhihu"]["credentials"]
    assert "rate_limits" not in member and "min_interval_seconds" not in member


def test_baiduqianfan_example_can_enable_search_with_its_configured_key() -> None:
    data = load_toml(_ROOT / "config.example.toml")
    web = data["web_providers"]
    assert isinstance(web, dict)
    assert "baiduqianfan" in web
    table = web["baiduqianfan"]
    data["web_providers"] = {"baiduqianfan": table}
    environment = _stub_environment(data)
    key_env = "BAIDUQIANFAN_API_KEY"
    environment.pop(key_env, None)
    registry = build_default_registry()
    [disabled] = resolve_config(data, registry, environment).web.providers
    assert disabled.credentials == ()

    table["enable_search"] = True
    environment[key_env] = "example-service-key"
    [enabled] = resolve_config(data, registry, environment).web.providers
    assert enabled.enable_search and not enabled.enable_fetch
    assert enabled.credentials[0].api_key_env == key_env
    assert (
        enabled.credentials[0].api_key is not None
        and enabled.credentials[0].api_key.reveal() == environment[key_env]
    )


@pytest.mark.parametrize(
    "source",
    [
        "README.md",
        "config.example.toml",
        "docs/youcom-free-mcp.md",
        "config.youcom-free.fragment.toml",
    ],
)
@pytest.mark.parametrize("existing_key", [False, True], ids=["no-key", "unused-environment-key"])
async def test_documented_youcom_mcp_example_builds_without_credentials(
    tmp_path: Path, source: str, existing_key: bool
) -> None:
    source_path = _ROOT / source
    assert source_path.is_file(), f"Missing free MCP configuration example: {source}"
    text = source_path.read_text(encoding="utf-8")
    if source_path.suffix == ".md":
        blocks = [
            block
            for block in re.findall(r"^```toml\n(.*?)^```", text, flags=re.MULTILINE | re.DOTALL)
            if "[web_providers.youcom]" in block and "https://api.you.com/mcp/free" in block
        ]
    elif source_path.name == "config.example.toml":
        match = re.search(
            r"^# BEGIN You.com free MCP replacement\n(.*?)^# END You.com free MCP replacement$",
            text,
            flags=re.MULTILINE | re.DOTALL,
        )
        blocks = (
            ["\n".join(line.removeprefix("# ") for line in match[1].splitlines())]
            if match is not None
            else []
        )
    else:
        blocks = [text]
    assert len(blocks) == 1, f"{source} must contain one free MCP replacement example"
    replacement = tomllib.loads(blocks[0])
    assert set(replacement) == {"web_providers"}
    assert set(replacement["web_providers"]) == {"youcom"}
    table = replacement["web_providers"]["youcom"]
    [member] = table["credentials"]
    assert member["api_url"] == "https://api.you.com/mcp/free"
    assert "api_key_env" not in member
    assert table["enable_search"] is True
    assert table["enable_fetch"] is True

    from tests.doctor._support import write_valid_config

    paths = RuntimePaths.from_home(tmp_path)
    write_valid_config(paths.config_file)
    data = load_toml(paths.config_file)
    data["web_providers"] = replacement["web_providers"]
    environment = _stub_environment(data)
    if existing_key:
        environment["YDC_API_KEY"] = "unused-environment-key"
    else:
        environment.pop("YDC_API_KEY", None)
    resolved = resolve_config(data, build_default_registry(), environment)
    [configured] = resolved.web.providers
    assert configured.name == "youcom"
    [credential] = configured.credentials
    assert credential.api_key_env is None
    assert credential.api_key is None
    assert dict(credential.options) == {"api_url": "https://api.you.com/mcp/free"}
    assert configured.enable_search and configured.enable_fetch
    requests: list[httpx.Request] = []

    def deny_http(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("Documented configuration assembly must not send HTTP")

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(deny_http))

    runtime = await Runtime.build(
        resolved, RuntimePaths.from_home(tmp_path), http_client_factory=client_factory
    )
    try:
        [search_provider] = runtime.web_search_providers
        [fetch_provider] = runtime.web_fetch_providers
        assert search_provider.name == fetch_provider.name == "youcom"
        assert id(search_provider) == id(fetch_provider)
    finally:
        await runtime.aclose()
    assert requests == []


def test_readme_documents_new_web_provider_configuration_contracts() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    for name in ("Doubao", "You.com", "Keenable", "open-webSearch"):
        assert f"| {name} | yes | yes |" in readme
    for token in (
        "https://open.feedcoopapi.com/search_api",
        "https://ydc-index.io",
        "https://api.keenable.ai",
        "http://127.0.0.1:3210",
        "DOUBAO_" + "API_KEY",
        "YDC_" + "API_KEY",
        "KEENABLE_" + "API_KEY",
        "[web_providers.open_websearch]",
        "engines",
        "ALLOWED_SEARCH_ENGINES",
        "DEFAULT_SEARCH_ENGINE",
        "partialFailures",
    ):
        assert token in readme


@pytest.mark.parametrize("explicit", [True, False], ids=["explicit-engines", "daemon-default"])
async def test_open_websearch_example_has_explicit_multi_engine_list(
    tmp_path: Path, explicit: bool
) -> None:
    data = load_toml(_ROOT / "config.example.toml")
    web = data["web_providers"]
    assert isinstance(web, dict)
    assert "open_websearch" in web
    table = web["open_websearch"]
    assert isinstance(table, dict)
    engines = [
        "bing",
        "duckduckgo",
        "brave",
        "startpage",
        "baidu",
        "sogou",
        "exa",
        "keenable",
        "csdn",
        "juejin",
        "hackernews",
    ]
    assert table["engines"] == engines
    assert len(engines) == len(set(engines))
    assert table["enable_search"] is False and table["enable_fetch"] is False
    if not explicit:
        table.pop("engines")
    table["enable_search"] = True
    data["web_providers"] = {"open_websearch": table}
    resolved = resolve_config(data, build_default_registry(), _stub_environment(data))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {
                    "results": [
                        {
                            "url": "https://example.com/docs",
                            "title": "Docs",
                            "description": "Reference",
                        }
                    ]
                },
            },
        )

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    runtime = await Runtime.build(
        resolved, RuntimePaths.from_home(tmp_path), http_client_factory=client_factory
    )
    try:
        [provider] = runtime.web_search_providers
        hits = await provider.search("documentation")
        assert [hit.url for hit in hits] == ["https://example.com/docs"]
    finally:
        await runtime.aclose()
    [request] = requests
    assert request.method == "POST"
    assert request.url == httpx.URL("http://127.0.0.1:3210/search")
    expected: dict[str, object] = {"query": "documentation"}
    if explicit:
        expected.update(engines=engines, limit=50)
    assert json.loads(request.content) == expected


def test_example_timeout_does_not_change_runtime_default() -> None:
    data = load_toml(_ROOT / "config.example.toml")
    assert resolve_retry_policy(data).request_timeout_seconds == 180.0
    assert resolve_retry_policy({}).request_timeout_seconds == 30.0


def test_example_provider_selection_omits_only_requested_templates() -> None:
    data = load_toml(_ROOT / "config.example.toml")
    web = data["web_providers"]
    assert isinstance(web, dict)
    expected = {
        "tavily",
        "firecrawl",
        "exa",
        "linkup",
        "brave",
        "anysearch",
        "tinyfish",
        "parallel",
        "brightdata",
        "scrape_do",
        "scraperapi",
        "scrapingant",
        "serpapi",
        "jina",
        "doubao",
        "youcom",
        "keenable",
        "open_websearch",
        "baiduqianfan",
        "yuanbao",
        "zhihu",
    }
    assert {name for name, table in web.items() if isinstance(table, dict)} == expected
    resolved = resolve_config(data, build_default_registry(), _stub_environment(data))
    assert {provider.name for provider in resolved.web.providers} == expected


def test_example_config_and_readme_document_all_new_provider_contracts() -> None:
    data = load_toml(_ROOT / "config.example.toml")
    registry = build_default_registry()
    resolved = resolve_config(data, registry, _stub_environment(data))
    web = data.get("web_providers")
    assert isinstance(web, dict)

    registered_names = {
        "brightdata",
        "scrape_do",
        "zenrows",
        "decodo",
        "scrapingdog",
        "scrapegraphai",
        "scraperapi",
        "scrapingant",
        "serpapi",
        "jina",
        "yuanbao",
        "zhihu",
    }
    example_names = registered_names - {"zenrows", "decodo", "scrapingdog", "scrapegraphai"}
    assert example_names <= set(web)
    assert "apify" not in web
    assert "scrape" not in web

    by_name = {item.name: item for item in resolved.web.providers}
    assert dict(by_name["brightdata"].credentials[0].options) == {
        "api_url": "https://api.brightdata.com",
        "search_zone": "example_serp_zone",
        "fetch_zone": "example_unlocker_zone",
    }
    assert dict(by_name["scrape_do"].credentials[0].options) == {"api_url": "https://api.scrape.do"}

    for name, api_url, expected_env_name in (
        ("yuanbao", "https://api.wsa.cloud.tencent.com", "YUANBAO_API_KEY"),
        ("zhihu", "https://developer.zhihu.com", "ZHIHU_ACCESS_SECRET"),
    ):
        assert dict(by_name[name].credentials[0].options) == {"api_url": api_url}
        assert by_name[name].credentials[0].api_key_env == expected_env_name

    expected_capabilities = {
        "brightdata": (True, True),
        "scrape_do": (True, True),
        "zenrows": (False, True),
        "decodo": (True, True),
        "scrapingdog": (True, True),
        "scrapegraphai": (True, True),
        "scraperapi": (True, True),
        "scrapingant": (False, True),
        "serpapi": (True, False),
        "jina": (False, True),
        "yuanbao": (True, False),
        "zhihu": (True, False),
    }
    for name, (search, fetch) in expected_capabilities.items():
        if name in example_names:
            configured = by_name[name]
            assert (configured.enable_search, configured.enable_fetch) == (search, fetch)
        registration = registry.get(name)
        assert registration is not None
        assert (
            registration.capabilities.search,
            registration.capabilities.fetch,
        ) == (search, fetch)
        assert registration.requires_api_key is (name != "jina")

    credentialless_names = {"jina"}
    credentialed_names = example_names - credentialless_names
    for name in credentialed_names:
        raw = web[name]
        assert isinstance(raw, dict)
        api_key_env = raw["credentials"][0].get("api_key_env")
        assert isinstance(api_key_env, str)
        assert api_key_env.strip()
        registration = registry.require(name)
        assert registration.requires_api_key is True
        configured = by_name[name]
        assert configured.credentials[0].api_key_env == api_key_env
        assert configured.credentials[0].api_key is not None

    jina_raw = web["jina"]
    assert isinstance(jina_raw, dict)
    assert "api_key_env" not in jina_raw["credentials"][0]
    jina_registration = registry.require("jina")
    assert jina_registration.requires_api_key is False
    jina = by_name["jina"]
    assert jina.credentials[0].api_key_env is None
    assert jina.credentials[0].api_key is None
    assert dict(jina.credentials[0].options) == {"api_url": "https://r.jina.ai"}

    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    expected_rows = {
        "Bright Data": ("yes", "yes"),
        "Scrape.do": ("yes", "yes"),
        "ZenRows": ("no", "yes"),
        "Decodo": ("yes", "yes"),
        "ScrapingDog": ("yes", "yes"),
        "ScrapeGraphAI": ("yes", "yes"),
        "ScraperAPI": ("yes", "yes"),
        "ScrapingAnt": ("no", "yes"),
        "SerpApi": ("yes", "no"),
        "Jina Reader": ("no", "yes"),
        "Yuanbao": ("yes", "no"),
        "Zhihu": ("yes", "no"),
        "Baidu Qianfan": ("yes", "no"),
    }
    for display_name, (search_label, fetch_label) in expected_rows.items():
        assert f"| {display_name} | {search_label} | {fetch_label} |" in readme
    assert "Only providers that require credentials use `api_key_env`" in readme
    assert "Each actual Jina Reader request sends `X-No-Cache: true`" in readme
    assert "does not bypass content already prepared by the gateway" in readme
    assert "no force-refresh option" in readme
    assert "| Apify |" not in readme


def test_example_tinyfish_uses_documented_public_endpoints() -> None:
    data = load_toml(_ROOT / "config.example.toml")
    resolved = resolve_config(data, build_default_registry(), _stub_environment(data))
    tinyfish = next(item for item in resolved.web.providers if item.name == "tinyfish")

    assert dict(tinyfish.credentials[0].options) == {
        "search_api_url": "https://api.search.tinyfish.ai",
        "fetch_api_url": "https://api.fetch.tinyfish.ai",
    }


def test_serp_wrapper_limitation_is_visible_in_readme_and_provider_configuration() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    heading = "#### Known provider limitations"
    assert heading in readme
    limitations = readme.split(heading, 1)[1].split("\n## ", 1)[0]
    for marker in (
        "ScraperAPI",
        "Scrape.do",
        "google.com/goto",
        "deduplication",
        "url-fetch",
        "enable_search = false",
        "enable_fetch",
        "https://github.com/Dingxingdi/agent-search-gateway/issues/74",
    ):
        assert marker in limitations

    example = (_ROOT / "config.example.toml").read_text(encoding="utf-8")
    for provider in ("scraperapi", "scrape_do"):
        block = example.split(f"[web_providers.{provider}]\n", 1)[1].split("\n[", 1)[0]
        assert "google.com/goto" in block
        assert "README.md#known-provider-limitations" in block


@pytest.mark.parametrize("custom", [False, True], ids=["example", "custom"])
async def test_tinyfish_example_and_custom_endpoints_reach_the_runtime_http_boundary(
    tmp_path: Path,
    custom: bool,
) -> None:
    data = load_toml(_ROOT / "config.example.toml")
    web = data["web_providers"]
    assert isinstance(web, dict)
    tinyfish = web["tinyfish"]
    assert isinstance(tinyfish, dict)
    if custom:
        tinyfish["credentials"][0]["search_api_url"] = "https://tinyfish.example.test/proxy/search"
        tinyfish["credentials"][0]["fetch_api_url"] = "https://tinyfish.example.test/proxy/fetch"
    data["web_providers"] = {"tinyfish": tinyfish}
    resolved = resolve_config(data, build_default_registry(), _stub_environment(data))
    requests: list[httpx.Request] = []
    target = normalize_url("https://example.test/article")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"results": [{"url": str(target), "title": "Title", "snippet": "Abstract"}]},
                request=request,
            )
        return httpx.Response(
            200,
            json={"results": [{"url": str(target), "text": "Fetched content"}]},
            request=request,
        )

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    runtime = await Runtime.build(
        resolved, RuntimePaths.from_home(tmp_path), http_client_factory=client_factory
    )
    try:
        [search_provider] = runtime.web_search_providers
        [fetch_provider] = runtime.web_fetch_providers
        assert id(search_provider) == id(fetch_provider)
        hits = await search_provider.search("hello world & docs")
        assert len(hits) == 1 and hits[0].url == str(target)
        candidate = await fetch_provider.fetch(target)
        assert candidate.content == "Fetched content"
    finally:
        await runtime.aclose()

    search_request, fetch_request = requests
    search_endpoint = (
        "https://tinyfish.example.test/proxy/search" if custom else "https://api.search.tinyfish.ai"
    )
    fetch_endpoint = (
        "https://tinyfish.example.test/proxy/fetch" if custom else "https://api.fetch.tinyfish.ai"
    )
    assert search_request.method == "GET"
    assert search_request.url.copy_with(query=None) == httpx.URL(search_endpoint)
    assert dict(search_request.url.params) == {"query": "hello world & docs"}
    assert search_request.headers["X-API-Key"] == "x"
    assert fetch_request.method == "POST"
    assert fetch_request.url == httpx.URL(fetch_endpoint)
    assert fetch_request.headers["X-API-Key"] == "x"
    assert json.loads(fetch_request.content) == {
        "urls": [str(target)],
        "format": "markdown",
        "links": False,
        "image_links": False,
    }
