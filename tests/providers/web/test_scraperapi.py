import logging
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import DiagnosticText, SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.web.common import HttpRequester
from agent_search_gateway.providers.web.scraperapi import ScraperAPIAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingHttpExecutor
from tests.support.logging import structured_test_logger


def _adapter(executor: HttpRequester) -> ScraperAPIAdapter:
    return ScraperAPIAdapter(
        name="scraperapi",
        api_url="https://api.scraperapi.test",
        secret=SecretValue("secret"),
        http_executor=executor,
    )


async def _search_payload(payload: object) -> list[KeywordSearchHit]:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="scraperapi")
        return await _adapter(executor).search("query")


def _events(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, object]]:
    return [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == event
    ]


async def test_scraperapi_reports_partial_candidate_rejections(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    valid = [
        {"link": f"https://example.com/{index}", "title": str(index), "snippet": "text"}
        for index in range(5)
    ]
    payload = {
        "organic_results": valid + [{}, {"title": "missing"}, {"snippet": "missing"}, None, 1]
    }
    hits = await _search_payload(payload)

    assert hits == [
        KeywordSearchHit(f"https://example.com/{index}", str(index), "text") for index in range(5)
    ]
    summaries = _events(caplog, "provider_candidates_summary")
    assert len(summaries) == 1
    assert summaries[0] == {
        "provider": "scraperapi",
        "stage": "search",
        "phase": "adapter",
        "received": 10,
        "accepted": 5,
        "rejected": 5,
        "rejection_counts": {"missing_url": 3, "invalid_record": 2},
        "rewritten": 0,
        "processed": 10,
        "unprocessed": 0,
    }


async def test_scraperapi_reports_empty_candidate_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    assert await _search_payload({"organic_results": []}) == []
    summaries = _events(caplog, "provider_candidates_summary")
    assert len(summaries) == 1
    assert summaries[0]["received"] == 0
    assert summaries[0]["accepted"] == 0
    assert summaries[0]["rejected"] == 0
    assert summaries[0]["rejection_counts"] == {}


async def test_scraperapi_reports_all_invalid_candidates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    payload = {"organic_results": [None, {}, {"link": ""}, {"link": "  "}, {"link": 1}]}
    assert await _search_payload(payload) == []
    summaries = _events(caplog, "provider_candidates_summary")
    assert len(summaries) == 1
    assert summaries[0]["received"] == 5
    assert summaries[0]["accepted"] == 0
    assert summaries[0]["rejected"] == 5
    assert summaries[0]["rejection_counts"] == {"missing_url": 3, "invalid_record": 2}


async def test_scraperapi_counts_bad_presentation_as_invalid_records(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    payload = {
        "organic_results": [
            {"link": "https://example.com/bad-title", "title": []},
            {"link": "https://example.com/bad-snippet", "snippet": {}},
            {"link": "https://example.com/good"},
        ]
    }
    assert await _search_payload(payload) == [KeywordSearchHit("https://example.com/good", "", "")]
    summaries = _events(caplog, "provider_candidates_summary")
    assert len(summaries) == 1
    assert summaries[0]["received"] == 3
    assert summaries[0]["accepted"] == 1
    assert summaries[0]["rejected"] == 2
    assert summaries[0]["rejection_counts"] == {"invalid_record": 2}


async def test_scraperapi_reports_root_error_with_original_public_failure() -> None:
    logger, stream = structured_test_logger("test.scraperapi.top_level_error")
    payload = {
        "error": "Vendor quota exhausted",
        "organic_results": [{"message": "PRIVATE_CANDIDATE_BODY"}],
        "body": {"message": "PRIVATE_RESPONSE_BODY"},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0, 0, 5), provider_name="scraperapi", logger=logger
        )
        with pytest.raises(ExecutionFailure) as caught:
            await _adapter(executor).search("query")

    assert caught.value.message == "scraperapi/search: provider reported failure"
    rendered = stream.getvalue()
    assert rendered.count("event=provider_response_diagnostic") == 1
    assert "upstream_message=" in rendered
    assert "Vendor quota exhausted" in rendered
    assert "semantic_result=error" in rendered
    assert "PRIVATE_" not in rendered
    assert "upstream_message" in caught.value.diagnostics


async def test_scraperapi_classifies_invalid_root_envelope(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    ) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="scraperapi")
        with pytest.raises(ExecutionFailure, match="response must be an object"):
            await _adapter(executor).search("query")

    diagnostics = _events(caplog, "provider_response_diagnostic")
    assert len(diagnostics) == 1
    assert diagnostics[0]["category"] == "invalid_envelope"
    assert diagnostics[0]["semantic_result"] == "unknown"
    assert _events(caplog, "provider_candidates_summary") == []


async def test_scraperapi_classifies_invalid_results_envelope(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"organic_results": {"message": "PRIVATE_RESULT_BODY"}}
            )
        )
    ) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="scraperapi")
        with pytest.raises(ExecutionFailure, match="organic_results must be an array"):
            await _adapter(executor).search("query")

    diagnostics = _events(caplog, "provider_response_diagnostic")
    assert len(diagnostics) == 1
    assert diagnostics[0].get("category") == "invalid_envelope"
    assert "upstream_message" not in diagnostics[0]
    assert _events(caplog, "provider_candidates_summary") == []


async def test_scraperapi_classifies_success_without_reading_candidate_error_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    payload = {
        "error": None,
        "organic_results": [
            {
                "link": "https://example.com/a",
                "error": "PRIVATE_CANDIDATE_ERROR",
                "message": "PRIVATE_CANDIDATE_MESSAGE",
            }
        ],
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="scraperapi")
        assert await _adapter(executor).search("query") == [
            KeywordSearchHit("https://example.com/a", "", "")
        ]
    diagnostics = _events(caplog, "provider_response_diagnostic")
    assert len(diagnostics) == 1
    assert diagnostics[0].get("semantic_result") == "success"
    assert diagnostics[0].get("response_kind") == "object"
    assert "upstream_message" not in diagnostics[0]


async def test_scraperapi_classifies_invalid_json_without_logging_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="PRIVATE_INVALID_JSON")
        )
    ) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="scraperapi")
        with pytest.raises(ExecutionFailure, match="response was not valid JSON"):
            await _adapter(executor).search("query")
    diagnostics = _events(caplog, "provider_response_diagnostic")
    assert len(diagnostics) == 1
    assert diagnostics[0].get("category") == "decode"
    assert "upstream_message" not in diagnostics[0]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            {"code": 429, "message": "Quota exhausted", "body": "PRIVATE_BODY"},
            {"upstream_code": "429", "upstream_message": "Quota exhausted"},
        ),
        ({"code": {}, "message": {"message": "PRIVATE_NESTED"}}, {}),
        (["PRIVATE_ERROR_ARRAY"], {}),
        ({}, {}),
        ("", {"upstream_message": ""}),
        (False, {}),
        (0, {}),
    ],
)
async def test_scraperapi_selects_only_known_error_children_and_preserves_failure_condition(
    error: object,
    expected: dict[str, str],
) -> None:
    logger, stream = structured_test_logger("test.scraperapi.error_children")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"error": error, "organic_results": []})
        )
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0, 0, 5), provider_name="scraperapi", logger=logger
        )
        with pytest.raises(ExecutionFailure) as caught:
            await _adapter(executor).search("query")
    assert caught.value.message == "scraperapi/search: provider reported failure"
    assert set(caught.value.diagnostics) == set(expected)
    for name, value in expected.items():
        diagnostic = caught.value.diagnostics[name]
        assert isinstance(diagnostic, DiagnosticText)
        assert diagnostic.text == value
    assert "PRIVATE_" not in stream.getvalue()
    assert stream.getvalue().count("event=provider_response_diagnostic") == 1


async def test_scraperapi_diagnostic_selection_failure_keeps_original_business_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(error: object) -> dict[str, object]:
        raise ValueError("PRIVATE_DIAGNOSTIC_FAILURE")

    monkeypatch.setattr("agent_search_gateway.providers.web.common.selected_error_fields", broken)
    with pytest.raises(ExecutionFailure) as caught:
        await _search_payload({"error": "Vendor failure", "organic_results": []})

    assert caught.value.message == "scraperapi/search: provider reported failure"
    assert caught.value.diagnostics == {}


async def test_scraperapi_search_uses_synchronous_structured_google_endpoint() -> None:
    executor = RecordingHttpExecutor(
        json_responses=[
            {
                "organic_results": [
                    {"link": "https://example.com/a", "title": "A", "snippet": "Snippet A"}
                ]
            }
        ]
    )
    adapter = ScraperAPIAdapter(
        name="scraperapi",
        api_url="https://api.scraperapi.test/",
        secret=SecretValue("secret"),
        http_executor=executor,
    )

    assert await adapter.search("hello world & docs") == [
        KeywordSearchHit("https://example.com/a", "A", "Snippet A")
    ]
    request = executor.requests[0]
    parsed = urlparse(request.url)
    assert request.method == "GET"
    assert request.stage == "search"
    assert (
        parsed._replace(query="").geturl() == "https://api.scraperapi.test/structured/google/search"
    )
    assert parse_qs(parsed.query) == {"api_key": ["secret"], "query": ["hello world & docs"]}


async def test_scraperapi_fetch_uses_synchronous_scrape_endpoint() -> None:
    executor = RecordingHttpExecutor(text_responses=["<html>page</html>"])
    adapter = ScraperAPIAdapter(
        name="scraperapi",
        api_url="https://api.scraperapi.test/",
        secret=SecretValue("secret"),
        http_executor=executor,
    )
    target = normalize_url("https://example.com/path?a=1&b=2")

    assert await adapter.fetch(target) == URLFetchCandidate(
        "<html>page</html>", "<html>page</html>"
    )
    request = executor.requests[0]
    parsed = urlparse(request.url)
    assert request.method == "GET"
    assert request.stage == "fetch"
    assert parsed._replace(query="").geturl() == "https://api.scraperapi.test"
    assert parse_qs(parsed.query) == {"api_key": ["secret"], "url": [str(target)]}


@pytest.mark.parametrize("body", ["", " \n"])
async def test_scraperapi_fetch_rejects_empty_body(body: str) -> None:
    adapter = ScraperAPIAdapter(
        name="scraperapi",
        api_url="https://api.scraperapi.test",
        secret=SecretValue("secret"),
        http_executor=RecordingHttpExecutor(text_responses=[body]),
    )
    with pytest.raises(ExecutionFailure, match="page body is empty"):
        await adapter.fetch(normalize_url("https://example.com"))


@pytest.mark.parametrize("api_url", ["", "   ", 1])
def test_scraperapi_requires_non_empty_api_url(api_url: object) -> None:
    with pytest.raises(TypeError):
        ScraperAPIAdapter(
            name="scraperapi",
            api_url=api_url,  # type: ignore[arg-type]
            secret=SecretValue("secret"),
            http_executor=RecordingHttpExecutor(),
        )


@pytest.mark.parametrize(
    ("body", "status", "semantic"),
    [
        ("# PRIVATE_BODY\n<html>PRIVATE_HTML</html>", 200, "success"),
        (" \n\t", 200, "empty"),
        ("<html>PRIVATE_HTTP_ERROR</html>", 403, "error"),
    ],
)
async def test_fetch_reports_safe_response_diagnostics(
    caplog: pytest.LogCaptureFixture, body: str, status: int, semantic: str
) -> None:
    from tests.support.web_diagnostics import assert_text_fetch_diagnostics

    await assert_text_fetch_diagnostics(
        caplog,
        "scraperapi",
        lambda executor: ScraperAPIAdapter(
            name="scraperapi",
            api_url="https://scraperapi.example.test",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=executor,
        ),
        body,
        status,
        semantic,
    )
