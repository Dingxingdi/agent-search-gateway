import asyncio
import io
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import quote

import httpx
import pytest

from agent_search_gateway.academic.aggregator import PaperAggregator
from agent_search_gateway.cli import build_parser, run_command
from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.errors import ErrorCode, InputFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import (
    AgentInvocation,
    ErrorResponse,
    KeywordSearchRequest,
    LLMInvocation,
    LLMSearchRequest,
    PaperSearchRequest,
    RetryPolicy,
    SearchScope,
    ShutdownRequest,
    SuccessResponse,
    URLFetchRequest,
)
from agent_search_gateway.observability import SecretValue, configure_debug_logging
from agent_search_gateway.orchestrators.fetch import FetchOrchestrator
from agent_search_gateway.orchestrators.paper import PaperSearchOrchestrator
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.protocol import parse_response_frame, send_request
from agent_search_gateway.providers.contracts import (
    KeywordSearchProvider,
    PaperSearchHit,
    URLFetchCandidate,
)
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.web.brightdata import BrightDataAdapter
from agent_search_gateway.providers.web.doubao import DoubaoAdapter
from agent_search_gateway.providers.web.keenable import KeenableAdapter
from agent_search_gateway.providers.web.open_websearch import OpenWebSearchAdapter
from agent_search_gateway.providers.web.scraperapi import ScraperAPIAdapter
from agent_search_gateway.providers.web.tavily import TavilyAdapter
from agent_search_gateway.providers.web.youcom import YouComAdapter
from agent_search_gateway.providers.web.yuanbao import YuanbaoAdapter
from agent_search_gateway.providers.web.zhihu import ZhihuAdapter
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.scheduler.fetch import FetchScheduler
from agent_search_gateway.url_normalization import normalize_url
from agent_search_gateway.url_store import URLStore
from tests.support.acceptance import (
    FilteringAcceptanceRuntime,
    build_acceptance_runtime,
    build_filtering_acceptance_runtime,
)
from tests.support.agent_search import RecordingRunner, paper_markdown, web_markdown
from tests.support.fakes import (
    FakeAcademicSearchProvider,
    FakeKeywordSearchProvider,
    FakeLLMClient,
    FakeURLFetchProvider,
)
from tests.support.logging import structured_test_logger

_DAEMON_TIMEOUT_SECONDS = 2.0


async def _start_daemon(daemon: ForegroundDaemon) -> asyncio.Task[None]:
    """Start a daemon with a bounded readiness wait and surface startup failures."""
    daemon_task = asyncio.create_task(daemon.start())
    ready_task = asyncio.create_task(daemon.ready.wait())
    done, pending = await asyncio.wait(
        {daemon_task, ready_task},
        timeout=_DAEMON_TIMEOUT_SECONDS,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if ready_task in done and daemon.ready.is_set():
        return daemon_task

    ready_task.cancel()
    await asyncio.gather(ready_task, return_exceptions=True)
    if daemon_task in done:
        await daemon_task
        raise AssertionError("daemon exited before becoming ready")

    for pending_task in pending:
        pending_task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    raise AssertionError("daemon did not become ready")


@asynccontextmanager
async def _running_daemon(daemon: ForegroundDaemon) -> AsyncIterator[asyncio.Task[None]]:
    """Run a daemon and guarantee bounded cleanup when an assertion fails."""
    daemon_task = await _start_daemon(daemon)
    try:
        yield daemon_task
    finally:
        if not daemon_task.done():
            await asyncio.wait_for(daemon.stop_for_test(), timeout=_DAEMON_TIMEOUT_SECONDS)
        await asyncio.wait_for(asyncio.shield(daemon_task), timeout=_DAEMON_TIMEOUT_SECONDS)


async def test_real_socket_workflows_match_public_contract_without_network(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    runtime = build_acceptance_runtime(paths)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with _running_daemon(daemon) as daemon_task:
        keyword = await send_request(paths.socket_file, KeywordSearchRequest("find article"))
        assert isinstance(keyword, SuccessResponse)
        keyword_path = Path(keyword.text)
        assert keyword_path.exists()
        keyword_lines = keyword_path.read_text(encoding="utf-8").splitlines()
        assert [json.loads(line) for line in keyword_lines] == [
            {"url": "https://example.com/article", "abstract": "Keyword abstract"}
        ]
        assert all(set(json.loads(line)) == {"url", "abstract"} for line in keyword_lines)

        full = await send_request(
            paths.socket_file,
            URLFetchRequest("https://example.com/article", None),
        )
        assert full == SuccessResponse("Full article content")
        assert len(runtime.fetch_provider.calls) == 1

        focused = await send_request(
            paths.socket_file,
            URLFetchRequest("https://example.com/article", "pricing"),
        )
        assert focused == SuccessResponse("Focused summary: pricing")
        assert len(runtime.fetch_provider.calls) == 1

        llm = await send_request(paths.socket_file, LLMSearchRequest("find llm result"))
        assert isinstance(llm, SuccessResponse)
        llm_lines = Path(llm.text).read_text(encoding="utf-8").splitlines()
        assert [json.loads(line) for line in llm_lines] == [
            {"url": "https://example.com/llm", "abstract": "LLM abstract"}
        ]

        paper_stdout = io.StringIO()
        paper_stderr = io.StringIO()
        paper_args = build_parser().parse_args(["paper-search", "academic topic"])
        assert (
            await run_command(
                paper_args,
                paths,
                stdout=paper_stdout,
                stderr=paper_stderr,
            )
            == 0
        )
        assert paper_stderr.getvalue() == ""
        paper_path = Path(paper_stdout.getvalue().strip())
        assert paper_path.name.startswith("paper-")
        assert paper_path.name.endswith(".jsonl")
        assert len(paper_path.stem.removeprefix("paper-")) == 8
        paper_lines = [
            json.loads(line) for line in paper_path.read_text(encoding="utf-8").splitlines()
        ]
        assert [line["title"] for line in paper_lines] == [
            "Direct Academic Paper",
            "Unique CORE Paper",
        ]
        assert all("type" not in line for line in paper_lines)
        assert paper_lines[0]["sources"] == ["openalex", "core"]
        assert paper_lines[0]["pdf_url"] == "https://repository.example/paper.pdf"
        assert paper_lines[0]["is_open_access"] is True
        assert runtime.oa_resolver.calls == ["10.1000/acceptance-shared"]

        paper_fetch = await send_request(
            paths.socket_file,
            URLFetchRequest("https://example.com/paper", None),
        )
        assert paper_fetch == SuccessResponse("Full article content")
        assert [str(url) for url in runtime.fetch_provider.calls] == [
            "https://example.com/article",
            "https://example.com/paper",
        ]
        pdf_fetch = await send_request(
            paths.socket_file,
            URLFetchRequest("https://repository.example/paper.pdf", None),
        )
        assert pdf_fetch == ErrorResponse(
            ErrorCode.URL_NOT_ADMITTED,
            "URL was not admitted by search",
        )

        mixed_stdout = io.StringIO()
        mixed_stderr = io.StringIO()
        mixed_args = build_parser().parse_args(
            ["llm-search", "find web and papers", "--scope", "all"]
        )
        assert (
            await run_command(
                mixed_args,
                paths,
                stdout=mixed_stdout,
                stderr=mixed_stderr,
            )
            == 0
        )
        assert mixed_stderr.getvalue() == ""
        mixed_path = Path(mixed_stdout.getvalue().strip())
        assert mixed_path.name.startswith("llm-")
        mixed_lines = [
            json.loads(line) for line in mixed_path.read_text(encoding="utf-8").splitlines()
        ]
        assert [line["type"] for line in mixed_lines] == ["web", "paper"]
        assert mixed_lines[0]["url"] == "https://example.com/llm"
        assert mixed_lines[1]["title"] == "LLM Academic Paper"

        assert await send_request(paths.socket_file, ShutdownRequest()) == SuccessResponse(
            "Daemon stopped."
        )
        await daemon_task
        assert not paths.socket_file.exists()

    fresh_runtime = build_acceptance_runtime(paths)
    fresh_daemon = ForegroundDaemon(paths, runtime_factory=lambda: fresh_runtime)
    async with _running_daemon(fresh_daemon) as fresh_task:
        after_restart = await send_request(
            paths.socket_file,
            URLFetchRequest("https://example.com/article", None),
        )
        assert after_restart == ErrorResponse(
            ErrorCode.URL_NOT_ADMITTED,
            "URL was not admitted by search",
        )
        assert fresh_runtime.fetch_provider.calls == []

        assert await send_request(paths.socket_file, ShutdownRequest()) == SuccessResponse(
            "Daemon stopped."
        )
        await fresh_task
        assert not paths.socket_file.exists()


async def test_cli_waits_for_example_http_retry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the socket open through three slow attempts using virtual elapsed time."""
    import httpx

    from agent_search_gateway.config import load_toml, resolve_retry_policy
    from agent_search_gateway.providers.http import HttpJsonExecutor

    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    policy = resolve_retry_policy(load_toml(Path(__file__).parents[2] / "config.example.toml"))
    runtime = build_acceptance_runtime(paths)
    keyword_search = runtime.search_orchestrator.keyword_search
    workflow_done = asyncio.Event()
    attempts = 0
    clock_offset = 0.0
    loop = asyncio.get_running_loop()
    real_time = loop.time

    async def advance_time(seconds: float) -> None:
        """Advance deadlines and let real socket and timeout callbacks run."""
        nonlocal clock_offset
        clock_offset += seconds
        await asyncio.sleep(0.001)

    async def handler(request: httpx.Request) -> httpx.Response:
        """Spend the configured timeout on each attempt, then recover on the last."""
        nonlocal attempts
        attempts += 1
        assert request.extensions["timeout"]["read"] == policy.request_timeout_seconds
        await advance_time(policy.request_timeout_seconds)
        if attempts < policy.max_attempts:
            raise httpx.ReadTimeout("simulated slow provider", request=request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(client, policy, provider_name="slow", sleep=advance_time)

        async def slow_keyword_search(
            query: str, *, request_id: str, provider: str | None = None
        ) -> str:
            """Exercise real HTTP retries before the normal acceptance pipeline."""
            try:
                await executor.request_json("GET", "https://provider.example.test", stage="search")
                return await keyword_search(query, request_id=request_id, provider=provider)
            finally:
                workflow_done.set()

        monkeypatch.setattr(runtime.search_orchestrator, "keyword_search", slow_keyword_search)
        daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
        stdout, stderr = io.StringIO(), io.StringIO()
        with monkeypatch.context() as clock_patch:
            clock_patch.setattr(loop, "time", lambda: real_time() + clock_offset)
            async with _running_daemon(daemon):
                code = await run_command(
                    build_parser().parse_args(["keyword-search", "find article"]),
                    paths,
                    stdout=stdout,
                    stderr=stderr,
                )
                # Let the server finish even when the old client deadline fires early.
                await workflow_done.wait()

    assert attempts == 3
    assert clock_offset == pytest.approx(540.75)
    assert code == 0, stderr.getvalue()
    assert stderr.getvalue() == ""
    assert Path(stdout.getvalue().strip()).is_file()


async def _filtering_cli(paths: RuntimePaths, argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = await asyncio.wait_for(
        run_command(build_parser().parse_args(argv), paths, stdout=stdout, stderr=stderr),
        timeout=_DAEMON_TIMEOUT_SECONDS,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def _successful_path(outcome: tuple[int, str, str]) -> Path:
    code, stdout, stderr = outcome
    assert code == 0 and stderr == ""
    path = Path(stdout.removesuffix("\n"))
    assert path.is_absolute() and path.is_file()
    assert stdout == f"{path}\n"
    return path


def _filtering_snapshot(
    runtime: FilteringAcceptanceRuntime, paths: RuntimePaths
) -> tuple[object, ...]:
    urls = [f"https://example.com/keyword/{p.name}" for p in runtime.keyword_providers]
    urls += [f"https://example.com/paper/{p.name}" for p in runtime.academic_providers]
    urls += [f"https://example.com/llm/{name}" for name in runtime.llm_clients]
    return (
        tuple(tuple(p.calls) for p in runtime.keyword_providers),
        tuple(tuple(p.calls) for p in runtime.academic_providers),
        tuple(
            (name, repr(client.text_calls), repr(client.json_calls))
            for name, client in runtime.llm_clients.items()
        ),
        tuple(runtime.oa_resolver.calls),
        tuple(runtime.fetch_provider.calls),
        tuple((p.name, p.read_bytes()) for p in sorted(paths.results_dir.glob("*.jsonl"))),
        tuple(repr(runtime.store.get(normalize_url(url))) for url in urls),
    )


async def test_cli_filters_keyword_and_paper_without_persisting_selection(tmp_path: Path) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    runtime = build_filtering_acceptance_runtime(paths)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with _running_daemon(daemon):
        first = _successful_path(
            await _filtering_cli(paths, ["keyword-search", "selected", "--provider", " beta "])
        )
        assert [p.calls for p in runtime.keyword_providers] == [[], ["selected"]]
        assert first.read_text() == '{"url":"https://example.com/keyword/beta","abstract":"beta"}\n'
        second = _successful_path(await _filtering_cli(paths, ["keyword-search", "all"]))
        assert [p.calls for p in runtime.keyword_providers] == [["all"], ["selected", "all"]]
        assert [json.loads(line)["abstract"] for line in second.read_text().splitlines()] == [
            "alpha",
            "beta",
        ]
        paper = _successful_path(
            await _filtering_cli(paths, ["paper-search", "papers", "--provider", "arxiv"])
        )
        assert [p.calls for p in runtime.academic_providers] == [["papers"], []]
        [record] = [json.loads(line) for line in paper.read_text().splitlines()]
        assert record["sources"] == ["arxiv"] and "type" not in record
        assert runtime.oa_resolver.calls == ["10.1000/arxiv"]
    assert runtime.close_calls == 1


@pytest.mark.parametrize(
    ("flags", "indexes"),
    [
        (["--provider", "openai_main"], [0, 1]),
        (["--model", "gpt-5"], [0, 3]),
        (["--provider", "openai_main", "--model", "gpt-5"], [0]),
    ],
)
async def test_cli_llm_selectors_reach_real_search_pipeline(
    tmp_path: Path, flags: list[str], indexes: list[int]
) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    runtime = build_filtering_acceptance_runtime(paths)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with _running_daemon(daemon):
        path = _successful_path(await _filtering_cli(paths, ["llm-search", "prompt", *flags]))
        selected = [runtime.llm_invocations[index] for index in indexes]
        for name, client in runtime.llm_clients.items():
            assert [item for item, _ in client.text_calls] == [
                item for item in selected if item.provider == name
            ]
            assert all(messages[-1]["content"] == "prompt" for _, messages in client.text_calls)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert all(set(record) == {"url", "abstract"} for record in records)
        assert {record["abstract"] for record in records} == {item.provider for item in selected}
        await _filtering_cli(paths, ["llm-search", "unfiltered"])
        assert (
            sum(len(client.text_calls) for client in runtime.llm_clients.values())
            == len(selected) + 4
        )


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (
            ["keyword-search", "query", "--provider", "unknown"],
            "No enabled keyword-search provider matches 'unknown'",
        ),
        (
            ["paper-search", "query", "--provider", "unknown"],
            "No enabled paper-search provider matches 'unknown'",
        ),
        (
            ["llm-search", "prompt", "--provider", "openai_main", "--model", "deepseek-v3"],
            "No LLM search invocation matches provider 'openai_main' with model 'deepseek-v3'",
        ),
        (
            ["llm-search", "prompt", "--provider", "missing", "--model", "missing-model"],
            "No LLM search invocation matches provider 'missing' or model 'missing-model'",
        ),
        (
            ["llm-search", "prompt", "--provider", "missing", "--model", "gpt-5"],
            "No LLM search invocation matches provider 'missing'",
        ),
        (
            ["llm-search", "prompt", "--provider", "openai_main", "--model", "missing-model"],
            "No LLM search invocation matches model 'missing-model'",
        ),
    ],
)
async def test_cli_bad_selection_is_precise_and_has_no_search_side_effects(
    tmp_path: Path, argv: list[str], message: str
) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    runtime = build_filtering_acceptance_runtime(paths)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with _running_daemon(daemon):
        _successful_path(
            await _filtering_cli(paths, ["keyword-search", "baseline", "--provider", "beta"])
        )
        before = _filtering_snapshot(runtime, paths)
        assert await _filtering_cli(paths, argv) == (1, "", message + "\n")
        assert _filtering_snapshot(runtime, paths) == before


@pytest.mark.parametrize(
    ("search_request", "message"),
    [
        (KeywordSearchRequest("query", provider=" \t "), "Provider must not be empty"),
        (PaperSearchRequest("query", provider=""), "Provider must not be empty"),
        (LLMSearchRequest("prompt", provider=" "), "Provider must not be empty"),
        (LLMSearchRequest("prompt", model=" "), "Model must not be empty"),
        (
            KeywordSearchRequest("query", provider=" beta "),
            "No enabled keyword-search provider matches ' beta '",
        ),
    ],
)
async def test_direct_socket_blank_selectors_do_not_restore_full_search(
    tmp_path: Path,
    search_request: KeywordSearchRequest | PaperSearchRequest | LLMSearchRequest,
    message: str,
) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    runtime = build_filtering_acceptance_runtime(paths)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with _running_daemon(daemon):
        before = _filtering_snapshot(runtime, paths)
        assert await send_request(paths.socket_file, search_request) == ErrorResponse(
            ErrorCode.BAD_REQUEST, message
        )
        assert _filtering_snapshot(runtime, paths) == before


async def test_raw_invalid_selector_schema_never_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    runtime = build_filtering_acceptance_runtime(paths)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    dispatch = AsyncMock(wraps=daemon._dispatch)
    monkeypatch.setattr(daemon, "_dispatch", dispatch)
    payloads = [
        {"type": "keyword_search", "query": "query", "provider": None},
        {"type": "paper_search", "query": "query", "provider": 1},
        {"type": "llm_search", "prompt": "prompt", "model": False},
        {"type": "keyword_search", "query": "query", "model": "gpt-5"},
        {"type": "llm_search", "provider": "openai_main"},
        {"type": "url_fetch", "url": "https://example.com", "focus": None, "provider": "beta"},
        {"type": "shutdown", "model": "gpt-5"},
    ]
    async with _running_daemon(daemon):
        before = _filtering_snapshot(runtime, paths)
        reader, writer = await asyncio.open_unix_connection(path=paths.socket_file)
        try:
            writer.write(b"".join(json.dumps(payload).encode() + b"\n" for payload in payloads))
            await writer.drain()
            for _ in payloads:
                frame = await asyncio.wait_for(reader.readline(), timeout=_DAEMON_TIMEOUT_SECONDS)
                assert parse_response_frame(frame) == ErrorResponse(
                    ErrorCode.BAD_REQUEST, "Request fields do not match schema"
                )
        finally:
            writer.close()
            await writer.wait_closed()
        dispatch.assert_not_awaited()
        assert _filtering_snapshot(runtime, paths) == before


async def test_failing_direct_paper_search_writes_no_result_file(tmp_path: Path) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    runtime = build_acceptance_runtime(paths)
    failing_provider = next(
        provider for provider in runtime.academic_providers if provider.name == "crossref"
    )
    runtime.paper_search_orchestrator.providers = (failing_provider,)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with _running_daemon(daemon) as daemon_task:
        response = await send_request(paths.socket_file, PaperSearchRequest("failing topic"))
        assert response == ErrorResponse(
            ErrorCode.ALL_PROVIDERS_FAILED,
            "All academic search provider pipelines failed",
        )
        assert not list(paths.results_dir.glob("paper-*.jsonl"))

        assert await send_request(paths.socket_file, ShutdownRequest()) == SuccessResponse(
            "Daemon stopped."
        )
        await daemon_task


@pytest.mark.parametrize("wrapper_provider", ["brightdata", "scraperapi"])
@pytest.mark.parametrize("existing_body", [False, True], ids=["scheduler-fetch", "existing-merge"])
async def test_real_keyword_adapters_deduplicate_goto_and_preserve_target_fetch_identity(
    tmp_path: Path, wrapper_provider: str, existing_body: bool
) -> None:
    target = "https://example.org/article?signature=[REDACTED_SECRET]&token=[REDACTED_SECRET]"
    wrapper = "https://google.com/goto?url=" + quote(target, safe="")
    rejected = "https://example.org/empty?signature=[REDACTED_SECRET]"
    requests: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "brightdata.example.test":
            body = json.loads(request.content)
            requests.append(("brightdata", body["zone"], body["url"]))
            if body["zone"] == "fetch":
                assert body["url"] == target
                return httpx.Response(200, text="Fetched article body")
            return httpx.Response(
                200,
                json={
                    "organic": [
                        {
                            "link": wrapper if wrapper_provider == "brightdata" else target,
                            "description": "First abstract",
                        },
                        {"link": rejected},
                        {"link": "https://google.com/goto?url=[REDACTED_SECRET]"},
                    ]
                },
            )
        assert request.url.host == "scraperapi.example.test"
        assert request.url.path == "/structured/google/search"
        requests.append(("scraperapi", "search", request.url.params["query"]))
        return httpx.Response(
            200,
            json={
                "organic_results": [
                    {
                        "link": wrapper if wrapper_provider == "scraperapi" else target,
                        "snippet": "Second abstract",
                    }
                ]
            },
        )

    store = URLStore()
    url = normalize_url(target)
    if existing_body:
        store.admit(
            url, "Existing abstract", raw_content="Existing raw", content="Existing content"
        )
    quotas = ProviderQuotaManager(web_limits={"brightdata": 1, "scraperapi": 1}, llm_limits={})
    invocation = LLMInvocation("judge", "model", {})
    stages = LLMStages(
        {"judge": FakeLLMClient("judge")},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
    )
    log_file = tmp_path / "debug.log"
    session = configure_debug_logging(log_file, stderr=io.StringIO())
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            brightdata = BrightDataAdapter(
                name="brightdata",
                api_url="https://brightdata.example.test",
                search_zone="search",
                fetch_zone="fetch",
                secret=SecretValue("[REDACTED_SECRET]"),
                http_executor=HttpJsonExecutor(
                    client, RetryPolicy(1, 0, 1, 0), provider_name="brightdata"
                ),
            )
            scraperapi = ScraperAPIAdapter(
                name="scraperapi",
                api_url="https://scraperapi.example.test",
                secret=SecretValue("[REDACTED_SECRET]"),
                http_executor=HttpJsonExecutor(
                    client, RetryPolicy(1, 0, 1, 0), provider_name="scraperapi"
                ),
            )
            search = SearchOrchestrator(
                keyword_providers=[brightdata, scraperapi],
                llm_invocations=(),
                quotas=quotas,
                stages=stages,
                store=store,
                result_writer=ResultWriter(tmp_path / "results"),
            )
            path = Path(await search.keyword_search("query", request_id="ab000001"))
            abstract = "Existing abstract" if existing_body else "First abstract"
            assert [json.loads(line) for line in path.read_text().splitlines()] == [
                {"url": target, "abstract": abstract}
            ]
            snapshot = store.get(url)
            assert snapshot is not None
            assert snapshot.url == target and snapshot.abstract == abstract
            assert (snapshot.raw_content, snapshot.content) == (
                ("Existing raw", "Existing content") if existing_body else ("", "")
            )
            assert store.get(normalize_url(wrapper)) is None
            assert store.get(normalize_url(rejected)) is None

            fetch = FetchOrchestrator(
                store=store,
                scheduler=FetchScheduler([brightdata], quotas, stages),
                stages=stages,
            )
            with pytest.raises(InputFailure) as not_admitted:
                await fetch.url_fetch(wrapper)
            assert not_admitted.value.code is ErrorCode.URL_NOT_ADMITTED
            assert await fetch.url_fetch(target) == (
                "Existing content" if existing_body else "Fetched article body"
            )
            assert store.get(normalize_url(wrapper)) is None
            after = store.get(url)
            assert after is not None and after.url == target and after.abstract == abstract
            assert after.content == (
                "Existing content" if existing_body else "Fetched article body"
            )
    finally:
        session.close()

    # Every observed request is an original provider call; goto never resolves over HTTP.
    assert sorted((provider, stage) for provider, stage, _ in requests) == (
        [("brightdata", "search"), ("scraperapi", "search")]
        if existing_body
        else [("brightdata", "fetch"), ("brightdata", "search"), ("scraperapi", "search")]
    )
    logged = log_file.read_text()
    for event in ("candidate_accepted", "candidate_rejected", "body_skipped", "body_accepted"):
        assert f"event={event}" in logged
    assert "reason=duplicate" in logged
    assert "phase=adapter" in logged and "phase=staging" in logged
    assert "[REDACTED_SECRET]" not in logged
    assert "signature=" not in logged and "token=" not in logged
    if not existing_body:
        assert "event=provider_selected" in logged and "stage=fetch" in logged


async def test_zhihu_cli_selection_deduplicates_both_branch_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = "https://example.org/article"
    branches = {
        "/api/v1/content/zhihu_search": " HTTPS://EXAMPLE.org/article ",
        "/api/v1/content/global_search": target,
    }
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "zhihu.example.test"
        assert request.url.params["Query"] == "selected query"
        requests.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "Code": 0,
                "Data": {
                    "Items": [
                        {
                            "Url": branches[request.url.path],
                            "Title": "Article",
                            "ContentText": "Branch abstract",
                        }
                    ]
                },
            },
        )

    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path.parent / "zhihu-cli.sock",
        results_dir=tmp_path / "results",
    )
    store = URLStore()
    invocation = LLMInvocation("judge", "model", {})
    stages = LLMStages(
        {"judge": FakeLLMClient("judge")},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
    )
    other = FakeKeywordSearchProvider("other")
    other_search = AsyncMock(side_effect=AssertionError("non-selected provider was called"))
    monkeypatch.setattr(other, "search", other_search)
    logger, stream = structured_test_logger("acceptance.zhihu.cli")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            zhihu: KeywordSearchProvider = ZhihuAdapter(
                name="zhihu",
                api_url="https://zhihu.example.test",
                secret=SecretValue("synthetic-zhihu-key"),
                http_executor=HttpJsonExecutor(
                    client, RetryPolicy(1, 0, 1, 0), provider_name="zhihu"
                ),
            )
            providers: list[KeywordSearchProvider] = [zhihu, other]
            runtime = build_acceptance_runtime(paths)
            runtime.search_orchestrator = SearchOrchestrator(
                keyword_providers=providers,
                llm_invocations=(),
                quotas=ProviderQuotaManager(web_limits={"zhihu": 1, "other": 1}, llm_limits={}),
                stages=stages,
                store=store,
                result_writer=ResultWriter(paths.results_dir),
                logger=logger,
            )
            daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
            async with _running_daemon(daemon):
                result = _successful_path(
                    await _filtering_cli(
                        paths, ["keyword-search", "selected query", "--provider", "zhihu"]
                    )
                )
                records = [json.loads(line) for line in result.read_text().splitlines()]
                assert records == [{"url": target, "abstract": "Branch abstract"}]
                assert set(records[0]) == {"url", "abstract"}
                snapshot = store.get(normalize_url(target))
                assert snapshot is not None
                assert (snapshot.url, snapshot.abstract) == (target, "Branch abstract")
                assert (snapshot.raw_content, snapshot.content) == ("", "")
            assert runtime.close_calls == 1
    finally:
        for log_handler in logger.handlers[:]:
            logger.removeHandler(log_handler)
            log_handler.close()

    assert not paths.socket_file.exists()
    assert sorted(requests) == sorted(branches)
    other_search.assert_not_awaited()
    # Both candidates reach the real pipeline before its URL deduplication.
    completed = [
        line for line in stream.getvalue().splitlines() if "event=provider_completed " in line
    ]
    assert len(completed) == 1
    assert {"provider=zhihu", "hits=2", "results=2"} <= set(completed[0].split())
    assert "reason=duplicate" in stream.getvalue()


@pytest.mark.parametrize("global_outcome", ["hits", "empty", "failure"])
async def test_zhihu_terminal_event_matches_branch_outcomes(
    tmp_path: Path, global_outcome: str
) -> None:
    target = "https://example.org/partial"
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "zhihu.example.test"
        assert request.url.params["Query"] == "branch outcomes"
        branch = request.url.path.removeprefix("/api/v1/content/")
        assert branch in {"zhihu_search", "global_search"}
        requests.append(branch)
        if branch == "zhihu_search" or global_outcome == "failure":
            return httpx.Response(403, json={"message": "denied"})
        return httpx.Response(
            200,
            json={
                "Code": 0,
                "Data": {
                    "Items": (
                        [{"Url": target, "Title": "Article", "ContentText": "Surviving abstract"}]
                        if global_outcome == "hits"
                        else []
                    )
                },
            },
        )

    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path.parent / "zhihu-events.sock",
        results_dir=tmp_path / "results",
    )
    store = URLStore()
    invocation = LLMInvocation("judge", "model", {})
    stages = LLMStages(
        {"judge": FakeLLMClient("judge")},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
    )
    log_file = tmp_path / "debug.log"
    session = configure_debug_logging(log_file, stderr=io.StringIO())
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            zhihu: KeywordSearchProvider = ZhihuAdapter(
                name="zhihu",
                api_url="https://zhihu.example.test",
                secret=SecretValue("synthetic-zhihu-key"),
                http_executor=HttpJsonExecutor(
                    client, RetryPolicy(1, 0, 1, 0), provider_name="zhihu"
                ),
            )
            runtime = build_acceptance_runtime(paths)
            runtime.search_orchestrator = SearchOrchestrator(
                keyword_providers=[zhihu],
                llm_invocations=(),
                quotas=ProviderQuotaManager(web_limits={"zhihu": 1}, llm_limits={}),
                stages=stages,
                store=store,
                result_writer=ResultWriter(paths.results_dir),
            )
            daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
            async with _running_daemon(daemon):
                response = await send_request(
                    paths.socket_file, KeywordSearchRequest("branch outcomes", provider="zhihu")
                )
                if global_outcome == "failure":
                    assert response == ErrorResponse(
                        ErrorCode.ALL_PROVIDERS_FAILED,
                        "All keyword search provider pipelines failed",
                    )
                    assert not list(paths.results_dir.glob("*.jsonl"))
                else:
                    assert isinstance(response, SuccessResponse)
                    result = Path(response.text)
                    assert result.is_file()
                    records = [json.loads(line) for line in result.read_text().splitlines()]
                    assert records == (
                        [{"url": target, "abstract": "Surviving abstract"}]
                        if global_outcome == "hits"
                        else []
                    )
                assert (store.get(normalize_url(target)) is not None) == (global_outcome == "hits")
            assert runtime.close_calls == 1
    finally:
        session.close()

    assert not paths.socket_file.exists()
    assert sorted(requests) == ["global_search", "zhihu_search"]
    events = [
        set(line.split())
        for line in log_file.read_text().splitlines()
        if "provider=zhihu" in line.split()
    ]
    # Physical HTTP failures are separate from the logical provider terminal outcome.
    http_failures = [event for event in events if "event=http_failed" in event]
    assert len(http_failures) == (2 if global_outcome == "failure" else 1)
    failed_stages = {
        token for event in http_failures for token in event if token.startswith("stage=")
    }
    assert failed_stages == (
        {"stage=zhihu_search", "stage=global_search"}
        if global_outcome == "failure"
        else {"stage=zhihu_search"}
    )
    partials = [event for event in events if "event=provider_partial_failure" in event]
    terminals = [
        event for event in events if event & {"event=provider_completed", "event=provider_failed"}
    ]
    assert len(terminals) == 1
    assert "stage=search" in terminals[0]
    if global_outcome == "failure":
        assert partials == []
        assert "event=provider_failed" in terminals[0]
    else:
        assert len(partials) == 1
        assert {"stage=search", "branch=zhihu_search"} <= partials[0]
        assert "event=provider_completed" in terminals[0]
        count = 1 if global_outcome == "hits" else 0
        assert {f"hits={count}", f"results={count}"} <= terminals[0]


async def test_yuanbao_summary_does_not_skip_url_fetch(tmp_path: Path) -> None:
    target = "https://example.org/yuanbao"
    summary = "SearchPro summary is not the fetched article"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://yuanbao.example.test/SearchPro"
        assert json.loads(request.content) == {"Query": "article query"}
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "Response": {
                    "RequestId": "synthetic-request-id",
                    "Pages": [
                        json.dumps(
                            {
                                "url": target,
                                "title": "Article",
                                "content": summary,
                                "passage": "Secondary search passage",
                            }
                        )
                    ],
                }
            },
        )

    store = URLStore()
    quotas = ProviderQuotaManager(web_limits={"yuanbao": 1, "fetch": 1}, llm_limits={})
    invocation = LLMInvocation("judge", "model", {})
    stages = LLMStages(
        {"judge": FakeLLMClient("judge")},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
    )
    fetch_provider = FakeURLFetchProvider(
        "fetch",
        URLFetchCandidate(raw_content="Raw body from Fetch", content="Article body from Fetch"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        yuanbao: KeywordSearchProvider = YuanbaoAdapter(
            name="yuanbao",
            api_url="https://yuanbao.example.test",
            secret=SecretValue("synthetic-yuanbao-key"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(1, 0, 1, 0), provider_name="yuanbao"
            ),
        )
        search = SearchOrchestrator(
            keyword_providers=[yuanbao],
            llm_invocations=(),
            quotas=quotas,
            stages=stages,
            store=store,
            result_writer=ResultWriter(tmp_path / "results"),
        )
        result = Path(await search.keyword_search("article query", request_id="ab000003"))
        assert [json.loads(line) for line in result.read_text().splitlines()] == [
            {"url": target, "abstract": summary}
        ]
        url = normalize_url(target)
        before = store.get(url)
        assert before is not None
        assert before.abstract == summary
        assert (before.raw_content, before.content) == ("", "")
        assert fetch_provider.calls == []

        fetch = FetchOrchestrator(
            store=store,
            scheduler=FetchScheduler([fetch_provider], quotas, stages),
            stages=stages,
        )
        assert await fetch.url_fetch(target) == "Article body from Fetch"
        assert fetch_provider.calls == [url]
        after = store.get(url)
        assert after is not None
        assert after.abstract == summary
        assert (after.raw_content, after.content) == (
            "Raw body from Fetch",
            "Article body from Fetch",
        )
        assert after.content != summary

    assert len(requests) == 1


@pytest.mark.parametrize("kind", ["llm-web", "llm-paper", "agent-web", "agent-paper", "paper"])
async def test_google_goto_is_unchanged_outside_keyword_ingestion(
    tmp_path: Path, kind: str
) -> None:
    target = "https://example.org/target"
    wrapper = "https://google.com/goto?url=" + quote(target, safe="")
    store = URLStore()
    invocation = LLMInvocation("llm", "model", {})
    markdown = paper_markdown(wrapper) if "paper" in kind else web_markdown(wrapper)
    stages = LLMStages(
        {"llm": FakeLLMClient("llm", text_result=markdown)},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
        agent_runners={"agent": RecordingRunner(markdown)},
    )
    quotas = ProviderQuotaManager(web_limits={}, llm_limits={}, academic_limits={"openalex": 1})
    writer = ResultWriter(tmp_path / "results")
    if kind == "paper":
        academic = PaperSearchOrchestrator(
            providers=[
                FakeAcademicSearchProvider(
                    "openalex",
                    [
                        PaperSearchHit(
                            source="openalex",
                            source_id="W1",
                            title="Paper",
                            abstract="Abstract",
                            url=wrapper,
                        )
                    ],
                )
            ],
            quotas=quotas,
            aggregator=PaperAggregator(("openalex",)),
            resolver=None,
            store=store,
            result_writer=writer,
        )
        path = Path(await academic.paper_search("query", request_id="ab000002"))
    else:
        search = SearchOrchestrator(
            keyword_providers=(),
            llm_invocations=[invocation],
            agent_invocations=[AgentInvocation("agent")],
            quotas=quotas,
            stages=stages,
            store=store,
            result_writer=writer,
        )
        scope: SearchScope = "paper" if "paper" in kind else "web"
        if kind.startswith("agent"):
            result = await search.agent_search("query", request_id="ab000002", scope=scope)
        else:
            result = await search.llm_search("query", request_id="ab000002", scope=scope)
        path = Path(result)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 1 and records[0]["url"] == wrapper
    assert store.get(normalize_url(wrapper)) is not None
    assert store.get(normalize_url(target)) is None


_NEW_WEB_PROVIDERS = ("doubao", "youcom", "keenable", "open_websearch")
_NEW_WEB_TARGET = "https://example.org/new-provider-article"
_NEW_WEB_BODY = "Article body returned by Tavily fallback."


class _NewWebWorkflow:
    """Exercise real adapters and shared workflows with only external I/O replaced."""

    def __init__(self, tmp_path: Path, client: httpx.AsyncClient, name: str) -> None:
        self.store = URLStore()
        self.quotas = ProviderQuotaManager(web_limits={name: 1, "tavily": 1}, llm_limits={})
        self.llm = FakeLLMClient("llm", text_result=_NEW_WEB_BODY)
        self.stages = LLMStages(
            {"llm": self.llm},
            judge=LLMInvocation("llm", "judge"),
            safety=LLMInvocation("llm", "safety"),
            content_clean=LLMInvocation("llm", "clean"),
            focus_summary=LLMInvocation("llm", "focus"),
        )
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 1, 0), provider_name=name)
        api_url = f"https://{name}.example.test"
        provider: DoubaoAdapter | YouComAdapter | KeenableAdapter | OpenWebSearchAdapter
        if name == "open_websearch":
            provider = OpenWebSearchAdapter(
                name=name, api_url=api_url, http_executor=executor, engines=["bing", "duckduckgo"]
            )
        else:
            adapters: dict[
                str, type[DoubaoAdapter] | type[YouComAdapter] | type[KeenableAdapter]
            ] = {
                "doubao": DoubaoAdapter,
                "youcom": YouComAdapter,
                "keenable": KeenableAdapter,
            }
            adapter = adapters[name]
            provider = adapter(
                name=name,
                api_url=api_url,
                secret=SecretValue("test-credential"),
                http_executor=executor,
            )
        self.provider = provider
        tavily = TavilyAdapter(
            name="tavily",
            api_url="https://tavily.example.test",
            secret=SecretValue("test-credential"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 1, 0), provider_name="tavily"),
        )
        self.search_orchestrator = SearchOrchestrator(
            keyword_providers=[provider, tavily],
            llm_invocations=(),
            quotas=self.quotas,
            stages=self.stages,
            store=self.store,
            result_writer=ResultWriter(tmp_path / "results"),
        )
        self.scheduler = FetchScheduler([provider, tavily], self.quotas, self.stages)
        self.fetch_orchestrator = FetchOrchestrator(
            store=self.store, scheduler=self.scheduler, stages=self.stages
        )

    async def search(self, provider: str | None = None) -> list[dict[str, str]]:
        path = Path(
            await self.search_orchestrator.keyword_search(
                "article query", request_id="ab000012", provider=provider
            )
        )
        return [json.loads(line) for line in path.read_text().splitlines()]


def _new_web_search_payload(name: str) -> object:
    if name == "doubao":
        return {
            "Result": {
                "WebResults": [
                    {"Url": _NEW_WEB_TARGET, "Title": "Article", "Summary": "New abstract"}
                ]
            }
        }
    if name == "youcom":
        return {
            "results": {
                "web": [{"url": _NEW_WEB_TARGET, "title": "Article", "snippets": ["New abstract"]}]
            }
        }
    if name == "keenable":
        return {
            "results": [{"url": _NEW_WEB_TARGET, "title": "Article", "snippet": "New abstract"}]
        }
    assert name == "open_websearch"
    return {
        "status": "ok",
        "data": {
            "results": [{"url": _NEW_WEB_TARGET, "title": "Article", "description": "New abstract"}]
        },
    }


class _NewWebHTTP:
    def __init__(self, name: str, *, search_failure: bool = False) -> None:
        self.name = name
        self.search_failure = search_failure
        self.calls: list[tuple[str, str]] = []
        self.responses: list[httpx.Response] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        response = self._respond(request)
        self.responses.append(response)
        return response

    def _respond(self, request: httpx.Request) -> httpx.Response:
        name = request.url.host.removesuffix(".example.test") if request.url.host else ""
        assert name in {self.name, "tavily"}
        search = request.url.path in {"/search", "/v1/search", "/web_search"}
        stage = "search" if search else "fetch"
        self.calls.append((name, stage))
        if search:
            assert request.method == "POST"
            if name == self.name:
                if self.search_failure:
                    return httpx.Response(401, json={"error": "Unauthorized"})
                return httpx.Response(200, json=_new_web_search_payload(name))
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": _NEW_WEB_TARGET, "title": "Article", "content": "Old abstract"}
                    ]
                },
            )
        if name == "tavily":
            assert self.calls[-2] == (self.name, "fetch")
            assert request.method == "POST" and request.url.path == "/extract"
            assert json.loads(request.content) == {"urls": [_NEW_WEB_TARGET]}
            return httpx.Response(
                200, json={"results": [{"url": _NEW_WEB_TARGET, "raw_content": _NEW_WEB_BODY}]}
            )
        # Every first Fetch reaches the real adapter and fails at HTTP 200.
        if name == "doubao":
            assert request.method == "POST" and request.url.path == "/fetch"
            assert json.loads(request.content) == {"Url": _NEW_WEB_TARGET}
            return httpx.Response(200, json={"ResponseMetadata": {"Error": {"Code": "10413"}}})
        if name == "youcom":
            assert request.method == "POST" and request.url.path == "/v1/contents"
            assert json.loads(request.content)["urls"] == [_NEW_WEB_TARGET]
            return httpx.Response(200, json=[{"url": _NEW_WEB_TARGET, "markdown": "  "}])
        if name == "keenable":
            assert request.method == "GET" and request.url.path == "/v1/fetch"
            assert request.url.params["url"] == _NEW_WEB_TARGET
            return httpx.Response(200, json={"url": _NEW_WEB_TARGET, "content": ""})
        assert request.method == "POST" and request.url.path == "/fetch-web"
        assert json.loads(request.content) == {"url": _NEW_WEB_TARGET}
        return httpx.Response(200, json={"status": "ok", "data": {"content": ""}})


@pytest.mark.parametrize("name", _NEW_WEB_PROVIDERS)
async def test_new_provider_search_admits_url_for_fetch_fallback(tmp_path: Path, name: str) -> None:
    http = _NewWebHTTP(name)
    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
        workflow = _NewWebWorkflow(tmp_path, client, name)
        records = await workflow.search(provider=name)
        assert records == [{"url": _NEW_WEB_TARGET, "abstract": "New abstract"}]
        snapshot = workflow.store.get(normalize_url(records[0]["url"]))
        assert snapshot is not None
        assert snapshot.url == _NEW_WEB_TARGET
        assert (snapshot.raw_content, snapshot.content) == ("", "")
        assert await workflow.fetch_orchestrator.url_fetch(records[0]["url"]) == _NEW_WEB_BODY
        after = workflow.store.get(normalize_url(records[0]["url"]))
        assert after is not None and after.available
        assert (after.url, after.abstract, after.raw_content, after.content) == (
            records[0]["url"],
            records[0]["abstract"],
            _NEW_WEB_BODY,
            _NEW_WEB_BODY,
        )
        assert http.calls == [(name, "search"), (name, "fetch"), ("tavily", "fetch")]
        assert [response.status_code for response in http.responses] == [200, 200, 200]
        assert workflow.scheduler.provider_names == (name, "tavily")
        assert [call[0].model for call in workflow.llm.json_calls] == ["judge", "safety"]
        assert [call[0].model for call in workflow.llm.text_calls] == ["clean"]
        assert _NEW_WEB_BODY in workflow.llm.text_calls[0][1][-1]["content"]
        assert workflow.quotas.get_web(name).max_observed_in_use == 1
        assert workflow.quotas.get_web("tavily").max_observed_in_use == 1


@pytest.mark.parametrize("name", _NEW_WEB_PROVIDERS)
async def test_new_provider_failure_does_not_discard_other_search_results(
    tmp_path: Path, name: str
) -> None:
    http = _NewWebHTTP(name, search_failure=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
        workflow = _NewWebWorkflow(tmp_path, client, name)
        assert await workflow.search() == [{"url": _NEW_WEB_TARGET, "abstract": "Old abstract"}]
        snapshot = workflow.store.get(normalize_url(_NEW_WEB_TARGET))
        assert snapshot is not None and snapshot.abstract == "Old abstract"
        assert sorted(http.calls) == sorted([(name, "search"), ("tavily", "search")])
        assert not workflow.llm.text_calls


@pytest.mark.parametrize("name", _NEW_WEB_PROVIDERS)
async def test_new_provider_cannot_fetch_unadmitted_url(tmp_path: Path, name: str) -> None:
    http = _NewWebHTTP(name)
    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
        workflow = _NewWebWorkflow(tmp_path, client, name)
        await workflow.search(provider=name)
        before = list(http.calls)
        unknown = "https://example.org/unadmitted"
        with pytest.raises(InputFailure) as error:
            await workflow.fetch_orchestrator.url_fetch(unknown)
        assert error.value.code is ErrorCode.URL_NOT_ADMITTED
        assert http.calls == before == [(name, "search")]
        assert workflow.store.get(normalize_url(unknown)) is None
        assert not workflow.llm.json_calls and not workflow.llm.text_calls


@pytest.mark.parametrize("name", _NEW_WEB_PROVIDERS)
async def test_new_provider_reuses_prepared_content(tmp_path: Path, name: str) -> None:
    http = _NewWebHTTP(name)
    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
        workflow = _NewWebWorkflow(tmp_path, client, name)
        normalized = normalize_url(_NEW_WEB_TARGET)
        workflow.store.admit(
            normalized, "Existing abstract", raw_content="Existing raw", content="Existing content"
        )
        records = await workflow.search(provider=name)
        assert records == [{"url": _NEW_WEB_TARGET, "abstract": "Existing abstract"}]
        assert await workflow.fetch_orchestrator.url_fetch(records[0]["url"]) == "Existing content"
        snapshot = workflow.store.get(normalized)
        assert snapshot is not None and snapshot.available
        assert (snapshot.url, snapshot.abstract, snapshot.raw_content, snapshot.content) == (
            _NEW_WEB_TARGET,
            "Existing abstract",
            "Existing raw",
            "Existing content",
        )
        assert http.calls == [(name, "search")]
        assert [call[0].model for call in workflow.llm.json_calls] == ["safety"]
        assert not workflow.llm.text_calls
        assert workflow.quotas.get_web("tavily").max_observed_in_use == 0


@pytest.mark.parametrize("name", _NEW_WEB_PROVIDERS)
async def test_new_provider_and_existing_provider_share_url_deduplication(
    tmp_path: Path, name: str, caplog: pytest.LogCaptureFixture
) -> None:
    http = _NewWebHTTP(name)
    caplog.set_level("DEBUG")
    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
        workflow = _NewWebWorkflow(tmp_path, client, name)
        records = await workflow.search()
        assert len(records) == 1 and records[0]["url"] == _NEW_WEB_TARGET
        assert records[0]["abstract"] in {"New abstract", "Old abstract"}
        snapshot = workflow.store.get(normalize_url(_NEW_WEB_TARGET))
        assert snapshot is not None and snapshot.abstract == records[0]["abstract"]
        assert snapshot.raw_content == snapshot.content == ""
        assert sorted(http.calls) == sorted([(name, "search"), ("tavily", "search")])
        assert [response.status_code for response in http.responses] == [200, 200]
        summaries = [
            getattr(record, "gateway_fields", {})
            for record in caplog.records
            if getattr(record, "gateway_event", None) == "provider_candidates_summary"
            and getattr(record, "gateway_fields", {}).get("phase") == "adapter"
        ]
        assert sorted((item["provider"], item["accepted"]) for item in summaries) == sorted(
            [(name, 1), ("tavily", 1)]
        )
        duplicates = [
            getattr(record, "gateway_fields", {})
            for record in caplog.records
            if getattr(record, "gateway_event", None) == "candidate_rejected"
            and getattr(record, "gateway_fields", {}).get("reason") == "duplicate"
        ]
        assert len(duplicates) == 1
        assert not workflow.llm.json_calls and not workflow.llm.text_calls


async def test_new_provider_open_websearch_multi_engine_results_use_existing_staging(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[httpx.Request] = []
    second = "https://example.org/second-engine-article"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.host == "open_websearch.example.test"
        assert request.method == "POST" and request.url.path == "/search"
        assert json.loads(request.content) == {
            "query": "article query",
            "engines": ["bing", "duckduckgo"],
            "limit": 50,
        }
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {
                    "results": [
                        {
                            "url": _NEW_WEB_TARGET,
                            "title": "Article",
                            "description": "Bing abstract",
                            "engine": "bing",
                        },
                        {
                            "url": _NEW_WEB_TARGET,
                            "title": "Duplicate",
                            "description": "Duplicate abstract",
                            "engine": "duckduckgo",
                        },
                        {
                            "url": second,
                            "title": "Second article",
                            "description": "DuckDuckGo abstract",
                            "engine": "duckduckgo",
                        },
                    ]
                },
            },
        )

    caplog.set_level("DEBUG")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        workflow = _NewWebWorkflow(tmp_path, client, "open_websearch")
        records = await workflow.search(provider="open_websearch")
        assert records == [
            {"url": _NEW_WEB_TARGET, "abstract": "Bing abstract"},
            {"url": second, "abstract": "DuckDuckGo abstract"},
        ]
        for record in records:
            snapshot = workflow.store.get(normalize_url(record["url"]))
            assert snapshot is not None and snapshot.abstract == record["abstract"]
            assert snapshot.raw_content == snapshot.content == ""
        assert len(calls) == 1
        assert workflow.provider.name == "open_websearch"
        assert workflow.scheduler.provider_names == ("open_websearch", "tavily")
        assert workflow.quotas.get_web("open_websearch").max_observed_in_use == 1
        assert workflow.quotas.get_web("tavily").max_observed_in_use == 0
        fields = [getattr(record, "gateway_fields", {}) for record in caplog.records]
        assert any(item.get("reason") == "duplicate" for item in fields)
        assert any(item.get("phase") == "staging" for item in fields)
        identities = {item["provider"] for item in fields if "provider" in item}
        assert identities == {"open_websearch"}
