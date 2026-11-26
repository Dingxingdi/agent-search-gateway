"""Offline credential scheduling and URL states through the production socket API."""

import asyncio
import json
from functools import partial
from pathlib import Path

import httpx
import pytest

from agent_search_gateway import runtime as runtime_module
from agent_search_gateway.credential_pool import CredentialPool
from agent_search_gateway.errors import UNAVAILABLE_MESSAGE, ErrorCode
from agent_search_gateway.models import ErrorResponse, URLFetchRequest, URLStatus
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.protocol import send_request
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.rate_limit import make_rate_gate
from agent_search_gateway.url_normalization import normalize_url
from tests.acceptance.test_gateway_workflows import _filtering_cli, _successful_path
from tests.acceptance.test_search_cost_arxiv_proxy_cli import (
    _CHAT,
    _SEARCH,
    _TARGET,
    _completion,
    _gateway,
    _keyword,
    _metadata,
)
from tests.doctor._support import environment_name, write_valid_config
from tests.support.agent_search import web_markdown
from tests.support.controlled import ManualClock


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(tmp_path / "config.toml", tmp_path / "daemon.sock", tmp_path / "results")


_RETRY = {"max_attempts": 2, "base_delay_seconds": 0.001, "max_delay_seconds": 0.001}


async def test_ordered_fetch_exhausts_member_retry_before_fallback(paths: RuntimePaths) -> None:
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        endpoint = str(request.url)
        calls.append(endpoint)
        if endpoint == _SEARCH:
            return _metadata()
        if request.url.host == "first.test":
            return httpx.Response(503)
        if request.url.host == "second.test":
            assert json.loads(request.content) == {"url": _TARGET}
            return httpx.Response(200, text="Second member article")
        assert endpoint == _CHAT
        return _completion(request)

    options = {
        "jina": {
            "enable_search": False,
            "credentials": [{"api_url": f"https://{label}.test"} for label in ("first", "second")],
        }
    }
    async with _gateway(
        paths,
        httpx.MockTransport(respond),
        web=("firecrawl", "jina"),
        web_options=options,
        fetch_order=["jina", "firecrawl"],
        overrides={"retry": _RETRY},
    ) as runtime:
        await _keyword(paths, "firecrawl")
        assert await _filtering_cli(paths, ["url-fetch", _TARGET]) == (
            0,
            "Second member article\n",
            "",
        )
        record = runtime.store.get(normalize_url(_TARGET))
        assert record is not None and record.status is URLStatus.READY
    assert calls == [
        _SEARCH,
        "https://first.test",
        "https://first.test",
        "https://second.test",
        _CHAT,
        _CHAT,
    ]


async def test_omitted_limits_allow_independent_searches_to_enter_together(
    paths: RuntimePaths,
) -> None:
    count = 12
    entered: set[str] = set()
    all_entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == _SEARCH
        entered.add(json.loads(request.content)["query"])
        if len(entered) == count:
            all_entered.set()
        await release.wait()
        return _metadata()

    async with _gateway(paths, httpx.MockTransport(respond), web=("firecrawl",)):
        tasks = [
            asyncio.create_task(_filtering_cli(paths, ["keyword-search", f"query-{index}"]))
            for index in range(count)
        ]
        try:
            await asyncio.wait_for(all_entered.wait(), timeout=5)
        finally:
            release.set()
            results = await asyncio.gather(*tasks)
        assert all(code == 0 and not stderr for code, _, stderr in results)
        assert len({stdout for _, stdout, _ in results}) == count


@pytest.mark.parametrize(
    ("strategy", "initial"),
    [("round_robin", ["a", "b", "a", "b"]), ("fill_first", ["a", "a", "b", "b"])],
)
async def test_independent_member_windows_count_physical_retries(
    paths: RuntimePaths, strategy: str, initial: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock()
    waiting = asyncio.Event()
    period = 10

    async def window_sleep(seconds: float) -> None:
        waiting.set()
        await clock.sleep(seconds)

    async def retry_sleep(seconds: float) -> None:
        await clock.advance_to(clock.now + seconds)

    monkeypatch.setattr(
        runtime_module,
        "make_rate_gate",
        partial(make_rate_gate, monotonic=clock.monotonic, sleep=window_sleep),
    )
    monkeypatch.setattr(
        runtime_module,
        "CredentialPool",
        partial(CredentialPool, monotonic=clock.monotonic, sleep=window_sleep),
    )
    monkeypatch.setattr(
        runtime_module,
        "HttpJsonExecutor",
        partial(HttpJsonExecutor, monotonic=clock.monotonic, sleep=retry_sleep),
    )
    urls = [f"{_TARGET}/{index}" for index in range(6)]
    sends: list[str] = []
    attempts: list[str] = []
    sent_at: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _SEARCH:
            rows = [{"url": url, "description": "Abstract"} for url in urls]
            return httpx.Response(200, json={"success": True, "data": {"web": rows}})
        if str(request.url) == _CHAT:
            return _completion(request)
        sent_at.append(clock.now)
        sends.append(request.url.host.split(".")[0])
        attempts.append(json.loads(request.content)["url"])
        if attempts == [*urls[:4], urls[4]]:
            return httpx.Response(503)
        return httpx.Response(200, text="Article")

    credentials = [
        {
            "api_url": f"https://{name}.test",
            "rate_limits": [{"requests": 2, "period_seconds": period}],
        }
        for name in ("a", "b")
    ]
    async with _gateway(
        paths,
        httpx.MockTransport(respond),
        web=("firecrawl", "jina"),
        web_options={
            "firecrawl": {"enable_fetch": False},
            "jina": {
                "enable_search": False,
                "credentials": credentials,
                "credential_strategy": strategy,
            },
        },
        overrides={"retry": _RETRY},
    ):
        code, _, stderr = await _filtering_cli(paths, ["keyword-search", "articles"])
        assert (code, stderr) == (0, "")
        for url in urls[:4]:
            assert await _filtering_cli(paths, ["url-fetch", url]) == (0, "Article\n", "")
        pending = asyncio.create_task(_filtering_cli(paths, ["url-fetch", urls[4]]))
        try:
            await asyncio.wait_for(waiting.wait(), timeout=2)
            await clock.advance_to(period - 1)
            assert sends == initial and not pending.done()
            await clock.advance_to(period)
            assert await pending == (0, "Article\n", "")
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        assert await _filtering_cli(paths, ["url-fetch", urls[5]]) == (0, "Article\n", "")
    retried_at = period + _RETRY["base_delay_seconds"]
    assert sent_at == [0, 0, 0, 0, period, retried_at, retried_at]
    assert sends == [*initial, "a", "a", "b"]
    assert attempts == [*urls[:5], urls[4], urls[5]]


async def test_search_candidate_has_request_local_focus_and_ready_fulltext(
    paths: RuntimePaths,
) -> None:
    models: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _CHAT:
            models.append(json.loads(request.content)["model"])
            return _completion(request)
        assert request.url.path == "/search"
        row = {"url": _TARGET, "content": "Abstract", "raw_content": "Natural article"}
        return httpx.Response(200, json={"results": [row]})

    async with _gateway(paths, httpx.MockTransport(respond), web=("tavily",)) as runtime:
        await _keyword(paths, "tavily")
        record = runtime.store.get(normalize_url(_TARGET))
        assert record is not None and record.status is URLStatus.NEED_JUDGE
        assert not models
        fetch = ["url-fetch", _TARGET]
        assert await _filtering_cli(paths, fetch) == (0, "Clean article\n", "")
        fulltext_stages = ["judge", "content_clean", "safety"]
        assert models == fulltext_stages
        ready = runtime.store.get(normalize_url(_TARGET))
        assert ready is not None and ready.status is URLStatus.READY
        assert (ready.raw_content, ready.content) == ("Natural article", "Clean article")
        assert await _filtering_cli(paths, fetch) == (0, "Clean article\n", "")
        assert models == fulltext_stages
        assert await _filtering_cli(paths, [*fetch, "topic"]) == (0, "Focused summary\n", "")
        assert await _filtering_cli(paths, fetch) == (0, "Clean article\n", "")
        assert models == [*fulltext_stages, "focus_summary"]
        assert runtime.store.get(normalize_url(_TARGET)) == ready


@pytest.mark.parametrize("fail_fetch", [False, True])
async def test_cached_rejection_clears_both_fields_before_actual_fetch(
    paths: RuntimePaths, fail_fetch: bool
) -> None:
    stages: list[str] = []
    snapshots: list[tuple[URLStatus, str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            row = {"url": _TARGET, "highlights": ["Abstract"], "text": "Old rejected article"}
            return httpx.Response(200, json={"results": [row]})
        if str(request.url) == _CHAT:
            payload = json.loads(request.content)
            stages.append(payload["model"])
            if stages == ["judge"]:
                assert "Old rejected article" in payload["messages"][-1]["content"]
                return _completion(request, '{"ok":false}')
            assert "Old rejected article" not in payload["messages"][-1]["content"]
            return _completion(request)
        assert request.url.host == "jina.test"
        record = runtime.store.get(normalize_url(_TARGET))
        assert record is not None
        snapshots.append((record.status, record.raw_content, record.content))
        return (
            httpx.Response(503) if fail_fetch else httpx.Response(200, text="Replacement article")
        )

    async with _gateway(
        paths,
        httpx.MockTransport(respond),
        web=("exa", "jina"),
        web_options={
            "exa": {"enable_fetch": False},
            "jina": {"enable_search": False, "credentials": [{"api_url": "https://jina.test"}]},
        },
    ) as runtime:
        await _keyword(paths, "exa")
        cached = runtime.store.get(normalize_url(_TARGET))
        assert cached is not None and cached.status is URLStatus.NEED_JUDGE
        assert cached.raw_content == cached.content == "Old rejected article"
        code, stdout, stderr = await _filtering_cli(paths, ["url-fetch", _TARGET])
        record = runtime.store.get(normalize_url(_TARGET))
        assert record is not None
        assert snapshots == [(URLStatus.NEED_FETCH, "", "")]
        if fail_fetch:
            assert (code, stdout, stderr) == (1, "", "All URL fetch provider pipelines failed\n")
            assert (record.status, record.raw_content, record.content) == snapshots[0]
            assert stages == ["judge"]
        else:
            assert (code, stdout, stderr) == (0, "Replacement article\n", "")
            assert (record.status, record.raw_content, record.content) == (
                URLStatus.READY,
                "Replacement article",
                "Replacement article",
            )
            assert stages == ["judge", "judge", "safety"]


async def test_four_provider_pools_and_selected_llm_alias_share_actual_sends(
    paths: RuntimePaths,
) -> None:
    calls: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "tavily.test":
            calls.append(("web", host))
            row = {"url": _TARGET, "content": "Abstract", "raw_content": "Natural article"}
            return httpx.Response(200, json={"results": [row]})
        if host.startswith("crossref-"):
            calls.append(("academic", host))
            item = {"DOI": "10.1000/example", "title": ["Example paper"]}
            return httpx.Response(200, json={"message": {"items": [item]}})
        if host.startswith("oa-"):
            calls.append(("resolver", host))
            return httpx.Response(200, json={"is_oa": True, "oa_status": "gold"})
        member_index = host.split(".")[0].split("-")[-1]
        assert request.headers["Authorization"] == f"Bearer offline-member-{member_index}"
        model = json.loads(request.content)["model"]
        calls.append((model, host))
        return _completion(request, web_markdown() if model == "lookup" else None)

    def endpoints(prefix: str) -> list[dict[str, str]]:
        return [{"api_url": f"https://{prefix}-{n}.test"} for n in (1, 2)]

    academic: dict[str, object] = {
        "crossref": {"enabled": True, "credentials": endpoints("crossref")}
    }
    llm = {
        "protocol": "openai",
        "api_endpoint": "chat_completions",
        "credentials": [
            dict(member, api_key_env=f"MEMBER_{index}")
            for index, member in enumerate(endpoints("chat"), start=1)
        ],
    }
    oa = {
        "enabled": True,
        "credentials": [dict(member, contact_email_env="CONTACT") for member in endpoints("oa")],
    }
    overrides = {
        "oa_resolvers": {"unpaywall": oa},
        "llm_providers": {"chat": llm},
        "search_llm": {
            "providers": [{"provider": "chat", "model": m} for m in ("lookup", "other")]
        },
    }
    async with _gateway(
        paths,
        httpx.MockTransport(respond),
        web=("tavily",),
        academic=academic,
        overrides=overrides,
        environ={
            "CONTACT": "reader@example.test",
            "MEMBER_1": "offline-member-1",
            "MEMBER_2": "offline-member-2",
        },
    ):
        await _keyword(paths, "tavily")
        for _ in range(2):
            path = _successful_path(
                await _filtering_cli(paths, ["paper-search", "papers", "--provider", "crossref"])
            )
            paper = json.loads(path.read_text())
            assert (paper["sources"], paper["is_open_access"]) == (["crossref"], True)
        path = _successful_path(
            await _filtering_cli(
                paths,
                ["llm-search", "find", "--provider", "chat", "--model", "lookup", "--scope", "web"],
            )
        )
        assert json.loads(path.read_text())["abstract"] == "Agent result"
        assert await _filtering_cli(paths, ["url-fetch", _TARGET]) == (0, "Clean article\n", "")
    assert calls == [
        ("web", "tavily.test"),
        ("academic", "crossref-1.test"),
        ("resolver", "oa-1.test"),
        ("academic", "crossref-2.test"),
        ("resolver", "oa-2.test"),
        ("lookup", "chat-1.test"),
        ("judge", "chat-2.test"),
        ("content_clean", "chat-1.test"),
        ("safety", "chat-2.test"),
    ]


async def test_stop_releases_retry_lease_and_pending_fetch_selection(
    paths: RuntimePaths, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agent_search_gateway.daemon._SHUTDOWN_GRACE_SECONDS", 0.01)
    caplog.set_level("DEBUG", logger="agent_search_gateway")
    urls = [f"{_TARGET}/{index}" for index in range(2)]
    sends: list[str] = []
    recovering = False

    def respond(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _SEARCH:
            rows = [{"url": url, "description": "Abstract"} for url in urls]
            return httpx.Response(200, json={"success": True, "data": {"web": rows}})
        if str(request.url) == _CHAT:
            return _completion(request)
        sends.append(json.loads(request.content)["url"])
        return httpx.Response(200, text="Recovered") if recovering else httpx.Response(503)

    options: dict[str, dict[str, object]] = {
        "firecrawl": {"enable_fetch": False},
        "jina": {
            "enable_search": False,
            "credentials": [{"api_url": "https://jina.test", "max_concurrency": 1}],
        },
    }
    retry = {"max_attempts": 2, "base_delay_seconds": 30, "max_delay_seconds": 30}

    async def wait_event(name: str) -> None:
        async with asyncio.timeout(2):
            while not any(getattr(r, "gateway_event", None) == name for r in caplog.records):
                await asyncio.sleep(0)

    async with _gateway(
        paths,
        httpx.MockTransport(respond),
        web=("firecrawl", "jina"),
        web_options=options,
        overrides={"retry": retry},
    ) as runtime:
        _successful_path(await _filtering_cli(paths, ["keyword-search", "articles"]))
        first = asyncio.create_task(_filtering_cli(paths, ["url-fetch", urls[0]]))
        await wait_event("http_retrying")
        second = asyncio.create_task(_filtering_cli(paths, ["url-fetch", urls[1]]))
        await wait_event("scheduler_waiting")
        assert await _filtering_cli(paths, ["stop"]) == (0, "Daemon stopped.\n", "")
        for code, stdout, stderr in await asyncio.gather(first, second):
            assert code != 0 and not stdout and stderr
        assert sends == urls[:1]
        reservation = await runtime.web_fetch_providers[0].try_reserve_fetch()
        assert reservation is not None
        await reservation.release()
        for url in urls:
            record = runtime.store.get(normalize_url(url))
            assert record is not None and record.status is URLStatus.NEED_FETCH

    recovering = True
    async with _gateway(
        paths, httpx.MockTransport(respond), web=("firecrawl", "jina"), web_options=options
    ):
        _successful_path(await _filtering_cli(paths, ["keyword-search", "articles"]))
        assert await _filtering_cli(paths, ["url-fetch", urls[1]]) == (0, "Recovered\n", "")
    assert sends == urls


async def test_cancelled_safety_resumes_from_its_stage_on_the_next_request(
    paths: RuntimePaths,
) -> None:
    stages: list[str] = []
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            row = {"url": _TARGET, "content": "Abstract", "raw_content": "Natural article"}
            return httpx.Response(200, json={"results": [row]})
        model = json.loads(request.content)["model"]
        stages.append(model)
        if model == "safety" and not entered.is_set():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return _completion(request)

    async with _gateway(paths, httpx.MockTransport(respond), web=("tavily",)) as runtime:
        await _keyword(paths, "tavily")
        leader = asyncio.create_task(runtime.fetch_orchestrator.url_fetch(_TARGET))
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            leader.cancel()
            with pytest.raises(asyncio.CancelledError):
                await leader
        assert cancelled.is_set()
        record = runtime.store.get(normalize_url(_TARGET))
        assert record is not None and record.status is URLStatus.NEED_SAFETY
        assert await _filtering_cli(paths, ["url-fetch", _TARGET]) == (0, "Clean article\n", "")
        assert stages == ["judge", "content_clean", "safety", "safety"]


async def test_doctor_reports_each_member_and_rejects_legacy_schema(
    paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "offline-diagnostic-value"
    monkeypatch.setenv(environment_name(), sentinel)
    write_valid_config(paths.config_file)
    valid = paths.config_file.read_text() + (
        "\n[web_providers.jina]\nenable_fetch = true\n"
        "[[web_providers.jina.credentials]]\napi_url = 'https://member-one.test'\n"
        "[[web_providers.jina.credentials]]\napi_url = 'https://member-two.test'\n"
    )
    paths.config_file.write_text(valid)
    code, stdout, stderr = await _filtering_cli(paths, ["doctor"])
    assert (code, stderr) == (0, "")
    for label in (
        "web_providers.jina.credentials[0]",
        "web_providers.jina.credentials[1]",
        "llm_providers.primary.credentials[0]",
    ):
        assert f"{label}: configuration valid" in stdout
    assert sentinel not in stdout
    paths.config_file.write_text(valid.replace("[[llm_providers.primary.credentials]]\n", ""))
    code, stdout, stderr = await _filtering_cli(paths, ["doctor"])
    assert code != 0 and stderr == ""
    assert "llm_providers.primary" in stdout and "credentials" in stdout
    assert sentinel not in stdout


async def test_unavailable_text_and_error_envelope_keep_page_content_private(
    paths: RuntimePaths,
) -> None:
    sentinel = "offline-unsafe-page-body"
    stages: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            row = {"url": _TARGET, "content": "Abstract", "raw_content": sentinel}
            return httpx.Response(200, json={"results": [row]})
        model = json.loads(request.content)["model"]
        stages.append(model)
        return _completion(request, '{"ok":false}' if model == "safety" else None)

    async with _gateway(paths, httpx.MockTransport(respond), web=("tavily",)) as runtime:
        response = await send_request(paths.socket_file, URLFetchRequest(_TARGET, None))
        assert response == ErrorResponse(
            ErrorCode.URL_NOT_ADMITTED, "URL was not admitted by search"
        )
        await _keyword(paths, "tavily")
        for _ in range(2):
            assert await _filtering_cli(paths, ["url-fetch", _TARGET]) == (
                0,
                UNAVAILABLE_MESSAGE + "\n",
                "",
            )
        record = runtime.store.get(normalize_url(_TARGET))
        assert record is not None and record.status is URLStatus.UNAVAILABLE
        assert stages == ["judge", "content_clean", "safety"]
