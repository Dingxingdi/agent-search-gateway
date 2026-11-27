"""Cross-frontend search and Fetch against a real gateway daemon."""

import asyncio
import io
import json
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

import httpx
import pytest
from mcp.client import Client

from agent_search_gateway import runtime as runtime_module
from agent_search_gateway.cli import build_parser, run_command
from agent_search_gateway.config import resolve_config
from agent_search_gateway.credential_pool import CredentialPool
from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.errors import UNAVAILABLE_MESSAGE, ErrorCode, ExecutionFailure
from agent_search_gateway.mcp.adapter import GatewayAdapter
from agent_search_gateway.mcp.server import create_http_app, create_server
from agent_search_gateway.models import (
    AgentInvocation,
    KeywordSearchRequest,
    LLMInvocation,
    Request,
    Response,
)
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.contracts import AgentTextRunner, ChatMessage
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.rate_limit import make_rate_gate
from agent_search_gateway.runtime import Runtime
from tests.acceptance.test_gateway_workflows import (
    _filtering_cli,
    _running_daemon,
    _successful_path,
)
from tests.acceptance.test_search_cost_arxiv_proxy_cli import _gateway, _metadata
from tests.mcp._support import Frontend, captured_stdio, http_frontend, http_session, running_http
from tests.mcp.test_stdio import _text
from tests.orchestrators.test_llm_provider_model_filtering import RecordingClient
from tests.orchestrators.test_llm_provider_model_filtering import _build as build_scoped_llm_search
from tests.support.acceptance import (
    FilteringAcceptanceRuntime,
    build_acceptance_runtime,
    build_filtering_acceptance_runtime,
)
from tests.support.agent_search import agent_orchestrator
from tests.support.controlled import ManualClock

_ARTICLE = "Clean article for public output."
_PRIVATE_BODY = "Private synthetic article body with enough text for evaluation."
_UNSAFE_BODY = "Private synthetic unsafe article that must never reach a client."
_UNSAFE_CLEAN = "Private cleaned text rejected by the safety stage."
_STAGES = ("judge", "content_clean", "safety", "focus_summary")


@pytest.fixture
async def web_daemon(mcp_home: Path) -> AsyncIterator[tuple[RuntimePaths, list[str]]]:
    paths = RuntimePaths.from_home(mcp_home)
    events: list[str] = []
    config = resolve_config(
        {
            "web_providers": {
                "tavily": {
                    "enable_search": True,
                    "enable_fetch": True,
                    "credentials": [{"api_key_env": "TEST_KEY", "api_url": "https://tavily.test"}],
                },
            },
            "llm_providers": {
                "chat": {
                    "protocol": "openai",
                    "api_endpoint": "chat_completions",
                    "credentials": [{"api_key_env": "TEST_KEY", "api_url": "https://llm.test"}],
                },
            },
            "global_default_llm": {"provider": "chat", "model": "default"},
            "fetch_llm": {stage: {"model": stage} for stage in _STAGES},
            "retry": {"max_attempts": 1},
        },
        build_default_registry(),
        {"TEST_KEY": "offline-credential"},
    )

    def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.host == "tavily.test":
            if request.url.path == "/search":
                events.append("search")
                query = payload["query"]
                hit = {"url": f"https://example.test/{query}", "content": "Keyword abstract"}
                if query == "natural":
                    hit["raw_content"] = _PRIVATE_BODY
                return httpx.Response(200, json={"results": [hit]})
            assert request.url.path == "/extract"
            events.append("fetch")
            (url,) = payload["urls"]
            raw = _UNSAFE_BODY if url.endswith("/unsafe") else _PRIVATE_BODY
            return httpx.Response(200, json={"results": [{"url": url, "raw_content": raw}]})
        assert request.url.host == "llm.test"
        stage = payload["model"]
        events.append(stage)
        prompt = payload["messages"][1]["content"]
        reply = {
            "judge": '{"ok":true}',
            "content_clean": _UNSAFE_CLEAN if _UNSAFE_BODY in prompt else _ARTICLE,
            "safety": json.dumps({"ok": _UNSAFE_CLEAN not in prompt}),
            "focus_summary": "Focused article.",
        }[stage]
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    runtime = await Runtime.build(
        config,
        paths,
        http_client_factory=lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    async with _running_daemon(ForegroundDaemon(paths, runtime_factory=lambda: runtime)):
        yield paths, events


async def _search_file_text(
    client: Client, paths: RuntimePaths, kind: str, **arguments: str
) -> str:
    pattern = f"{kind}-*.jsonl"
    before = set(paths.results_dir.glob(pattern))
    response = await client.call_tool(f"{kind}_search", arguments)
    files = set(paths.results_dir.glob(pattern)) - before
    assert len(files) == 1
    text = files.pop().read_bytes().decode("utf-8")
    assert _text(response) == text
    return text


async def test_keyword_admission_and_fetch_cache_across_cli_stdio_http(
    mcp_home: Path,
    web_daemon: tuple[RuntimePaths, list[str]],
) -> None:
    paths, events = web_daemon
    with captured_stdio(mcp_home) as capture:
        async with asyncio.timeout(15), capture.client as stdio, http_frontend(mcp_home) as http:
            cli_url = "https://example.test/cli"
            output = _successful_path(await _filtering_cli(paths, ["keyword-search", "cli"]))
            assert json.loads(output.read_text(encoding="utf-8")) == {
                "url": cli_url,
                "abstract": "Keyword abstract",
            }
            assert _text(await stdio.call_tool("url_fetch", {"url": cli_url})) == _ARTICLE
            work = ["search", "fetch", "judge", "content_clean", "safety"]
            assert events == work

            http_url = "https://example.test/http"
            text = await _search_file_text(http, paths, "keyword", query="http")
            assert json.loads(text) == {"url": http_url, "abstract": "Keyword abstract"}
            assert await _filtering_cli(paths, ["url-fetch", http_url]) == (0, _ARTICLE + "\n", "")
            assert events == work * 2
            for client in (stdio, http):
                assert _text(await client.call_tool("url_fetch", {"url": http_url})) == _ARTICLE
            assert events == work * 2


async def test_natural_body_deferred_stages_run_only_on_fetch(
    mcp_home: Path,
    web_daemon: tuple[RuntimePaths, list[str]],
) -> None:
    paths, events = web_daemon
    async with asyncio.timeout(15), http_frontend(mcp_home) as client:
        text = await _search_file_text(client, paths, "keyword", query="natural")
        url = "https://example.test/natural"
        assert json.loads(text) == {"url": url, "abstract": "Keyword abstract"}
        assert events == ["search"]
        assert await _filtering_cli(paths, ["url-fetch", url, "subject"]) == (
            0,
            "Focused article.\n",
            "",
        )
        assert events == ["search", "judge", "content_clean", "safety", "focus_summary"]


async def test_unavailable_fetch_cache_is_distinct_from_failed_admission(
    mcp_home: Path,
    web_daemon: tuple[RuntimePaths, list[str]],
) -> None:
    paths, events = web_daemon
    async with asyncio.timeout(15), http_frontend(mcp_home) as client:
        url = "https://example.test/unsafe"
        denied = _text(await client.call_tool("url_fetch", {"url": url}), error=True)
        assert ErrorCode.URL_NOT_ADMITTED.value in denied
        assert all(secret not in denied for secret in (_UNSAFE_BODY, _UNSAFE_CLEAN))
        assert events == []
        text = await _search_file_text(client, paths, "keyword", query="unsafe")
        assert json.loads(text) == {"url": url, "abstract": "Keyword abstract"}
        assert _text(await client.call_tool("url_fetch", {"url": url})) == UNAVAILABLE_MESSAGE
        work = ["search", "fetch", "judge", "content_clean", "safety"]
        assert events == work
        assert await _filtering_cli(paths, ["url-fetch", url]) == (
            0,
            UNAVAILABLE_MESSAGE + "\n",
            "",
        )
        assert _text(await client.call_tool("url_fetch", {"url": url})) == UNAVAILABLE_MESSAGE
        assert events == work


async def test_paper_admission_includes_landing_url_and_excludes_pdf(mcp_home: Path) -> None:
    paths = RuntimePaths.from_home(mcp_home)
    runtime = build_acceptance_runtime(paths)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with asyncio.timeout(15), _running_daemon(daemon), http_frontend(mcp_home) as client:
        text = await _search_file_text(client, paths, "paper", query="academic topic")
        first = json.loads(text.splitlines()[0])
        assert first["url"] == "https://example.com/paper"
        assert first["pdf_url"] == "https://repository.example/paper.pdf"
        denied = await client.call_tool("url_fetch", {"url": first["pdf_url"]})
        assert ErrorCode.URL_NOT_ADMITTED.value in _text(denied, error=True)
        assert not runtime.fetch_provider.calls
        assert (
            _text(await client.call_tool("url_fetch", {"url": first["url"]}))
            == "Full article content"
        )
        assert len(runtime.fetch_provider.calls) == 1


def _recording_agent(recorder: RecordingClient) -> AgentTextRunner:
    async def run(model: str | None, messages: Sequence[ChatMessage]) -> str:
        assert model is not None
        return await recorder.complete_text(LLMInvocation(recorder.name, model), messages)

    return run


@pytest.mark.parametrize("kind", ["llm", "agent"])
async def test_scope_and_model_selectors_reach_configured_invocations(
    mcp_home: Path, frontend: Frontend, kind: str
) -> None:
    paths = RuntimePaths.from_home(mcp_home)
    runtime = build_filtering_acceptance_runtime(paths)
    if kind == "llm":
        runtime.search_orchestrator, recorders, _ = build_scoped_llm_search(
            paths.results_dir.parent, runtime.llm_invocations
        )
    else:
        recorders = {name: RecordingClient(name) for name in runtime.llm_clients}
        runtime.search_orchestrator = agent_orchestrator(
            paths.results_dir,
            [AgentInvocation(item.provider, item.model) for item in runtime.llm_invocations],
            {name: _recording_agent(recorder) for name, recorder in recorders.items()},
            store=runtime.store,
        )
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with asyncio.timeout(25), _running_daemon(daemon), frontend("auto") as client:
        unmatched = await client.call_tool(
            f"{kind}_search", {"prompt": "unmatched", "provider": "OpenAI_main"}
        )
        assert ErrorCode.BAD_REQUEST.value in _text(unmatched, error=True)
        assert all(not recorder.scoped_calls for recorder in recorders.values())
        assert not list(paths.results_dir.glob("*.jsonl"))

        primary = ("openai_main", "gpt-5")
        cases: list[tuple[str, dict[str, str], list[tuple[str, str]]]] = [
            ("web", {"provider": " openai_main "}, [primary, ("openai_main", "gpt-5-mini")]),
            ("paper", {"model": "gpt-5"}, [primary, ("azure_main", "gpt-5")]),
            ("all", {"provider": "openai_main", "model": "gpt-5"}, [primary]),
        ]
        expected_calls: Counter[tuple[str, str, str, str]] = Counter()
        for scope, selectors, selected in cases:
            prompt = f"find {scope} results"
            text = await _search_file_text(
                client, paths, kind, prompt=prompt, scope=scope, **selectors
            )
            rows = [json.loads(line) for line in text.splitlines()]
            branches = ["web", "paper"] if scope == "all" else [scope]
            assert Counter(row["url"] for row in rows) == Counter(
                f"https://example.com/{branch}/{provider}/{model}"
                for branch in branches
                for provider, model in selected
            )
            if scope == "all":
                assert [row["type"] for row in rows] == ["web", "paper"]
            else:
                assert all("type" not in row for row in rows)
            expected_calls.update(
                (provider, model, branch, prompt)
                for branch in branches
                for provider, model in selected
            )
            assert (
                Counter(
                    (name, invocation.model, branch, messages[-1]["content"])
                    for name, recorder in recorders.items()
                    for branch, invocation, messages in recorder.scoped_calls
                )
                == expected_calls
            )


@pytest.fixture
async def filtering_daemon(
    mcp_home: Path,
) -> AsyncIterator[tuple[RuntimePaths, FilteringAcceptanceRuntime]]:
    paths = RuntimePaths.from_home(mcp_home)
    runtime = build_filtering_acceptance_runtime(paths)
    async with _running_daemon(ForegroundDaemon(paths, runtime_factory=lambda: runtime)):
        yield paths, runtime


@pytest.mark.parametrize(("kind", "alias"), [("keyword", "beta"), ("paper", "openalex")])
async def test_provider_selector_reaches_only_the_requested_search_provider(
    filtering_daemon: tuple[RuntimePaths, FilteringAcceptanceRuntime],
    frontend: Frontend,
    kind: str,
    alias: str,
) -> None:
    paths, runtime = filtering_daemon
    async with asyncio.timeout(25), frontend("auto") as client:
        text = await _search_file_text(client, paths, kind, query="selected", provider=alias)
        assert json.loads(text)["url"] == f"https://example.com/{kind}/{alias}"
        for keyword in runtime.keyword_providers:
            assert keyword.calls == (["selected"] if keyword.name == alias else [])
        for academic in runtime.academic_providers:
            assert academic.calls == (["selected"] if academic.name == alias else [])


async def test_keyword_partial_success_and_all_failed_selection(
    filtering_daemon: tuple[RuntimePaths, FilteringAcceptanceRuntime], frontend: Frontend
) -> None:
    paths, runtime = filtering_daemon
    alpha, beta = runtime.keyword_providers
    alpha.failure = ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "offline provider failed")
    async with asyncio.timeout(25), frontend("auto") as client:
        text = await _search_file_text(client, paths, "keyword", query="partial")
        assert json.loads(text) == {"url": "https://example.com/keyword/beta", "abstract": "beta"}
        assert alpha.calls == beta.calls == ["partial"]
        files = set(paths.results_dir.glob("*.jsonl"))
        failed = await client.call_tool("keyword_search", {"query": "failed", "provider": "alpha"})
        assert ErrorCode.ALL_PROVIDERS_FAILED.value in _text(failed, error=True)
        assert alpha.calls == ["partial", "failed"]
        assert beta.calls == ["partial"]
        assert set(paths.results_dir.glob("*.jsonl")) == files


class _SearchBarrier:
    """Record physical Firecrawl sends and hold selected queries at the upstream."""

    def __init__(self, held: Sequence[str]) -> None:
        self.held = set(held)
        self.release = asyncio.Event()
        self.arrivals: asyncio.Queue[str] = asyncio.Queue()
        self.sends: list[tuple[str, str]] = []
        self.in_flight = 0

    async def respond(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/search"
        query = json.loads(request.content)["query"]
        self.sends.append((query, request.url.host))
        self.arrivals.put_nowait(query)
        self.in_flight += 1
        try:
            if query in self.held:
                await asyncio.wait_for(self.release.wait(), timeout=10)
            return _metadata(url=f"https://article.test/{query}")
        finally:
            self.in_flight -= 1


@asynccontextmanager
async def _scheduled_gateway(
    home: Path, upstream: _SearchBarrier, **options: object
) -> AsyncIterator[tuple[Runtime, Client, Client]]:
    with captured_stdio(home) as capture:
        async with (
            asyncio.timeout(25),
            _gateway(
                RuntimePaths.from_home(home),
                httpx.MockTransport(upstream.respond),
                web=("firecrawl",),
                web_options={"firecrawl": options},
            ) as runtime,
            capture.client as stdio,
            http_frontend(home) as http,
        ):
            try:
                yield runtime, stdio, http
            finally:
                upstream.release.set()


async def _keyword_call(home: Path, client: Client | None, query: str) -> None:
    if client is None:
        stdout, stderr = io.StringIO(), io.StringIO()
        args = build_parser().parse_args(["keyword-search", query])
        code = await run_command(args, RuntimePaths.from_home(home), stdout=stdout, stderr=stderr)
        path = _successful_path((code, stdout.getvalue(), stderr.getvalue()))
        text = path.read_text(encoding="utf-8")
    else:
        text = _text(await client.call_tool("keyword_search", {"query": query}))
    assert json.loads(text)["url"] == f"https://article.test/{query}"


async def test_shared_pool_skips_the_member_held_by_cli(mcp_home: Path) -> None:
    upstream = _SearchBarrier(["cli"])
    credentials = [
        {"api_url": f"https://{name}.test", "api_key_env": "TEST_KEY", "max_concurrency": 1}
        for name in ("first", "second")
    ]
    async with (
        _scheduled_gateway(
            mcp_home, upstream, credentials=credentials, credential_strategy="fill_first"
        ) as (_, stdio, http),
        asyncio.TaskGroup() as tasks,
    ):
        cli = tasks.create_task(_keyword_call(mcp_home, None, "cli"))
        assert await upstream.arrivals.get() == "cli"
        await _keyword_call(mcp_home, stdio, "stdio")
        await _keyword_call(mcp_home, http, "http")
        assert upstream.in_flight == 1
        assert upstream.sends == [
            ("cli", "first.test"),
            ("stdio", "second.test"),
            ("http", "second.test"),
        ]
        upstream.release.set()
        await cli
        await _keyword_call(mcp_home, http, "after-release")
        assert upstream.sends[3:] == [("after-release", "first.test")]


async def test_shared_limits_gate_cli_stdio_and_http(
    mcp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    period = 10
    clock = ManualClock()
    waiting: asyncio.Queue[float] = asyncio.Queue()
    rate_waiting = asyncio.Event()

    async def window_sleep(seconds: float) -> None:
        waiting.put_nowait(seconds)
        if seconds == period:
            rate_waiting.set()
        await clock.sleep(seconds)

    for name, factory in (
        ("make_rate_gate", make_rate_gate),
        ("CredentialPool", CredentialPool),
        ("HttpJsonExecutor", HttpJsonExecutor),
    ):
        monkeypatch.setattr(
            runtime_module, name, partial(factory, monotonic=clock.monotonic, sleep=window_sleep)
        )

    upstream = _SearchBarrier(["stdio"])
    credential = {
        "api_url": "https://member.test",
        "api_key_env": "TEST_KEY",
        "max_concurrency": 1,
        "rate_limits": [{"requests": 2, "period_seconds": period}],
    }
    async with (
        _scheduled_gateway(mcp_home, upstream, credentials=[credential]) as (runtime, stdio, http),
        asyncio.TaskGroup() as group,
    ):
        # Get the actual gates from a reservation and release it before the first search.
        reservation = await runtime.web_fetch_providers[0].try_reserve_fetch()
        assert reservation is not None
        capacity, rate = reservation.member.capacity, reservation.member.rate
        await reservation.release()
        assert capacity is not None and rate is not None

        stdio_call = group.create_task(_keyword_call(mcp_home, stdio, "stdio"))
        assert await upstream.arrivals.get() == "stdio"
        pending = {
            query: group.create_task(_keyword_call(mcp_home, client, query))
            for query, client in (("cli", None), ("http", http))
        }
        # Both other callers reached the pool while only capacity prevented a send.
        await waiting.get()
        await waiting.get()
        assert capacity.in_use == upstream.in_flight == 1
        assert rate.next_available_in_seconds() == 0
        assert upstream.sends == [("stdio", "member.test")]

        upstream.release.set()
        second = await upstream.arrivals.get()
        await pending.pop(second)
        await stdio_call
        await rate_waiting.wait()
        (third,) = pending
        assert capacity.in_use == upstream.in_flight == 0
        assert rate.next_available_in_seconds() == period
        await clock.advance_to(period - 1)
        assert rate.next_available_in_seconds() == 1
        assert upstream.sends == [("stdio", "member.test"), (second, "member.test")]
        assert not pending[third].done()

        await clock.advance_to(period)
        await pending[third]
        assert upstream.sends[2:] == [(third, "member.test")]
        assert capacity.in_use == 0 and capacity.max_observed_in_use == 1


async def test_omitted_limits_admit_all_three_entrypoints_before_release(mcp_home: Path) -> None:
    queries = [f"{source}-{i}" for source in ("cli", "stdio", "http") for i in range(4)]
    upstream = _SearchBarrier(queries)
    async with (
        _scheduled_gateway(mcp_home, upstream) as (_, stdio, http),
        asyncio.TaskGroup() as tasks,
    ):
        for source, client in (("cli", None), ("stdio", stdio), ("http", http)):
            for i in range(4):
                tasks.create_task(_keyword_call(mcp_home, client, f"{source}-{i}"))
        arrivals = [await upstream.arrivals.get() for _ in queries]
        assert sorted(arrivals) == sorted(queries)
        assert upstream.in_flight == len(queries)
        upstream.release.set()
    assert upstream.in_flight == 0
    assert sorted(upstream.sends) == sorted((query, "firecrawl.test") for query in queries)


async def test_daemon_restart_preserves_clients_and_requires_fresh_admission(
    mcp_home: Path,
) -> None:
    paths = RuntimePaths.from_home(mcp_home)
    fetch = {"url": "https://example.com/article"}
    first = build_acceptance_runtime(paths)
    with captured_stdio(mcp_home) as capture:
        async with asyncio.timeout(25), capture.client as stdio, http_frontend(mcp_home) as http:
            async with _running_daemon(ForegroundDaemon(paths, runtime_factory=lambda: first)):
                text = await _search_file_text(stdio, paths, "keyword", query="before restart")
                assert json.loads(text)["url"] == fetch["url"]
                assert _text(await http.call_tool("url_fetch", fetch)) == "Full article content"
            assert first.close_calls == 1 and not paths.socket_file.exists()

            for client in (stdio, http):
                missing = _text(await client.call_tool("url_fetch", fetch), error=True)
                assert "agent-search-gateway start" in missing and str(mcp_home) not in missing
            assert not paths.socket_file.exists()

            second = build_acceptance_runtime(paths)
            async with _running_daemon(ForegroundDaemon(paths, runtime_factory=lambda: second)):
                for client in (stdio, http):
                    denied = _text(await client.call_tool("url_fetch", fetch), error=True)
                    assert ErrorCode.URL_NOT_ADMITTED.value in denied
                assert not second.fetch_provider.calls
                text = await _search_file_text(http, paths, "keyword", query="after restart")
                assert json.loads(text)["url"] == fetch["url"]
                assert _text(await stdio.call_tool("url_fetch", fetch)) == "Full article content"
                assert len(second.fetch_provider.calls) == 1


@pytest.mark.parametrize("close_first", ["stdio", "http"])
async def test_independent_lifecycle_keeps_other_frontend_and_cli_live(
    mcp_home: Path, close_first: str
) -> None:
    paths = RuntimePaths.from_home(mcp_home)
    runtime = build_acceptance_runtime(paths)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with asyncio.timeout(25), _running_daemon(daemon) as running:
        with captured_stdio(mcp_home) as capture:
            http = http_frontend(mcp_home)
            survivor, closing = (
                (http, capture.client) if close_first == "stdio" else (capture.client, http)
            )
            async with survivor as live:
                async with closing as client:
                    text = await _search_file_text(client, paths, "keyword", query=close_first)
                    url = json.loads(text)["url"]
                if close_first == "stdio":
                    assert capture.process is not None and capture.process.get_returncode() == 0
                assert (
                    _text(await live.call_tool("url_fetch", {"url": url})) == "Full article content"
                )
                assert await _filtering_cli(paths, ["url-fetch", url]) == (
                    0,
                    "Full article content\n",
                    "",
                )
        output = _successful_path(
            await _filtering_cli(paths, ["keyword-search", "after both closed"])
        )
        assert json.loads(output.read_bytes())["url"] == url
        assert not running.done() and not daemon.shutting_down and runtime.close_calls == 0
    assert runtime.close_calls == 1


async def test_cancel_after_upstream_send_closes_socket_without_replay(
    mcp_home: Path, frontend: Frontend, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths.from_home(mcp_home)
    upstream = _SearchBarrier(["abandoned"])
    received: list[tuple[Request, asyncio.Task[object]]] = []
    dispatch = ForegroundDaemon._dispatch

    async def observe(daemon: ForegroundDaemon, request: Request) -> Response:
        # Dispatch runs in the accepted Unix connection's task, which owns writer cleanup.
        connection = asyncio.current_task()
        assert connection is not None
        received.append((request, connection))
        return await dispatch(daemon, request)

    monkeypatch.setattr(ForegroundDaemon, "_dispatch", observe)
    async with (
        asyncio.timeout(25),
        _gateway(paths, httpx.MockTransport(upstream.respond), web=("firecrawl",)),
    ):
        try:
            async with frontend("legacy") as client, asyncio.TaskGroup() as tasks:
                abandoned = tasks.create_task(
                    client.call_tool("keyword_search", {"query": "abandoned"})
                )
                assert await upstream.arrivals.get() == "abandoned"
                abandoned.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await abandoned
        finally:
            upstream.release.set()

        ((request, connection),) = received
        assert request == KeywordSearchRequest("abandoned")
        await asyncio.wait_for(asyncio.shield(connection), timeout=5)
        (original,) = paths.results_dir.glob("keyword-*.jsonl")
        written = original.read_bytes()
        assert json.loads(written)["url"] == "https://article.test/abandoned"

        await _keyword_call(mcp_home, None, "independent")
        assert original.read_bytes() == written
        assert len(list(paths.results_dir.glob("keyword-*.jsonl"))) == 2
        assert [request for request, _ in received] == [
            KeywordSearchRequest("abandoned"),
            KeywordSearchRequest("independent"),
        ]
        assert upstream.sends == [
            ("abandoned", "firecrawl.test"),
            ("independent", "firecrawl.test"),
        ]


async def test_http_concurrent_sessions_survive_call_error_and_session_close(
    mcp_home: Path,
) -> None:
    paths = RuntimePaths.from_home(mcp_home)
    upstream = _SearchBarrier(["held"])
    app = create_http_app(create_server(GatewayAdapter(paths)), "127.0.0.1")
    async with (
        asyncio.timeout(25),
        _gateway(paths, httpx.MockTransport(upstream.respond), web=("firecrawl",)),
        running_http(app) as url,
        http_session(url, "legacy") as first,
        http_session(url, "legacy") as second,
        asyncio.TaskGroup() as tasks,
    ):
        held = tasks.create_task(_keyword_call(mcp_home, first, "held"))
        try:
            assert await upstream.arrivals.get() == "held"
            async with http_session(url, "legacy") as closing:
                result = await closing.call_tool(
                    "url_fetch", {"url": "https://article.test/unknown"}
                )
                assert ErrorCode.URL_NOT_ADMITTED.value in _text(result, error=True)
            await _keyword_call(mcp_home, second, "after-close")
            assert not held.done() and upstream.in_flight == 1
        finally:
            upstream.release.set()
        await held
        await _keyword_call(mcp_home, first, "after-release")
        assert upstream.sends == [
            ("held", "firecrawl.test"),
            ("after-close", "firecrawl.test"),
            ("after-release", "firecrawl.test"),
        ]
