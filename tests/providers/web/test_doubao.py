"""Offline Doubao contracts using neutral synthetic response fixtures."""

import asyncio
import json
import logging
import shlex
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.errors import ExecutionFailure, ProtocolFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import DiagnosticText, SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit
from agent_search_gateway.providers.http import HttpJsonExecutor, ProviderResponseFailure
from agent_search_gateway.providers.web.doubao import DoubaoAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor
from tests.support.logging import structured_test_logger

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "doubao"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text())


async def test_search_maps_web_results() -> None:
    http = RecordingJsonExecutor([_fixture("search_success.json")])
    adapter = DoubaoAdapter(name="doubao", secret=SecretValue("synthetic-key"), http_executor=http)
    hits = await adapter.search("合成查询")
    assert hits == [
        KeywordSearchHit("https://EXAMPLE.com/one#section", "First page", "First summary"),
        KeywordSearchHit("https://example.com/two", "Second page", "Second snippet"),
    ]
    assert all(hit.raw_content == hit.content == "" for hit in hits)


async def test_search_sends_minimal_custom_request() -> None:
    for root in ("https://example.com/api", "https://example.com/api/"):
        http = RecordingJsonExecutor([_fixture("search_success.json")])
        adapter = DoubaoAdapter(
            name="doubao", api_url=root, secret=SecretValue("synthetic-key"), http_executor=http
        )
        await adapter.search("合成查询")
        assert len(http.requests) == 1
        request = http.requests[0]
        assert (request.method, request.url, request.stage) == (
            "POST",
            "https://example.com/api/web_search",
            "search",
        )
        assert request.headers == {"Authorization": "Bearer synthetic-key"}
        assert request.json_body == {"Query": "合成查询", "SearchType": "web"}
        assert request.params is None


async def test_search_returns_empty_for_valid_empty_results() -> None:
    http = RecordingJsonExecutor([_fixture("search_empty.json")])
    adapter = DoubaoAdapter(name="doubao", secret=SecretValue("synthetic-key"), http_executor=http)
    assert await adapter.search("合成查询") == []
    assert len(http.requests) == 1


async def test_search_rejects_only_invalid_records(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    http = RecordingJsonExecutor([_fixture("search_partial.json")])
    adapter = DoubaoAdapter(name="doubao", secret=SecretValue("synthetic-key"), http_executor=http)
    assert await adapter.search("合成查询") == [
        KeywordSearchHit("https://example.com/good", "Good", "Summary")
    ]
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert summaries == [
        {
            "provider": "doubao",
            "stage": "search",
            "phase": "adapter",
            "received": 10,
            "accepted": 1,
            "rejected": 9,
            "rejection_counts": {"missing_url": 3, "invalid_record": 6},
            "processed": 10,
            "unprocessed": 0,
            "rewritten": 0,
        }
    ]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "invalid",
        {},
        {"Result": None},
        {"Result": []},
        {"Result": {}},
        {"Result": {"WebResults": {}}},
        {"ResponseMetadata": None, "Result": {"WebResults": []}},
        {"ResponseMetadata": [], "Result": {"WebResults": []}},
    ],
)
async def test_search_fails_on_invalid_envelope(payload: object) -> None:
    http = RecordingJsonExecutor([payload])
    adapter = DoubaoAdapter(name="doubao", secret=SecretValue("synthetic-key"), http_executor=http)
    with pytest.raises(ExecutionFailure):
        await adapter.search("合成查询")


@pytest.mark.parametrize(
    "error", [{"Code": "700429"}, {"CodeN": 700429}, {"Code": "10500", "CodeN": 10500}]
)
async def test_search_retries_documented_business_codes(error: dict[str, object]) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"ResponseMetadata": {"Error": error}})
        return httpx.Response(200, json=_fixture("search_success.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(2, 0, 0, 5), provider_name="doubao")
        adapter = DoubaoAdapter(
            name="doubao", secret=SecretValue("synthetic-key"), http_executor=executor
        )
        hits = await adapter.search("合成查询")
    assert len(hits) == 2
    assert len(requests) == 2


@pytest.mark.parametrize(
    "error",
    [
        {"Code": "10400"},
        {"Code": "10403"},
        {"Code": "10406"},
        {"CodeN": 10409},
        {"Code": "10413"},
        {"Code": "10502"},
        {"Code": "PRIVATE_UNKNOWN"},
        {"Code": "99999"},
        {"Code": "700429", "CodeN": 10400},
        {"Code": 700429},
        {"CodeN": "700429"},
        {"Code": "700429", "CodeN": True},
        {"Code": "700429", "CodeN": None},
        {"Code": None, "CodeN": 700429},
        {"CodeN": True},
        {"Message": "PRIVATE_ERROR"},
        "PRIVATE_ERROR",
    ],
)
async def test_search_does_not_retry_permanent_or_unknown_business_error(error: object) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"ResponseMetadata": {"Error": error}, "Result": {"WebResults": []}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(3, 0, 0, 5), provider_name="doubao")
        adapter = DoubaoAdapter(
            name="doubao", secret=SecretValue("synthetic-key"), http_executor=executor
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await adapter.search("合成查询")
    assert len(requests) == 1
    assert caught.value.retryable is False
    assert caught.value.safe_reason == "provider_reported_failure"
    assert "PRIVATE_" not in str(caught.value) + str(caught.value.diagnostics)


@pytest.mark.parametrize(
    "fixture_name,category,semantic",
    [
        ("search_success.json", None, "success"),
        ("search_malformed.json", "invalid_envelope", "unknown"),
        ("search_error.json", "provider_response", "error"),
    ],
)
async def test_search_emits_safe_response_diagnostics(
    fixture_name: str,
    category: str | None,
    semantic: str,
) -> None:
    logger, stream = structured_test_logger("tests.doubao.safe-response")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=_fixture(fixture_name))
        )
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0, 0, 5), provider_name="doubao", logger=logger
        )
        adapter = DoubaoAdapter(
            name="doubao", secret=SecretValue("PRIVATE_KEY"), http_executor=executor
        )
        if fixture_name == "search_success.json":
            assert len(await adapter.search("PRIVATE_QUERY")) == 2
        else:
            with pytest.raises(ExecutionFailure):
                await adapter.search("PRIVATE_QUERY")
    diagnostics = [
        dict(token.split("=", 1) for token in shlex.split(line)[1:])
        for line in stream.getvalue().splitlines()
        if "event=provider_response_diagnostic " in line
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["semantic_result"] == semantic
    assert diagnostics[0]["response_kind"] == "object"
    if category is not None:
        assert diagnostics[0]["category"] == category
    if fixture_name == "search_error.json":
        assert diagnostics[0]["upstream_code"] == "10403"
    assert "PRIVATE_" not in stream.getvalue()


async def test_search_rejects_metadata_error_before_parsing_empty_results() -> None:
    http = RecordingJsonExecutor([_fixture("search_error.json")])
    adapter = DoubaoAdapter(name="doubao", secret=SecretValue("synthetic-key"), http_executor=http)
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.search("合成查询")
    assert "PRIVATE_" not in str(caught.value) + str(caught.value.diagnostics)


@pytest.mark.parametrize("summary", [None, "", " \n\t"])
async def test_search_falls_back_only_for_absent_or_empty_summary(summary: object) -> None:
    http = RecordingJsonExecutor(
        [
            {
                "Result": {
                    "WebResults": [
                        {
                            "Url": "https://example.com/fallback",
                            "Summary": summary,
                            "Snippet": "Fallback",
                        }
                    ]
                }
            }
        ]
    )
    adapter = DoubaoAdapter(name="doubao", secret=SecretValue("synthetic-key"), http_executor=http)
    assert await adapter.search("合成查询") == [
        KeywordSearchHit("https://example.com/fallback", "", "Fallback")
    ]
    assert http.requests[0].url == "https://open.feedcoopapi.com/search_api/web_search"


async def test_fetch_maps_content_text() -> None:
    target = normalize_url("https://EXAMPLE.com/article?q=synthetic#section")
    for root in ("https://example.com/api", "https://example.com/api/"):
        http = RecordingJsonExecutor([_fixture("fetch_success.json")])
        adapter = DoubaoAdapter(
            name="doubao", api_url=root, secret=SecretValue("synthetic-key"), http_executor=http
        )
        candidate = await adapter.fetch(target)
        assert (
            candidate.raw_content
            == candidate.content
            == ("  First synthetic paragraph.\n\nSecond synthetic paragraph.\n")
        )
        assert len(http.requests) == 1
        request = http.requests[0]
        assert (request.method, request.url, request.stage) == (
            "POST",
            "https://example.com/api/fetch",
            "fetch",
        )
        assert request.headers == {"Authorization": "Bearer synthetic-key"}
        assert request.json_body == {"Url": str(target)}
        assert request.params is None


@pytest.mark.parametrize("status_code", [403, 404, 500, 199, 300])
async def test_fetch_rejects_failed_origin_status(status_code: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "Result": {
                    "Data": {"StatusCode": status_code, "ContentText": "Synthetic error page"}
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(3, 0, 0, 5), provider_name="doubao")
        adapter = DoubaoAdapter(
            name="doubao", secret=SecretValue("synthetic-key"), http_executor=executor
        )
        with pytest.raises(ExecutionFailure):
            await adapter.fetch(normalize_url("https://example.com/article"))
    assert len(requests) == 1


@pytest.mark.parametrize(
    "data",
    [
        {"ContentText": "Synthetic content"},
        {"StatusCode": None, "ContentText": "Synthetic content"},
        {"StatusCode": "200", "ContentText": "Synthetic content"},
        {"StatusCode": True, "ContentText": "Synthetic content"},
        {"StatusCode": {}, "ContentText": "Synthetic content"},
        {"StatusCode": 200.0, "ContentText": "Synthetic content"},
    ],
)
async def test_fetch_rejects_invalid_status_code(data: dict[str, object]) -> None:
    http = RecordingJsonExecutor([{"Result": {"Data": data}}])
    adapter = DoubaoAdapter(name="doubao", secret=SecretValue("synthetic-key"), http_executor=http)
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.fetch(normalize_url("https://example.com/article"))
    assert caught.value.diagnostics["category"] == "invalid_envelope"
    assert caught.value.diagnostics["semantic_result"] == "error"
    assert len(http.requests) == 1


@pytest.mark.parametrize(
    "data",
    [
        {"StatusCode": 200},
        *({"StatusCode": 200, "ContentText": content} for content in (None, "", " \n\t")),
    ],
)
async def test_fetch_rejects_empty_content(data: dict[str, object]) -> None:
    http = RecordingJsonExecutor([{"Result": {"Data": data}}])
    adapter = DoubaoAdapter(name="doubao", secret=SecretValue("synthetic-key"), http_executor=http)
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.fetch(normalize_url("https://example.com/article"))
    assert caught.value.diagnostics.get("semantic_result") == "empty"


@pytest.mark.parametrize("content", [200, True, {}, [], ["Synthetic content"]])
async def test_fetch_rejects_invalid_content_type(content: object) -> None:
    http = RecordingJsonExecutor(
        [{"Result": {"Data": {"StatusCode": 200, "ContentText": content}}}]
    )
    adapter = DoubaoAdapter(name="doubao", secret=SecretValue("synthetic-key"), http_executor=http)
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.fetch(normalize_url("https://example.com/article"))
    assert caught.value.diagnostics.get("category") == "invalid_envelope"
    assert caught.value.diagnostics.get("semantic_result") == "error"


@pytest.mark.parametrize(
    "error", [{"Code": "700429"}, {"CodeN": 10500}, {"Code": "10502", "CodeN": 10502}]
)
async def test_fetch_retries_documented_temporary_errors(error: dict[str, object]) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"ResponseMetadata": {"Error": error}})
        return httpx.Response(200, json=_fixture("fetch_success.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(2, 0, 0, 5), provider_name="doubao")
        adapter = DoubaoAdapter(
            name="doubao", secret=SecretValue("synthetic-key"), http_executor=executor
        )
        candidate = await adapter.fetch(normalize_url("https://example.com/article"))
    assert (
        candidate.raw_content
        == candidate.content
        == ("  First synthetic paragraph.\n\nSecond synthetic paragraph.\n")
    )
    assert len(requests) == 2


@pytest.mark.parametrize("error", [{"CodeN": 10409}, {"Code": "10413"}])
async def test_fetch_rejects_subscription_key_error_without_retry(error: dict[str, object]) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "ResponseMetadata": {"Error": error},
                "Result": {"Data": {"StatusCode": 200, "ContentText": "Synthetic content"}},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(3, 0, 0, 5), provider_name="doubao")
        adapter = DoubaoAdapter(
            name="doubao", secret=SecretValue("synthetic-key"), http_executor=executor
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await adapter.fetch(normalize_url("https://example.com/article"))
    assert len(requests) == 1
    assert caught.value.retryable is False
    assert caught.value.safe_reason == "provider_reported_failure"
    code = caught.value.diagnostics.get("upstream_code")
    assert isinstance(code, DiagnosticText)
    assert code.text == str(error.get("CodeN", error.get("Code")))


async def test_fetch_business_retries_share_http_attempt_budget() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, json={"ResponseMetadata": {"Error": {"Code": "10409"}}})
        return httpx.Response(200, json={"ResponseMetadata": {"Error": {"Code": "10502"}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(2, 0, 0, 5), provider_name="doubao")
        adapter = DoubaoAdapter(
            name="doubao", secret=SecretValue("synthetic-key"), http_executor=executor
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await adapter.fetch(normalize_url("https://example.com/article"))
    assert len(requests) == 2
    assert caught.value.retryable is True


async def test_fetch_rejects_metadata_error_before_parsing_content() -> None:
    http = RecordingJsonExecutor([_fixture("fetch_error.json")])
    adapter = DoubaoAdapter(name="doubao", secret=SecretValue("synthetic-key"), http_executor=http)
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.fetch(normalize_url("https://example.com/article"))
    assert caught.value.diagnostics.get("category") == "provider_response"
    assert caught.value.diagnostics.get("semantic_result") == "error"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "invalid",
        {},
        {"Result": None},
        {"Result": []},
        {"Result": {}},
        {"Result": {"Data": None}},
        {"Result": {"Data": []}},
        {
            "ResponseMetadata": None,
            "Result": {"Data": {"StatusCode": 200, "ContentText": "Synthetic"}},
        },
        {
            "ResponseMetadata": [],
            "Result": {"Data": {"StatusCode": 200, "ContentText": "Synthetic"}},
        },
    ],
)
async def test_fetch_rejects_invalid_envelope(payload: object) -> None:
    http = RecordingJsonExecutor([payload])
    adapter = DoubaoAdapter(name="doubao", secret=SecretValue("synthetic-key"), http_executor=http)
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.fetch(normalize_url("https://example.com/article"))
    assert caught.value.diagnostics.get("category") == "invalid_envelope"


@pytest.mark.parametrize(
    "fixture_name,category,semantic,upstream_code",
    [
        ("fetch_success.json", None, "success", None),
        ("fetch_empty.json", "invalid_envelope", "empty", None),
        ("fetch_error.json", "provider_response", "error", "10409"),
    ],
)
async def test_fetch_response_diagnostics_match_parser(
    fixture_name: str, category: str | None, semantic: str, upstream_code: str | None
) -> None:
    payload = _fixture(fixture_name)
    parser_http = RecordingJsonExecutor([payload])
    parser_adapter = DoubaoAdapter(
        name="doubao", secret=SecretValue("PRIVATE_KEY"), http_executor=parser_http
    )
    target = normalize_url("https://example.com/PRIVATE_URL")
    expected: dict[str, object] = {}
    if semantic == "success":
        await parser_adapter.fetch(target)
    else:
        with pytest.raises(ExecutionFailure) as caught:
            await parser_adapter.fetch(target)
        expected = dict(caught.value.diagnostics)
    logger, stream = structured_test_logger("tests.doubao.fetch.safe-response")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0, 0, 5), provider_name="doubao", logger=logger
        )
        adapter = DoubaoAdapter(
            name="doubao", secret=SecretValue("PRIVATE_KEY"), http_executor=executor
        )
        if semantic == "success":
            await adapter.fetch(target)
        else:
            with pytest.raises(ExecutionFailure):
                await adapter.fetch(target)
    diagnostics = [
        dict(token.split("=", 1) for token in shlex.split(line)[1:])
        for line in stream.getvalue().splitlines()
        if "event=provider_response_diagnostic " in line
    ]
    assert len(diagnostics) == 1
    fields = diagnostics[0]
    assert fields["stage"] == "fetch"
    assert fields.get("semantic_result") == semantic
    assert fields.get("response_kind") == "object"
    if category is not None:
        assert fields.get("category") == category == expected.get("category")
        assert fields["semantic_result"] == expected.get("semantic_result")
    if upstream_code is not None:
        assert fields.get("upstream_code") == upstream_code
    assert "PRIVATE_" not in stream.getvalue()


@pytest.mark.parametrize(
    "error",
    [
        {"Code": "PRIVATE_UNKNOWN", "Message": "PRIVATE_MESSAGE https://example.com/PRIVATE_URL"},
        {"Code": "99999"},
        {"Code": "10502", "CodeN": 10409},
        {"Code": 10502},
        {"CodeN": "10502"},
        {"Code": "10502", "CodeN": True},
        "PRIVATE_ERROR",
    ],
)
async def test_fetch_does_not_retry_unknown_business_errors(error: object) -> None:
    requests: list[httpx.Request] = []
    logger, stream = structured_test_logger("tests.doubao.fetch.unknown-error")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "ResponseMetadata": {"Error": error},
                "Result": {"Data": {"StatusCode": 200, "ContentText": "PRIVATE_CONTENT"}},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(3, 0, 0, 5), provider_name="doubao", logger=logger
        )
        adapter = DoubaoAdapter(
            name="doubao", secret=SecretValue("PRIVATE_KEY"), http_executor=executor
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await adapter.fetch(normalize_url("https://example.com/PRIVATE_URL"))
    assert len(requests) == 1
    assert caught.value.retryable is False
    diagnostics = [
        dict(token.split("=", 1) for token in shlex.split(line)[1:])
        for line in stream.getvalue().splitlines()
        if "event=provider_response_diagnostic " in line
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["upstream_code"] == "unknown"
    assert diagnostics[0]["category"] == "provider_response"
    assert "PRIVATE_" not in stream.getvalue() + str(caught.value) + str(caught.value.diagnostics)


async def test_fetch_invalid_json_uses_shared_decode_failure() -> None:
    requests: list[httpx.Request] = []
    logger, stream = structured_test_logger("tests.doubao.fetch.decode")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="PRIVATE_INVALID_JSON")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(3, 0, 0, 5), provider_name="doubao", logger=logger
        )
        adapter = DoubaoAdapter(
            name="doubao", secret=SecretValue("PRIVATE_KEY"), http_executor=executor
        )
        with pytest.raises(ProtocolFailure):
            await adapter.fetch(normalize_url("https://example.com/PRIVATE_URL"))
    assert len(requests) == 1
    assert "category=decode" in stream.getvalue()
    assert "PRIVATE_" not in stream.getvalue()


async def test_fetch_propagates_cancellation_without_retry() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(3, 0, 0, 5), provider_name="doubao")
        adapter = DoubaoAdapter(
            name="doubao", secret=SecretValue("synthetic-key"), http_executor=executor
        )
        with pytest.raises(asyncio.CancelledError):
            await adapter.fetch(normalize_url("https://example.com/article"))
    assert len(requests) == 1
