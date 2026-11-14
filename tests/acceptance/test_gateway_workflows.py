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
from agent_search_gateway.providers.contracts import PaperSearchHit
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.web.brightdata import BrightDataAdapter
from agent_search_gateway.providers.web.scraperapi import ScraperAPIAdapter
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
from tests.support.fakes import FakeAcademicSearchProvider, FakeLLMClient

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
