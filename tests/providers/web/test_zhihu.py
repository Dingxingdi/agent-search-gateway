import asyncio
import json
import logging
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import DiagnosticText, SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, KeywordSearchProvider
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.web import zhihu
from agent_search_gateway.providers.web.zhihu import ZhihuAdapter
from tests.support.http import RecordingJsonExecutor
from tests.support.logging import structured_test_logger

_FIXTURES = Path(__file__).parents[2] / "fixtures/providers/zhihu"
_BRANCHES = ("zhihu_search", "global_search")


def _fixture(branch: str, *, empty: bool = False) -> dict[str, object]:
    suffix = "_empty" if empty else ""
    payload: object = json.loads((_FIXTURES / f"{branch}{suffix}.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


async def test_zhihu_search_combines_both_official_endpoints(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    query = "  中文 & original /?  "
    requests: list[httpx.Request] = []
    started = int(time.time())

    def respond(request: httpx.Request) -> httpx.Response:
        branch = request.url.path.rsplit("/", 1)[-1]
        assert branch in _BRANCHES
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url) == (
            f"https://developer.zhihu.com/api/v1/content/{branch}?" + urlencode({"Query": query})
        )
        assert request.url.params["Query"] == query
        assert request.headers["Authorization"] == "Bearer access-secret"
        assert request.headers["Content-Type"] == "application/json"
        assert started <= int(request.headers["X-Request-Timestamp"]) <= int(time.time())
        assert request.content == b""
        return httpx.Response(200, json=_fixture(branch))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        registration = build_default_registry().require("zhihu")
        adapter = registration.factory(
            name="zhihu",
            secret=SecretValue("access-secret"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="zhihu"),
        )
        assert isinstance(adapter, KeywordSearchProvider)
        hits = await adapter.search(query)

    assert len(requests) == 2
    assert {request.url.path.rsplit("/", 1)[-1] for request in requests} == set(_BRANCHES)
    assert hits == [
        KeywordSearchHit(
            "https://example.com/zhihu-result?utm_medium=openapi_platform&utm_source=fixture",
            "示例标题",
            "简短示例摘要",
        ),
        KeywordSearchHit(
            "https://example.org/global-result?utm_medium=openapi_platform&utm_source=fixture",
            "全网示例标题",
            "包含 <em>关键词</em> 的摘要",
        ),
    ]
    assert all(hit.raw_content == hit.content == "" for hit in hits)
    stages = {
        vars(record)["gateway_fields"]["stage"]
        for record in caplog.records
        if getattr(record, "gateway_event", "").startswith("http_")
    }
    assert stages == set(_BRANCHES)
    assert not hasattr(adapter, "fetch")


async def test_zhihu_starts_both_branches_before_either_finishes() -> None:
    entered = {branch: asyncio.Event() for branch in _BRANCHES}
    release = {branch: asyncio.Event() for branch in _BRANCHES}
    finished = {branch: asyncio.Event() for branch in _BRANCHES}

    async def respond(request: httpx.Request) -> httpx.Response:
        branch = request.url.path.rsplit("/", 1)[-1]
        entered[branch].set()
        await release[branch].wait()
        finished[branch].set()
        return httpx.Response(200, json=_fixture(branch))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        registration = build_default_registry().require("zhihu")
        adapter = registration.factory(
            name="zhihu",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="zhihu"),
        )
        assert isinstance(adapter, KeywordSearchProvider)
        search = asyncio.create_task(adapter.search("query"))
        try:
            async with asyncio.timeout(1):
                await asyncio.gather(*(event.wait() for event in entered.values()))
                release["global_search"].set()
                await finished["global_search"].wait()
                assert not search.done()
                release["zhihu_search"].set()
                hits = await search
        finally:
            for event in release.values():
                event.set()
            if not search.done():
                search.cancel()
            await asyncio.gather(search, return_exceptions=True)

    assert [hit.title for hit in hits] == ["示例标题", "全网示例标题"]


@pytest.mark.parametrize("cancelled_branch", _BRANCHES)
async def test_zhihu_branch_cancellation_cancels_and_awaits_sibling(
    cancelled_branch: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    entered = {branch: asyncio.Event() for branch in _BRANCHES}
    cancel_branch = asyncio.Event()
    blocked = asyncio.Event()
    cleaned = asyncio.Event()
    tasks: dict[str, asyncio.Task[object]] = {}
    cancellation = asyncio.CancelledError("branch cancelled")

    class ControlledRequester:
        async def request_json(
            self, method: str, url: str, *, stage: str, **kwargs: object
        ) -> object:
            task = asyncio.current_task()
            assert task is not None
            tasks[stage] = task
            entered[stage].set()
            if stage == cancelled_branch:
                await cancel_branch.wait()
                raise cancellation
            try:
                await blocked.wait()
                return _fixture(stage)
            finally:
                cleaned.set()

    adapter = ZhihuAdapter(
        name="zhihu", secret=SecretValue("x"), http_executor=ControlledRequester()
    )
    search = asyncio.create_task(adapter.search("query"))
    try:
        async with asyncio.timeout(1):
            await asyncio.gather(*(event.wait() for event in entered.values()))
            cancel_branch.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await search
            assert caught.value is cancellation
            assert cleaned.is_set()
            assert all(task.done() for task in tasks.values())
            assert search.cancelled()
            assert not any(
                getattr(record, "gateway_event", None)
                in {
                    "provider_partial_failure",
                    "provider_candidates_summary",
                    "provider_completed",
                    "provider_failed",
                }
                for record in caplog.records
            )
    finally:
        for task in (search, *tasks.values()):
            if not task.done():
                task.cancel()
        await asyncio.gather(search, *tasks.values(), return_exceptions=True)


async def test_zhihu_caller_cancellation_awaits_asynchronous_branch_cleanup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    entered = {branch: asyncio.Event() for branch in _BRANCHES}
    cleaned = {branch: asyncio.Event() for branch in _BRANCHES}
    blocked = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    first_done = asyncio.Event()
    tasks: dict[str, asyncio.Task[object]] = {}

    async def respond(request: httpx.Request) -> httpx.Response:
        branch = request.url.path.rsplit("/", 1)[-1]
        task = asyncio.current_task()
        assert task is not None
        tasks[branch] = task
        if branch == "zhihu_search":
            task.add_done_callback(lambda _: first_done.set())
        entered[branch].set()
        try:
            await blocked.wait()
            return httpx.Response(200, json=_fixture(branch))
        finally:
            if branch == "global_search":
                cleanup_started.set()
                await release_cleanup.wait()
            cleaned[branch].set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(3, 0, 0, 5), provider_name="zhihu"),
        )
        search = asyncio.create_task(adapter.search("query"))
        try:
            async with asyncio.timeout(1):
                await asyncio.gather(*(event.wait() for event in entered.values()))
                search.cancel("caller cancelled")
                await cleanup_started.wait()
                await first_done.wait()
                assert not search.done()
                release_cleanup.set()
                with pytest.raises(asyncio.CancelledError, match="caller cancelled"):
                    await search
                assert all(event.is_set() for event in cleaned.values())
                assert all(task.cancelled() for task in tasks.values())
                assert search.cancelled()
        finally:
            release_cleanup.set()
            for task in (search, *tasks.values()):
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(search, *tasks.values(), return_exceptions=True)

    assert not any(
        getattr(record, "gateway_event", None)
        in {
            "provider_partial_failure",
            "provider_candidates_summary",
            "provider_completed",
            "provider_failed",
            "http_attempt_completed",
        }
        for record in caplog.records
    )


@pytest.mark.parametrize("cancellation_source", ["branch", "caller"])
async def test_zhihu_retry_after_sleep_cancellation_stops_later_requests(
    cancellation_source: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    attempts = dict.fromkeys(_BRANCHES, 0)
    sleep_entered = asyncio.Event()
    blocked = asyncio.Event()
    sleep_cleaned = asyncio.Event()
    sibling_done = asyncio.Event()
    tasks: dict[str, asyncio.Task[object]] = {}
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        sleep_entered.set()
        try:
            await blocked.wait()
        finally:
            sleep_cleaned.set()

    def respond(request: httpx.Request) -> httpx.Response:
        branch = request.url.path.rsplit("/", 1)[-1]
        task = asyncio.current_task()
        assert task is not None
        tasks[branch] = task
        attempts[branch] += 1
        if branch == "zhihu_search":
            return httpx.Response(429, headers={"Retry-After": "2"})
        task.add_done_callback(lambda _: sibling_done.set())
        return httpx.Response(200, json=_fixture(branch))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(3, 0, 0, 5), provider_name="zhihu", sleep=sleep
            ),
        )
        search = asyncio.create_task(adapter.search("query"))
        try:
            async with asyncio.timeout(1):
                await sleep_entered.wait()
                await sibling_done.wait()
                assert not search.done()
                target = tasks["zhihu_search"] if cancellation_source == "branch" else search
                target.cancel("retry sleep cancelled")
                with pytest.raises(asyncio.CancelledError, match="retry sleep cancelled"):
                    await search
                assert sleep_cleaned.is_set()
                assert all(task.done() for task in tasks.values())
                assert tasks["zhihu_search"].cancelled()
                assert search.cancelled()
                assert delays == [2.0]
                assert attempts == dict.fromkeys(_BRANCHES, 1)
        finally:
            for task in (search, *tasks.values()):
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(search, *tasks.values(), return_exceptions=True)

    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["stage"] == "global_search"
    assert summaries[0]["accepted"] == 1
    assert not any(
        getattr(record, "gateway_event", None)
        in {"provider_partial_failure", "provider_completed", "provider_failed", "http_failed"}
        for record in caplog.records
    )


async def test_zhihu_refreshes_timestamp_immediately_before_each_branch_and_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1_800_000_000.75]
    monkeypatch.setattr("agent_search_gateway.providers.web.zhihu.time.time", lambda: now[0])
    timestamps: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        branch = request.url.path.rsplit("/", 1)[-1]
        timestamps.append((branch, request.headers["X-Request-Timestamp"]))
        now[0] += 1
        return httpx.Response(200, json=_fixture(branch, empty=True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="zhihu"),
        )
        now[0] += 10
        await adapter.search("first")
        now[0] += 10
        await adapter.search("second")

    assert timestamps == [
        ("zhihu_search", "1800000010"),
        ("global_search", "1800000011"),
        ("zhihu_search", "1800000022"),
        ("global_search", "1800000023"),
    ]


@pytest.mark.parametrize("query", ["中文查询", "a&b/c? d", " + literal &/?  "])
async def test_zhihu_encodes_query_without_changing_its_meaning(query: str) -> None:
    seen: set[str] = set()

    def respond(request: httpx.Request) -> httpx.Response:
        branch = request.url.path.rsplit("/", 1)[-1]
        seen.add(branch)
        assert request.url.params.multi_items() == [("Query", query)]
        assert request.url.query.decode("ascii") == urlencode({"Query": query})
        return httpx.Response(200, json=_fixture(branch, empty=True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="zhihu"),
        )
        await adapter.search(query)

    assert seen == set(_BRANCHES)


@pytest.mark.parametrize(
    ("api_url", "expected_base"),
    [
        (None, "https://developer.zhihu.com"),
        ("https://developer.zhihu.com/", "https://developer.zhihu.com"),
        ("https://zhihu.example.test", "https://zhihu.example.test"),
        ("https://zhihu.example.test/proxy", "https://zhihu.example.test/proxy"),
        ("https://zhihu.example.test/proxy/", "https://zhihu.example.test/proxy"),
    ],
)
async def test_zhihu_appends_both_paths_to_default_or_custom_base(
    api_url: str | None, expected_base: str
) -> None:
    seen: set[str] = set()

    def respond(request: httpx.Request) -> httpx.Response:
        branch = request.url.path.rsplit("/", 1)[-1]
        seen.add(branch)
        assert str(request.url) == f"{expected_base}/api/v1/content/{branch}?Query=query"
        return httpx.Response(200, json=_fixture(branch, empty=True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        options = {} if api_url is None else {"api_url": api_url}
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="zhihu"),
            **options,
        )
        await adapter.search("query")

    assert seen == set(_BRANCHES)


async def test_zhihu_accepts_both_successful_empty_lists(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    seen: set[str] = set()

    def respond(request: httpx.Request) -> httpx.Response:
        branch = request.url.path.rsplit("/", 1)[-1]
        seen.add(branch)
        return httpx.Response(200, json=_fixture(branch, empty=True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="zhihu"),
        )
        assert await adapter.search("query") == []

    assert seen == set(_BRANCHES)
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 2
    assert {fields["stage"] for fields in summaries} == set(_BRANCHES)
    for fields in summaries:
        assert fields["received"] == fields["accepted"] == fields["rejected"] == 0
        assert fields["processed"] == fields["unprocessed"] == fields["rewritten"] == 0
        assert fields["rejection_counts"] == {}
    assert not any(
        getattr(record, "gateway_event", None)
        in {"provider_partial_failure", "provider_completed", "provider_failed"}
        for record in caplog.records
    )


@pytest.mark.parametrize("optional_value", ["missing", None, ""])
async def test_zhihu_defaults_absent_optional_title_and_summary_to_empty_strings(
    optional_value: str | None,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        branch = request.url.path.rsplit("/", 1)[-1]
        payload = _fixture(branch)
        data = payload["Data"]
        assert isinstance(data, dict)
        items = data["Items"]
        assert isinstance(items, list)
        item = items[0]
        for key in ("Title", "ContentText"):
            if optional_value == "missing":
                item.pop(key)
            else:
                item[key] = optional_value
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="zhihu"),
        )
        hits = await adapter.search("query")

    assert len(hits) == 2
    assert all(hit.title == hit.snippet == hit.raw_content == hit.content == "" for hit in hits)


async def test_zhihu_preserves_duplicate_urls_from_both_branches() -> None:
    url = "  HTTPS://example.com/shared?utm_source=fixture  "

    def respond(request: httpx.Request) -> httpx.Response:
        branch = request.url.path.rsplit("/", 1)[-1]
        payload = _fixture(branch)
        data = payload["Data"]
        assert isinstance(data, dict)
        items = data["Items"]
        assert isinstance(items, list)
        items[0]["Url"] = url
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="zhihu"),
        )
        hits = await adapter.search("query")

    assert [hit.url for hit in hits] == [url, url]
    assert [hit.title for hit in hits] == ["示例标题", "全网示例标题"]


@pytest.mark.parametrize("successful_branch", _BRANCHES)
@pytest.mark.parametrize("empty", [False, True])
async def test_zhihu_preserves_successful_branch_after_sibling_failure(
    successful_branch: str, empty: bool, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    attempts = dict.fromkeys(_BRANCHES, 0)
    failing_branch = next(branch for branch in _BRANCHES if branch != successful_branch)

    def respond(request: httpx.Request) -> httpx.Response:
        branch = request.url.path.rsplit("/", 1)[-1]
        attempts[branch] += 1
        if branch == successful_branch:
            return httpx.Response(200, json=_fixture(branch, empty=empty))
        if branch == "global_search":
            return httpx.Response(503)
        return httpx.Response(200, json={"Code": 0, "Data": {"Items": {}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(2, 0, 0, 5), provider_name="zhihu"),
        )
        hits = await adapter.search("query")

    expected_title = "示例标题" if successful_branch == "zhihu_search" else "全网示例标题"
    assert [hit.title for hit in hits] == ([] if empty else [expected_title])
    assert attempts[successful_branch] == 1
    assert attempts[failing_branch] == (2 if failing_branch == "global_search" else 1)
    partials = [
        record
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_partial_failure"
    ]
    assert len(partials) == 1
    assert partials[0].name == "agent_search_gateway.providers.web.zhihu"
    fields = vars(partials[0])["gateway_fields"]
    assert fields["provider"] == "zhihu"
    assert fields["stage"] == "search"
    assert fields["branch"] == failing_branch
    assert fields["error_type"] == (
        "HttpStatusFailure" if failing_branch == "global_search" else "ExecutionFailure"
    )
    assert fields["reason"] in {"execution_failure", "invalid_results_envelope"}
    assert not any(
        getattr(record, "gateway_event", None) in {"provider_completed", "provider_failed"}
        for record in caplog.records
    )


async def test_zhihu_aggregates_two_failures_using_fixed_first_branch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    global_finished = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        branch = request.url.path.rsplit("/", 1)[-1]
        if branch == "zhihu_search":
            await global_finished.wait()
            return httpx.Response(401)
        global_finished.set()
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="zhihu"),
        )
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.search("query")

    exc = caught.value
    assert exc.code == ErrorCode.ALL_PROVIDERS_FAILED
    assert exc.message == "zhihu/search: both search branches failed"
    assert exc.reason == "execution_failure"
    assert exc.diagnostics["status"] == 401
    assert exc.diagnostics["http_status"] == 401
    assert isinstance(exc.__cause__, ExecutionFailure)
    assert exc.__cause__.diagnostics["status"] == 401
    assert not any(
        getattr(record, "gateway_event", None)
        in {"provider_partial_failure", "provider_completed", "provider_failed"}
        for record in caplog.records
    )


@pytest.mark.parametrize("failing_branch", _BRANCHES)
@pytest.mark.parametrize("code", [17, True, False, "0", None, 0.0])
async def test_zhihu_rejects_non_success_codes_even_with_items(
    failing_branch: str, code: object, caplog: pytest.LogCaptureFixture
) -> None:
    # Constructed negative cases, not an observed global vendor error catalogue.
    caplog.set_level(logging.DEBUG)
    attempts = dict.fromkeys(_BRANCHES, 0)

    def respond(request: httpx.Request) -> httpx.Response:
        branch = request.url.path.rsplit("/", 1)[-1]
        attempts[branch] += 1
        payload = _fixture(branch, empty=branch != failing_branch)
        if branch == failing_branch:
            payload["Code"] = code
            payload["Message"] = "Synthetic business failure"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(3, 0, 0, 5), provider_name="zhihu"),
        )
        assert await adapter.search("query") == []

    assert attempts == dict.fromkeys(_BRANCHES, 1)
    partials = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_partial_failure"
    ]
    assert len(partials) == 1
    assert partials[0]["branch"] == failing_branch
    assert partials[0]["upstream_message"] == DiagnosticText("Synthetic business failure", 512)
    if type(code) in (int, str):
        assert partials[0]["upstream_code"] == DiagnosticText(str(code), 128)
    else:
        assert "upstream_code" not in partials[0]


@pytest.mark.parametrize("failing_branch", _BRANCHES)
@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "not-an-object",
        {},
        {"Data": {"Items": []}},
        {"Code": 0},
        {"Code": 0, "Data": None},
        {"Code": 0, "Data": []},
        {"Code": 0, "Data": {}},
        {"Code": 0, "Data": {"Items": None}},
        {"Code": 0, "Data": {"Items": {}}},
        {"Code": 0, "Data": {"Items": "not-a-list"}},
    ],
)
async def test_zhihu_invalid_envelope_is_a_branch_failure(
    failing_branch: str, payload: object, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    attempts = dict.fromkeys(_BRANCHES, 0)

    def respond(request: httpx.Request) -> httpx.Response:
        branch = request.url.path.rsplit("/", 1)[-1]
        attempts[branch] += 1
        body = payload if branch == failing_branch else _fixture(branch, empty=True)
        return httpx.Response(
            200, content=json.dumps(body), headers={"Content-Type": "application/json"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(3, 0, 0, 5), provider_name="zhihu"),
        )
        assert await adapter.search("query") == []

    partials = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_partial_failure"
    ]
    assert len(partials) == 1
    assert partials[0]["branch"] == failing_branch
    assert attempts == dict.fromkeys(_BRANCHES, 1)


@pytest.mark.parametrize("populated_branch", _BRANCHES)
async def test_zhihu_keeps_valid_siblings_and_counts_each_branch(
    populated_branch: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    items: list[object] = [
        {"Url": "https://example.test/first", "Title": "First", "ContentText": "First summary"},
        None,
        [],
        "not-an-object",
        {},
        {"Url": None},
        {"Url": " \t"},
        {"Url": 42},
        {"Url": "https://example.test/bad-title", "Title": []},
        {"Url": "https://example.test/bad-summary", "ContentText": {}},
        {"url": "https://example.test/ignored-alias"},
        {
            "Url": "https://example.test/last",
            "Title": "Last",
            "ContentText": "Last summary",
            "title": [],
            "content": {},
            "raw_content": "unconsumed",
        },
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        branch = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200, json={"Code": 0, "Data": {"Items": items if branch == populated_branch else []}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(3, 0, 0, 5), provider_name="zhihu"),
        )
        hits = await adapter.search("query")

    assert hits == [
        KeywordSearchHit("https://example.test/first", "First", "First summary"),
        KeywordSearchHit("https://example.test/last", "Last", "Last summary"),
    ]
    assert all(hit.raw_content == hit.content == "" for hit in hits)
    summaries = [
        record
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 2
    assert all(record.name == "agent_search_gateway.providers.web.zhihu" for record in summaries)
    by_branch = {
        vars(record)["gateway_fields"]["stage"]: vars(record)["gateway_fields"]
        for record in summaries
    }
    for branch in _BRANCHES:
        populated = branch == populated_branch
        assert by_branch[branch] == {
            "provider": "zhihu",
            "stage": branch,
            "phase": "adapter",
            "received": 12 if populated else 0,
            "accepted": 2 if populated else 0,
            "rejected": 10 if populated else 0,
            "rejection_counts": {
                "invalid_record_shape": 3,
                "missing_url": 4,
                "invalid_record": 3,
            }
            if populated
            else {},
            "rewritten": 0,
            "processed": 12 if populated else 0,
            "unprocessed": 0,
        }
    assert not any(
        getattr(record, "gateway_event", None)
        in {"provider_partial_failure", "provider_completed", "provider_failed"}
        for record in caplog.records
    )


@pytest.mark.parametrize(
    ("body", "category", "semantic_result"),
    [
        ("{bad-json", "decode", "unknown"),
        ("[]", "invalid_envelope", "unknown"),
        ("{}", "invalid_envelope", "unknown"),
        ('{"Code": false, "Data": {"Items": []}}', "invalid_envelope", "unknown"),
        ('{"Code": "0", "Data": {"Items": []}}', "invalid_envelope", "unknown"),
        ('{"Code": 0, "Data": []}', "invalid_envelope", "unknown"),
        ('{"Code": 0, "Data": {}}', "invalid_envelope", "unknown"),
        ('{"Code": 0, "Data": {"Items": {}}}', "invalid_envelope", "unknown"),
        (
            '{"Code": 91, "Message": "Synthetic failure", "Data": {"Items": []}}',
            "provider_response",
            "error",
        ),
        ('{"Code": 0, "Data": {"Items": []}}', None, "success"),
    ],
)
async def test_zhihu_response_diagnostics_classifies_only_known_envelope(
    body: str,
    category: str | None,
    semantic_result: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text=body, headers={"Content-Type": "application/json"})
        )
    ) as client:
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(3, 0, 0, 5), provider_name="zhihu"),
        )
        if semantic_result == "success":
            assert await adapter.search("query") == []
        else:
            with pytest.raises(ExecutionFailure):
                await adapter.search("query")

    responses = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(responses) == 2
    assert {fields["stage"] for fields in responses} == set(_BRANCHES)
    for fields in responses:
        assert fields.get("category") == category
        assert fields["semantic_result"] == semantic_result
        if category == "provider_response":
            assert fields["upstream_code"] == DiagnosticText("91", 128)
            assert fields["upstream_message"] == DiagnosticText("Synthetic failure", 512)
    summaries = [
        record
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == (2 if semantic_result == "success" else 0)


async def test_zhihu_success_logs_omit_query_items_and_unaudited_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = SecretValue("synthetic-zhihu-success-credential")
    logger, stream = structured_test_logger("test.zhihu.success", secrets=[credential])
    monkeypatch.setattr(zhihu, "_LOGGER", logger)
    query = "ZHIHU_QUERY_SENTINEL"
    body = "ZHIHU_ITEM_BODY_SENTINEL"
    payload = {
        "Code": 0,
        "Message": "ZHIHU_SUCCESS_MESSAGE_SENTINEL",
        "Query": query,
        "RequestId": "ZHIHU_UNAUDITED_REQUEST_ID_SENTINEL",
        "body": "ZHIHU_RESPONSE_BODY_SENTINEL",
        "Data": {
            "Items": [
                {"Url": "https://example.test/success", "ContentText": body},
                {"Url": "https://example.test/rejected", "Title": {"raw": body}},
            ],
            "Details": "ZHIHU_UNAUDITED_DETAIL_SENTINEL",
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=credential,
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(1, 0, 0, 5), provider_name="zhihu", logger=logger
            ),
        )
        hits = await adapter.search(query)

    assert hits == [KeywordSearchHit("https://example.test/success", snippet=body)] * 2
    rendered = stream.getvalue()
    assert rendered.count("event=provider_response_diagnostic") == 2
    assert rendered.count("event=provider_candidates_summary") == 2
    assert "received=2" in rendered and "accepted=1" in rendered and "rejected=1" in rendered
    assert "ZHIHU_" not in rendered
    assert credential.reveal() not in rendered


@pytest.mark.parametrize("failing_branch", _BRANCHES)
async def test_zhihu_partial_logs_redact_and_bound_selected_vendor_text(
    failing_branch: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = SecretValue("synthetic-zhihu-partial-credential")
    logger, stream = structured_test_logger("test.zhihu.partial", secrets=[credential])
    monkeypatch.setattr(zhihu, "_LOGGER", logger)
    payload = {
        "Code": 7,
        "Message": credential.reveal() + "界" * 513 + "MESSAGE_TAIL_SENTINEL",
        "Query": "ZHIHU_RESPONSE_QUERY_SENTINEL",
        "Details": "ZHIHU_ERROR_DETAIL_SENTINEL",
        "code": "ZHIHU_LOWERCASE_CODE_SENTINEL",
        "message": "ZHIHU_LOWERCASE_MESSAGE_SENTINEL",
        "Data": {
            "Items": [{"Url": "https://example.test/failure", "ContentText": "ZHIHU_BODY_SENTINEL"}]
        },
    }
    attempts = dict.fromkeys(_BRANCHES, 0)

    def respond(request: httpx.Request) -> httpx.Response:
        branch = request.url.path.rsplit("/", 1)[-1]
        attempts[branch] += 1
        return httpx.Response(
            200, json=payload if branch == failing_branch else _fixture(branch, empty=True)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=credential,
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(3, 0, 0, 5), provider_name="zhihu", logger=logger
            ),
        )
        assert await adapter.search("ZHIHU_REQUEST_QUERY_SENTINEL") == []

    assert attempts == dict.fromkeys(_BRANCHES, 1)
    rendered = stream.getvalue()
    assert rendered.count("event=provider_partial_failure") == 1
    assert rendered.count("event=provider_response_diagnostic") == 2
    assert rendered.count("event=provider_candidates_summary") == 1
    assert f"branch={failing_branch}" in rendered
    assert "upstream_code=7" in rendered
    assert "upstream_message=<redacted>" in rendered
    assert "upstream_message_truncated=true" in rendered
    assert len(rendered.split("upstream_message=", 1)[1].split()[0]) == 512
    assert "界" * 512 not in rendered
    assert credential.reveal() not in rendered
    assert "ZHIHU_" not in rendered
    assert "MESSAGE_TAIL_SENTINEL" not in rendered
    assert "Traceback" not in rendered
    assert "event=provider_completed" not in rendered
    assert "event=provider_failed" not in rendered


async def test_zhihu_double_failure_logs_keep_safe_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = SecretValue("synthetic-zhihu-double-credential")
    logger, stream = structured_test_logger("test.zhihu.double", secrets=[credential])
    monkeypatch.setattr(zhihu, "_LOGGER", logger)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/zhihu_search"):
            return httpx.Response(
                200,
                json={
                    "Code": 11,
                    "Message": "Synthetic site failure",
                    "Query": "ZHIHU_DOUBLE_RESPONSE_QUERY_SENTINEL",
                    "Details": "ZHIHU_DOUBLE_DETAIL_SENTINEL",
                    "Data": {"Items": ["ZHIHU_DOUBLE_ITEMS_SENTINEL"]},
                },
            )
        return httpx.Response(503, text="ZHIHU_DOUBLE_HTTP_BODY_SENTINEL")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=credential,
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(1, 0, 0, 5), provider_name="zhihu", logger=logger
            ),
        )
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.search("ZHIHU_DOUBLE_REQUEST_QUERY_SENTINEL")

    assert caught.value.message == "zhihu/search: both search branches failed"
    assert caught.value.diagnostics == {
        "upstream_code": DiagnosticText("11", 128),
        "upstream_message": DiagnosticText("Synthetic site failure", 512),
    }
    assert isinstance(caught.value.__cause__, ExecutionFailure)
    rendered = stream.getvalue()
    assert rendered.count("event=provider_response_diagnostic") == 2
    assert "upstream_code=11" in rendered
    assert 'upstream_message="Synthetic site failure"' in rendered
    assert credential.reveal() not in rendered
    assert "ZHIHU_" not in rendered
    assert "Traceback" not in rendered
    for event in (
        "provider_partial_failure",
        "provider_candidates_summary",
        "provider_completed",
        "provider_failed",
    ):
        assert f"event={event}" not in rendered


@pytest.mark.parametrize("both_fail", [False, True])
async def test_zhihu_ordinary_failure_never_renders_arbitrary_exception_text(
    both_fail: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnsafeError(Exception):
        def __str__(self) -> str:
            raise AssertionError("arbitrary exception text must not be rendered")

    first = UnsafeError("ZHIHU_EXCEPTION_TEXT_SENTINEL")
    executor = RecordingJsonExecutor(
        [
            first,
            RuntimeError("ZHIHU_SIBLING_EXCEPTION_SENTINEL")
            if both_fail
            else _fixture("global_search", empty=True),
        ]
    )
    logger, stream = structured_test_logger("test.zhihu.ordinary")
    monkeypatch.setattr(zhihu, "_LOGGER", logger)
    adapter = ZhihuAdapter(name="zhihu", secret=SecretValue("x"), http_executor=executor)
    if both_fail:
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.search("ZHIHU_QUERY_SENTINEL")
        assert caught.value.__cause__ is first
        assert caught.value.diagnostics == {}
        assert caught.value.reason == "execution_failure"
    else:
        assert await adapter.search("ZHIHU_QUERY_SENTINEL") == []

    assert len(executor.requests) == 2
    rendered = stream.getvalue()
    assert rendered.count("event=provider_partial_failure") == (0 if both_fail else 1)
    if not both_fail:
        assert "branch=zhihu_search" in rendered
        assert "error_type=UnsafeError" in rendered
        assert "reason=execution_failure" in rendered
    assert "ZHIHU_" not in rendered
    assert "Traceback" not in rendered
    assert "event=provider_completed" not in rendered
    assert "event=provider_failed" not in rendered


async def test_zhihu_diagnostic_callback_failure_does_not_change_success(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Response] = []

    def broken_diagnostics(response: httpx.Response) -> dict[str, object]:
        calls.append(response)
        raise RuntimeError("synthetic diagnostic-only failure")

    monkeypatch.setattr(zhihu, "_response_diagnostics", broken_diagnostics)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_fixture(request.url.path.rsplit("/", 1)[-1]))
        )
    ) as client:
        adapter = ZhihuAdapter(
            name="zhihu",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(3, 0, 0, 5), provider_name="zhihu"),
        )
        hits = await adapter.search("query")

    assert [hit.title for hit in hits] == ["示例标题", "全网示例标题"]
    assert len(calls) == 2
    assert (
        sum(
            getattr(record, "gateway_event", None) == "provider_candidates_summary"
            for record in caplog.records
        )
        == 2
    )
