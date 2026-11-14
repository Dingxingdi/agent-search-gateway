import argparse
import json
import re
import shlex
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.cli import build_parser
from agent_search_gateway.config import load_toml, resolve_config, resolve_retry_policy
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.academic.defaults import (
    build_default_academic_registry,
    build_default_oa_resolver_registry,
)
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.runtime import Runtime
from agent_search_gateway.url_normalization import normalize_url

_ROOT = Path(__file__).parents[2]


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


def test_documented_agent_timeout_policy_matches_runtime() -> None:
    from agent_search_gateway import protocol

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
    other_socket_row = " ".join(rows["Other socket response wait"])
    assert re.findall(r"\b\d+\b", other_socket_row) == [
        str(int(protocol._RESPONSE_TIMEOUT_SECONDS))
    ]


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
    web = data.get("web_providers")
    if isinstance(web, dict):
        for value in web.values():
            if isinstance(value, dict):
                name = value.get("api_key_env")
                if isinstance(name, str):
                    names.add(name)
    llm = data.get("llm_providers")
    if isinstance(llm, dict):
        for value in llm.values():
            if isinstance(value, dict):
                name = value.get("api_key_env")
                if isinstance(name, str):
                    names.add(name)
    academic = data.get("academic_providers")
    if isinstance(academic, dict):
        for value in academic.values():
            if not isinstance(value, dict):
                continue
            for key in ("api_key_env", "contact_email_env"):
                name = value.get(key)
                if isinstance(name, str):
                    names.add(name)
    resolvers = data.get("oa_resolvers")
    if isinstance(resolvers, dict):
        for value in resolvers.values():
            if not isinstance(value, dict):
                continue
            for key in ("api_key_env", "contact_email_env"):
                name = value.get(key)
                if isinstance(name, str):
                    names.add(name)
    return {name: "x" for name in names}


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
    assert dict(parallel.options) == {
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
    }
    example_names = registered_names - {"zenrows", "decodo", "scrapingdog", "scrapegraphai"}
    assert example_names <= set(web)
    assert "apify" not in web
    assert "scrape" not in web

    by_name = {item.name: item for item in resolved.web.providers}
    assert dict(by_name["brightdata"].options) == {
        "api_url": "https://api.brightdata.com",
        "search_zone": "example_serp_zone",
        "fetch_zone": "example_unlocker_zone",
    }
    assert dict(by_name["scrape_do"].options) == {"api_url": "https://api.scrape.do"}

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
        api_key_env = raw.get("api_key_env")
        assert isinstance(api_key_env, str)
        assert api_key_env.strip()
        registration = registry.require(name)
        assert registration.requires_api_key is True
        configured = by_name[name]
        assert configured.api_key_env == api_key_env
        assert configured.secret is not None

    jina_raw = web["jina"]
    assert isinstance(jina_raw, dict)
    assert "api_key_env" not in jina_raw
    jina_registration = registry.require("jina")
    assert jina_registration.requires_api_key is False
    jina = by_name["jina"]
    assert jina.api_key_env is None
    assert jina.secret is None
    assert dict(jina.options) == {"api_url": "https://r.jina.ai"}

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

    assert dict(tinyfish.options) == {
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
        tinyfish["search_api_url"] = "https://tinyfish.example.test/proxy/search"
        tinyfish["fetch_api_url"] = "https://tinyfish.example.test/proxy/fetch"
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

    def client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    runtime = Runtime.build(
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
