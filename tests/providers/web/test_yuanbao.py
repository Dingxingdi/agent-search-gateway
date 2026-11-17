import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import DiagnosticText, SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, KeywordSearchProvider
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.web import yuanbao
from agent_search_gateway.providers.web.yuanbao import YuanbaoAdapter, _response_diagnostics
from tests.support.http import RecordingJsonExecutor
from tests.support.logging import structured_test_logger

_FIXTURE = Path(__file__).parents[2] / "fixtures/providers/yuanbao/search.json"


async def test_yuanbao_search_uses_service_key_and_maps_pages() -> None:
    query = "  示例查询 & original + /?  "
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url == httpx.URL("https://api.wsa.cloud.tencent.com/SearchPro")
        assert request.headers["Authorization"] == "Bearer service-key"
        assert request.headers["Content-Type"] == "application/json"
        assert json.loads(request.content) == {"Query": query}
        return httpx.Response(200, json=json.loads(_FIXTURE.read_text(encoding="utf-8")))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        registration = build_default_registry().require("yuanbao")
        adapter = registration.factory(
            name="yuanbao",
            secret=SecretValue("service-key"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(1, 0, 0, 5), provider_name="yuanbao"
            ),
        )
        assert isinstance(adapter, KeywordSearchProvider)
        hits = await adapter.search(query)

    assert hits == [
        KeywordSearchHit("https://example.com/article", "示例标题", "动态摘要"),
        KeywordSearchHit("https://example.org/article", "另一篇示例", "备用摘要"),
    ]
    assert all(hit.raw_content == hit.content == "" for hit in hits)
    assert len(requests) == 1
    assert not hasattr(adapter, "fetch")


@pytest.mark.parametrize(
    ("api_url", "expected_url"),
    [
        (None, "https://api.wsa.cloud.tencent.com/SearchPro"),
        ("https://api.wsa.cloud.tencent.com", "https://api.wsa.cloud.tencent.com/SearchPro"),
        ("https://yuanbao.example.test", "https://yuanbao.example.test/SearchPro"),
        ("https://yuanbao.example.test/proxy", "https://yuanbao.example.test/proxy/SearchPro"),
        ("https://yuanbao.example.test/proxy/", "https://yuanbao.example.test/proxy/SearchPro"),
    ],
)
async def test_yuanbao_appends_search_path_to_default_or_custom_base(
    api_url: str | None, expected_url: str
) -> None:
    executor = RecordingJsonExecutor([json.loads(_FIXTURE.read_text(encoding="utf-8"))])
    options = {} if api_url is None else {"api_url": api_url}
    adapter = YuanbaoAdapter(
        name="yuanbao", secret=SecretValue("x"), http_executor=executor, **options
    )

    hits = await adapter.search("query")

    assert len(hits) == 2
    assert executor.requests[0].url == expected_url


@pytest.mark.parametrize("content", [None, " \t\n"])
async def test_yuanbao_uses_passage_when_content_is_missing_or_whitespace(
    content: str | None,
) -> None:
    page = {"url": "https://example.com/passage", "passage": "Fallback summary"}
    if content is not None:
        page["content"] = content
    executor = RecordingJsonExecutor([{"Response": {"Pages": [json.dumps(page)]}}])
    adapter = YuanbaoAdapter(name="yuanbao", secret=SecretValue("x"), http_executor=executor)

    assert await adapter.search("query") == [
        KeywordSearchHit(url="https://example.com/passage", snippet="Fallback summary")
    ]


async def test_yuanbao_accepts_successful_empty_pages(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    executor = RecordingJsonExecutor([{"Response": {"Query": "query", "Pages": []}}])
    adapter = YuanbaoAdapter(name="yuanbao", secret=SecretValue("x"), http_executor=executor)

    assert await adapter.search("query") == []
    assert len(executor.requests) == 1
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert summaries == [
        {
            "provider": "yuanbao",
            "stage": "search",
            "phase": "adapter",
            "received": 0,
            "accepted": 0,
            "rejected": 0,
            "rejection_counts": {},
            "rewritten": 0,
            "processed": 0,
            "unprocessed": 0,
        }
    ]


@pytest.mark.parametrize("include_pages", [True, False], ids=["empty-pages", "missing-pages"])
async def test_yuanbao_business_error_precedes_pages_validation(
    include_pages: bool, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    payload = json.loads(_FIXTURE.with_name("error.json").read_text(encoding="utf-8"))
    if include_pages:
        payload["Response"]["Pages"] = []
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = YuanbaoAdapter(
            name="yuanbao",
            secret=SecretValue("service-key"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(3, 0, 0, 5), provider_name="yuanbao"
            ),
        )
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.search("query")

    assert type(caught.value) is ExecutionFailure
    assert caught.value.message == "yuanbao/search: provider reported failure"
    assert caught.value.diagnostics["upstream_code"] == DiagnosticText("RequestLimitExceeded", 128)
    assert caught.value.diagnostics["upstream_message"] == DiagnosticText(
        "Request limit exceeded.", 512
    )
    assert len(requests) == 1
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["category"] == "provider_response"
    assert diagnostics[0]["semantic_result"] == "error"
    assert diagnostics[0]["upstream_code"] == caught.value.diagnostics["upstream_code"]
    assert diagnostics[0]["upstream_message"] == caught.value.diagnostics["upstream_message"]


@pytest.mark.parametrize("error", [None, False, "", [], "malformed", {}, {"Code": []}])
async def test_yuanbao_malformed_error_never_becomes_empty_success(error: object) -> None:
    executor = RecordingJsonExecutor([{"Response": {"Error": error, "Pages": []}}])
    adapter = YuanbaoAdapter(name="yuanbao", secret=SecretValue("x"), http_executor=executor)

    with pytest.raises(ExecutionFailure, match="provider reported failure") as caught:
        await adapter.search("query")

    assert caught.value.diagnostics == {}
    assert len(executor.requests) == 1
    assert _response_diagnostics(
        httpx.Response(200, json={"Response": {"Error": error, "Pages": []}})
    ) == {
        "category": "provider_response",
        "semantic_result": "error",
        "response_kind": "object",
    }


@pytest.mark.parametrize("payload", [None, [], "invalid", {}, {"Response": None}, {"Response": []}])
async def test_yuanbao_requires_outer_and_response_objects(payload: object) -> None:
    executor = RecordingJsonExecutor([payload])
    adapter = YuanbaoAdapter(name="yuanbao", secret=SecretValue("x"), http_executor=executor)

    with pytest.raises(ExecutionFailure, match="must be an object"):
        await adapter.search("query")

    assert len(executor.requests) == 1


@pytest.mark.parametrize("response", [{}, {"Pages": None}, {"Pages": {}}, {"Pages": "[]"}])
async def test_yuanbao_requires_pages_array(response: object) -> None:
    executor = RecordingJsonExecutor([{"Response": response}])
    adapter = YuanbaoAdapter(name="yuanbao", secret=SecretValue("x"), http_executor=executor)

    with pytest.raises(ExecutionFailure, match="Response.Pages must be an array"):
        await adapter.search("query")

    assert len(executor.requests) == 1


def test_yuanbao_response_diagnostics_classifies_decode_failure() -> None:
    assert _response_diagnostics(httpx.Response(200, content=b"not-json")) == {
        "category": "decode",
        "semantic_result": "unknown",
    }


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "invalid",
        {},
        {"Response": None},
        {"Response": []},
        {"Response": {}},
        {"Response": {"Pages": None}},
        {"Response": {"Pages": {}}},
        {"Response": {"Pages": "[]"}},
    ],
)
def test_yuanbao_response_diagnostics_classifies_invalid_envelope(payload: object) -> None:
    assert _response_diagnostics(httpx.Response(200, content=json.dumps(payload))) == {
        "category": "invalid_envelope",
        "semantic_result": "unknown",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"Response": {"Pages": []}},
        json.loads(_FIXTURE.read_text(encoding="utf-8")),
    ],
)
def test_yuanbao_response_diagnostics_classifies_success(payload: object) -> None:
    assert _response_diagnostics(httpx.Response(200, json=payload)) == {
        "semantic_result": "success",
        "response_kind": "object",
    }


def test_yuanbao_response_diagnostics_selects_only_known_error_fields() -> None:
    payload = {
        "Response": {
            "Error": {
                "Code": "RequestLimitExceeded",
                "Message": "Request limit exceeded.",
                "detail": {"message": "arbitrary detail"},
                "code": "ignored lowercase code",
            },
            "Query": "original query",
            "RequestId": "request identifier",
            "Pages": [],
        },
        "message": "arbitrary root message",
    }

    assert _response_diagnostics(httpx.Response(200, json=payload)) == {
        "category": "provider_response",
        "semantic_result": "error",
        "response_kind": "object",
        "upstream_code": DiagnosticText("RequestLimitExceeded", 128),
        "upstream_message": DiagnosticText("Request limit exceeded.", 512),
    }


async def test_yuanbao_keeps_valid_pages_and_counts_rejections(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    pages: list[object] = [
        json.dumps({"url": "https://example.test/first", "content": "First summary"}),
        "{bad-json",
        "[]",
        "42",
        {"url": "https://example.test/not-encoded"},
        None,
        json.dumps({}),
        json.dumps({"url": " \t"}),
        json.dumps({"url": 42}),
        json.dumps({"url": "https://example.test/bad-title", "title": []}),
        json.dumps({"url": "https://example.test/bad-content", "content": {}}),
        json.dumps({"url": "https://example.test/bad-passage", "passage": 7}),
        json.dumps({"url": "https://example.test/last", "passage": "Last summary"}),
    ]
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"Response": {"Pages": pages}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = YuanbaoAdapter(
            name="yuanbao",
            secret=SecretValue("synthetic-yuanbao-key"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(3, 0, 0, 5), provider_name="yuanbao"
            ),
        )
        hits = await adapter.search("query")

    assert hits == [
        KeywordSearchHit("https://example.test/first", snippet="First summary"),
        KeywordSearchHit("https://example.test/last", snippet="Last summary"),
    ]
    assert all(hit.raw_content == hit.content == "" for hit in hits)
    assert len(requests) == 1
    summaries = [
        record
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0].name == "agent_search_gateway.providers.web.yuanbao"
    assert vars(summaries[0])["gateway_fields"] == {
        "provider": "yuanbao",
        "stage": "search",
        "phase": "adapter",
        "received": 13,
        "accepted": 2,
        "rejected": 11,
        "rejection_counts": {
            "decode_error": 1,
            "invalid_record_shape": 4,
            "missing_url": 2,
            "invalid_record": 4,
        },
        "rewritten": 0,
        "processed": 13,
        "unprocessed": 0,
    }


async def test_yuanbao_success_logs_omit_query_pages_and_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = SecretValue("synthetic-yuanbao-log-credential")
    logger, stream = structured_test_logger("test.yuanbao.success", secrets=[credential])
    monkeypatch.setattr(yuanbao, "_LOGGER", logger)
    query = "YUANBAO_QUERY_SENTINEL"
    body = "YUANBAO_PAGE_BODY_SENTINEL"
    request_id = "YUANBAO_UNAUDITED_REQUEST_ID"
    payload = {
        "Response": {
            "Query": query,
            "RequestId": request_id,
            "Pages": [
                json.dumps({"url": "https://example.test/summary", "content": body}),
                "{" + body,
            ],
        },
        "body": "YUANBAO_WHOLE_RESPONSE_SENTINEL",
    }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        adapter = YuanbaoAdapter(
            name="yuanbao",
            secret=credential,
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(1, 0, 0, 5), provider_name="yuanbao", logger=logger
            ),
        )
        hits = await adapter.search(query)

    assert hits == [KeywordSearchHit("https://example.test/summary", snippet=body)]
    assert hits[0].raw_content == hits[0].content == ""
    rendered = stream.getvalue()
    assert "event=provider_response_diagnostic" in rendered
    assert "event=provider_candidates_summary" in rendered
    assert "received=2" in rendered and "accepted=1" in rendered and "rejected=1" in rendered
    for sentinel in (
        credential.reveal(),
        query,
        body,
        request_id,
        "YUANBAO_WHOLE_RESPONSE_SENTINEL",
    ):
        assert sentinel not in rendered


async def test_yuanbao_error_logs_bound_and_redact_selected_vendor_fields() -> None:
    credential = SecretValue("synthetic-yuanbao-error-credential")
    logger, stream = structured_test_logger("test.yuanbao.error", secrets=[credential])
    payload = {
        "Response": {
            "Error": {
                "Code": credential.reveal() + "c" * 129 + "CODE_TAIL_SENTINEL",
                "Message": credential.reveal() + "界" * 513 + "MESSAGE_TAIL_SENTINEL",
                "Details": "YUANBAO_ERROR_DETAIL_SENTINEL",
            },
            "Query": "YUANBAO_ERROR_QUERY_SENTINEL",
            "RequestId": "YUANBAO_ERROR_REQUEST_ID_SENTINEL",
            "Pages": ["YUANBAO_ERROR_BODY_SENTINEL"],
        }
    }
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = YuanbaoAdapter(
            name="yuanbao",
            secret=credential,
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(3, 0, 0, 5), provider_name="yuanbao", logger=logger
            ),
        )
        with pytest.raises(ExecutionFailure, match="provider reported failure"):
            await adapter.search("YUANBAO_REQUEST_QUERY_SENTINEL")

    assert len(requests) == 1
    rendered = stream.getvalue()
    assert "semantic_result=error" in rendered
    assert "upstream_code=<redacted>" in rendered
    assert "upstream_message=<redacted>" in rendered
    assert len(rendered.split("upstream_code=", 1)[1].split()[0]) == 128
    assert len(rendered.split("upstream_message=", 1)[1].split()[0]) == 512
    assert "upstream_message_truncated=true" in rendered
    assert "c" * 128 not in rendered
    assert "界" * 512 not in rendered
    for sentinel in (
        credential.reveal(),
        "CODE_TAIL_SENTINEL",
        "MESSAGE_TAIL_SENTINEL",
        "YUANBAO_ERROR_DETAIL_SENTINEL",
        "YUANBAO_ERROR_QUERY_SENTINEL",
        "YUANBAO_ERROR_REQUEST_ID_SENTINEL",
        "YUANBAO_ERROR_BODY_SENTINEL",
        "YUANBAO_REQUEST_QUERY_SENTINEL",
    ):
        assert sentinel not in rendered


async def test_yuanbao_diagnostic_callback_failure_does_not_change_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def broken_diagnostics(_: httpx.Response) -> dict[str, object]:
        raise RuntimeError("synthetic diagnostic-only failure")

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"Response": {"Pages": []}})

    monkeypatch.setattr(yuanbao, "_response_diagnostics", broken_diagnostics)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = YuanbaoAdapter(
            name="yuanbao",
            secret=SecretValue("synthetic-yuanbao-callback-key"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(3, 0, 0, 5), provider_name="yuanbao"
            ),
        )
        assert await adapter.search("query") == []

    assert len(requests) == 1


async def test_yuanbao_http_429_retries_only_through_shared_executor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(
            200,
            json={
                "Response": {
                    "Pages": [
                        "{bad-json",
                        json.dumps(
                            {
                                "url": "https://example.test/recovered",
                                "content": "Preferred summary",
                                "passage": ["unconsumed optional field"],
                            }
                        ),
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = YuanbaoAdapter(
            name="yuanbao",
            secret=SecretValue("synthetic-yuanbao-retry-key"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(3, 0.1, 1, 5), provider_name="yuanbao", sleep=sleep
            ),
        )
        hits = await adapter.search("query")

    assert hits == [KeywordSearchHit("https://example.test/recovered", snippet="Preferred summary")]
    assert hits[0].raw_content == hits[0].content == ""
    assert len(requests) == 2
    assert sleeps == [2.0]
    retries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "http_retrying"
    ]
    assert len(retries) == 1
    assert retries[0]["delay_source"] == "retry_after"
    assert retries[0]["delay_ms"] == 2000
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["received"] == 2
    assert summaries[0]["accepted"] == summaries[0]["rejected"] == 1
    assert summaries[0]["rejection_counts"] == {"decode_error": 1}
    assert summaries[0]["unprocessed"] == 0


async def test_yuanbao_propagates_request_cancellation_without_candidate_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    cancellation = asyncio.CancelledError()
    executor = RecordingJsonExecutor([cancellation])
    adapter = YuanbaoAdapter(
        name="yuanbao", secret=SecretValue("synthetic-yuanbao-cancel-key"), http_executor=executor
    )

    with pytest.raises(asyncio.CancelledError) as caught:
        await adapter.search("query")

    assert caught.value is cancellation
    assert len(executor.requests) == 1
    assert not any(
        getattr(record, "gateway_event", None) == "provider_candidates_summary"
        for record in caplog.records
    )
