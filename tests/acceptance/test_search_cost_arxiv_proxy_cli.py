"""Offline CLI workflows using the production runtime and simulated upstream HTTP."""

import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from agent_search_gateway.config import resolve_config
from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.llm.stages import cheap_check
from agent_search_gateway.models import KeywordSearchRequest, SearchRecord
from agent_search_gateway.orchestrators import search
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.protocol import encode_request
from agent_search_gateway.providers.academic.defaults import build_default_academic_registry
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.request_ids import ResultKind
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.runtime import Runtime
from agent_search_gateway.url_normalization import normalize_url
from tests.acceptance.test_gateway_workflows import (
    _filtering_cli,
    _running_daemon,
    _successful_path,
)

_TARGET = "https://article.test/page"
_SEARCH = "https://firecrawl.test/v2/search"
_SCRAPE = "https://firecrawl.test/v2/scrape"
_EXTRACT = "https://tavily.test/extract"
_CHAT = "https://llm.test/v1/chat/completions"
_ARXIV = "https://arxiv.test/query"
_CROSSREF = "https://crossref.test/works"
_ATOM = Path(__file__).parents[1] / "fixtures/providers/academic/arxiv/search.xml"


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(tmp_path / "config.toml", tmp_path / "daemon.sock", tmp_path / "results")


@asynccontextmanager
async def _gateway(
    paths: RuntimePaths,
    transport: httpx.MockTransport,
    *,
    web: tuple[str, ...] = (),
    academic: dict[str, object] | None = None,
) -> AsyncIterator[Runtime]:
    raw = {
        "web_providers": {
            name: {
                "enable_search": True,
                "enable_fetch": True,
                "api_url": f"https://{name}.test",
                "api_key_env": "TEST_KEY",
            }
            for name in web
        },
        "academic_providers": academic or {},
        "llm_providers": {
            "chat": {
                "protocol": "openai",
                "api_endpoint": "chat_completions",
                "api_url": "https://llm.test",
                "api_key_env": "TEST_KEY",
            }
        },
        "global_default_llm": {"provider": "chat", "model": "default"},
        "fetch_llm": {
            stage: {"model": stage}
            for stage in ("judge", "content_clean", "safety", "focus_summary")
        },
        "retry": {"max_attempts": 1},
    }
    config = resolve_config(
        raw,
        build_default_registry(),
        {"TEST_KEY": "x"},
        academic_registry=build_default_academic_registry(),
    )
    clients: list[httpx.AsyncClient] = []

    def client_factory() -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=transport)
        clients.append(client)
        return client

    runtime = Runtime.build(config, paths, http_client_factory=client_factory)
    async with _running_daemon(ForegroundDaemon(paths, runtime_factory=lambda: runtime)):
        yield runtime
    assert all(client.is_closed for client in clients)
    assert not paths.socket_file.exists()


def _completion(request: httpx.Request) -> httpx.Response:
    model = json.loads(request.content)["model"]
    reply = {"judge": '{"ok":true}', "content_clean": "Clean article", "safety": '{"ok":true}'}[
        model
    ]
    return httpx.Response(
        200,
        json={
            "id": "offline",
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


def _transport(
    calls: list[httpx.Request], responses: dict[str, httpx.Response]
) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        endpoint = str(request.url.copy_with(query=None))
        return _completion(request) if endpoint == _CHAT else responses[endpoint]

    return httpx.MockTransport(respond)


def _metadata(**extra: str) -> httpx.Response:
    row = {"url": _TARGET, "title": "Article", "description": "Abstract", **extra}
    return httpx.Response(200, json={"success": True, "data": {"web": [row]}})


async def _keyword(paths: RuntimePaths, provider: str) -> None:
    path = _successful_path(
        await _filtering_cli(paths, ["keyword-search", "article", "--provider", provider])
    )
    assert json.loads(path.read_text()) == {"url": _TARGET, "abstract": "Abstract"}


def _assert_no_cached_body(runtime: Runtime) -> None:
    row = runtime.store.get(normalize_url(_TARGET))
    assert row is not None and row.available
    assert (row.raw_content, row.content) == ("", "")


def _chat_models(calls: list[httpx.Request]) -> list[str]:
    return [json.loads(r.content)["model"] for r in calls if str(r.url) == _CHAT]


@pytest.mark.parametrize("unexpected_body", [False, True])
async def test_firecrawl_metadata_search_then_explicit_fetch(
    paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch, unexpected_body: bool
) -> None:
    extra = (
        {"markdown": "Unrequested markdown", "rawHtml": "<p>Unrequested</p>"}
        if unexpected_body
        else {}
    )
    # A ready markdown body can be reused directly. Raw HTML needs the cleaning stage.
    page = {"rawHtml": "<p>Article</p>"} if unexpected_body else {"markdown": "Article"}
    content = "Clean article" if unexpected_body else "Article"
    stages = ["judge", "content_clean", "safety"] if unexpected_body else ["judge", "safety"]
    calls: list[httpx.Request] = []
    upstream = _transport(
        calls,
        {
            _SEARCH: _metadata(**extra),
            _SCRAPE: httpx.Response(200, json={"success": True, "data": page}),
        },
    )
    cheap = Mock(wraps=cheap_check)
    monkeypatch.setattr(search, "cheap_check", cheap)
    async with _gateway(paths, upstream, web=("firecrawl",)) as runtime:
        await _keyword(paths, "firecrawl")
        _assert_no_cached_body(runtime)
        cheap.assert_not_called()
        assert [(r.method, str(r.url)) for r in calls] == [("POST", _SEARCH)]
        assert json.loads(calls[0].content) == {
            "query": "article",
            "sources": ["web"],
            "limit": 10,
            "highlights": True,
        }
        fetch = ["url-fetch", _TARGET]
        assert await _filtering_cli(paths, fetch) == (0, f"{content}\n", "")
        assert json.loads(calls[1].content) == {
            "url": _TARGET,
            "formats": ["markdown"],
            "parsers": [],
        }
        assert _chat_models(calls) == stages
        row = runtime.store.get(normalize_url(_TARGET))
        assert row is not None and row.content == content
        assert row.raw_content == ("<p>Article</p>" if unexpected_body else "Article")
        assert await _filtering_cli(paths, fetch) == (0, f"{content}\n", "")
    assert [(r.method, str(r.url)) for r in calls if str(r.url) != _CHAT] == [
        ("POST", _SEARCH),
        ("POST", _SCRAPE),
    ]


async def test_firecrawl_base64_only_fetch_uses_existing_fallback(paths: RuntimePaths) -> None:
    calls: list[httpx.Request] = []
    marker = "UERGQUNDRVBUQU5DRQ=="
    upstream = _transport(
        calls,
        {
            _SEARCH: _metadata(),
            _SCRAPE: httpx.Response(200, json={"success": True, "data": {"rawBase64": marker}}),
            _EXTRACT: httpx.Response(
                200, json={"results": [{"url": _TARGET, "raw_content": "Tavily body"}]}
            ),
        },
    )
    async with _gateway(paths, upstream, web=("firecrawl", "tavily")) as runtime:
        await _keyword(paths, "firecrawl")
        _assert_no_cached_body(runtime)
        assert [str(r.url) for r in calls] == [_SEARCH]
        assert await _filtering_cli(paths, ["url-fetch", _TARGET]) == (0, "Clean article\n", "")
        row = runtime.store.get(normalize_url(_TARGET))
        assert row is not None and (row.raw_content, row.content) == (
            "Tavily body",
            "Clean article",
        )
    assert [(r.method, str(r.url)) for r in calls if str(r.url) != _CHAT] == [
        ("POST", _SEARCH),
        ("POST", _SCRAPE),
        ("POST", _EXTRACT),
    ]
    assert _chat_models(calls) == ["judge", "content_clean", "safety"]
    assert all(marker.encode() not in r.content for r in calls if str(r.url) == _CHAT)


async def test_tavily_search_body_still_runs_cheap_check_and_judge(
    paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://tavily.test/search"
    calls: list[httpx.Request] = []
    hit = {"url": _TARGET, "raw_content": "Tavily body", "content": "Abstract"}
    upstream = _transport(calls, {url: httpx.Response(200, json={"results": [hit]})})
    cheap = Mock(wraps=cheap_check)
    monkeypatch.setattr(search, "cheap_check", cheap)
    async with _gateway(paths, upstream, web=("tavily",)) as runtime:
        await _keyword(paths, "tavily")
        row = runtime.store.get(normalize_url(_TARGET))
        assert row is not None and row.raw_content == "Tavily body"
    cheap.assert_called_once_with("Tavily body")
    assert [str(r.url) for r in calls] == [url, _CHAT]
    assert _chat_models(calls) == ["judge"]


def _academic(options: dict[str, object]) -> dict[str, object]:
    return {
        "arxiv": {"enabled": True, "api_url": _ARXIV, **options},
        "crossref": {"enabled": True, "api_url": "https://crossref.test"},
    }


@pytest.mark.parametrize(
    ("query", "compiled", "custom"),
    [
        (
            "graph transformers",
            "(ti:graph OR abs:graph) AND (ti:transformers OR abs:transformers)",
            False,
        ),
        (
            '"graph neural networks"',
            '(ti:"graph neural networks" OR abs:"graph neural networks")',
            False,
        ),
        (
            '"graph neural networks" molecular',
            '(abs:"graph neural networks") AND (abs:molecular)',
            True,
        ),
    ],
)
async def test_arxiv_cli_uses_compiled_query_and_writes_paper_rows(
    paths: RuntimePaths, query: str, compiled: str, custom: bool
) -> None:
    calls: list[httpx.Request] = []
    upstream = _transport(calls, {_ARXIV: httpx.Response(200, text=_ATOM.read_text())})
    options: dict[str, object] = (
        {
            "query_fields": ["abs"],
            "max_results": 25,
            "sort_by": "submittedDate",
            "sort_order": "ascending",
        }
        if custom
        else {}
    )
    async with _gateway(paths, upstream, academic=_academic(options)):
        output = _successful_path(
            await _filtering_cli(paths, ["paper-search", query, "--provider", "arxiv"])
        )
    [request] = calls
    assert request.method == "GET" and str(request.url.copy_with(query=None)) == _ARXIV
    assert dict(request.url.params) == {
        "search_query": compiled,
        "max_results": "25" if custom else "10",
        "sortBy": "submittedDate" if custom else "relevance",
        "sortOrder": "ascending" if custom else "descending",
    }
    [row] = [json.loads(line) for line in output.read_text().splitlines()]
    assert row == {
        "title": "Example arXiv Paper",
        "authors": ["Alice Example", "Bob Example"],
        "abstract": "Example abstract.",
        "identifiers": {
            "doi": "10.1000/arxiv.example",
            "arxiv_id": "2401.12345",
            "semantic_scholar_id": "",
            "openalex_id": "",
            "dblp_key": "",
            "core_id": "",
        },
        "published_date": "2024-01-02",
        "updated_date": "2024-02-03",
        "url": "https://arxiv.org/abs/2401.12345v2",
        "pdf_url": "https://arxiv.org/pdf/2401.12345v2.pdf",
        "venue": "",
        "topics": ["cs.AI", "cs.LG"],
        "citation_counts": {},
        "is_open_access": None,
        "oa_status": "",
        "license": "",
        "sources": ["arxiv"],
    }


async def test_unrepresentable_arxiv_query_preserves_other_provider_results(
    paths: RuntimePaths,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[httpx.Request] = []
    item = {"DOI": "10.1000/crossref", "title": ["Crossref paper"]}
    upstream = _transport(
        calls, {_CROSSREF: httpx.Response(200, json={"message": {"items": [item]}})}
    )
    query = 'a"b'
    caplog.set_level("DEBUG", logger="agent_search_gateway.orchestrators.paper")
    async with _gateway(paths, upstream, academic=_academic({})):
        output = _successful_path(await _filtering_cli(paths, ["paper-search", query]))
    [failure] = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_failed"
    ]
    assert (failure["provider"], failure["error_type"]) == ("arxiv", "ProtocolFailure")
    [request] = calls
    assert request.method == "GET" and str(request.url.copy_with(query=None)) == _CROSSREF
    assert request.url.params["query"] == query
    [row] = [json.loads(line) for line in output.read_text().splitlines()]
    assert (row["sources"], row["title"], row["identifiers"]["doi"]) == (
        ["crossref"],
        "Crossref paper",
        "10.1000/crossref",
    )


async def test_client_disconnect_does_not_cancel_running_workflow(
    paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release, written = asyncio.Event(), asyncio.Event(), asyncio.Event()
    outputs: list[Path] = []
    original = ResultWriter.write_results

    def observe_write(
        writer: ResultWriter, kind: ResultKind, records: Iterable[SearchRecord], *, request_id: str
    ) -> Path:
        path = original(writer, kind, records, request_id=request_id)
        outputs.append(path)
        written.set()
        return path

    monkeypatch.setattr(ResultWriter, "write_results", observe_write)

    async def respond(request: httpx.Request) -> httpx.Response:
        assert (request.method, str(request.url)) == ("POST", _SEARCH)
        entered.set()
        await release.wait()
        return _metadata()

    async with _gateway(paths, httpx.MockTransport(respond), web=("firecrawl",)):
        _, writer = await asyncio.open_unix_connection(paths.socket_file)
        try:
            writer.write(encode_request(KeywordSearchRequest("article", provider="firecrawl")))
            await writer.drain()
            await asyncio.wait_for(entered.wait(), timeout=2)
            assert not written.is_set()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            finally:
                release.set()
        await asyncio.wait_for(written.wait(), timeout=2)
        [path] = outputs
        assert path.parent == paths.results_dir and path.name.startswith("keyword-")
        assert json.loads(path.read_text()) == {"url": _TARGET, "abstract": "Abstract"}
