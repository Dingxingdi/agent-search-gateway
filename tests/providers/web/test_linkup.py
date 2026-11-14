import json
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.web.linkup import LinkupAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor
from tests.support.logging import structured_test_logger

_FIXTURES = Path(__file__).parents[2] / "fixtures/providers/linkup"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


async def test_linkup_adapter_maps_search_results_and_fetch_markdown() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json"), _fixture("fetch.json")])
    adapter = LinkupAdapter(
        name="linkup",
        api_url="https://linkup.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
    )
    assert await adapter.search("query") == [
        KeywordSearchHit(
            "https://example.com/linkup",
            "Linkup result",
            "Linkup search snippet",
        )
    ]
    assert await adapter.fetch(normalize_url("https://example.com/linkup")) == URLFetchCandidate(
        "<article>Linkup raw</article>", "Linkup markdown"
    )

    search_request, fetch_request = executor.requests
    assert search_request.url == "https://linkup.example.test/v1/search"
    assert fetch_request.url == "https://linkup.example.test/v1/fetch"
    assert search_request.headers == {"Authorization": "Bearer x"}
    assert search_request.json_body == {
        "q": "query",
        "depth": "standard",
        "outputType": "searchResults",
    }
    assert fetch_request.json_body == {
        "url": "https://example.com/linkup",
        "includeRawContent": True,
        "extractImages": False,
    }

    bad = LinkupAdapter(
        name="linkup",
        api_url="https://linkup.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([{"markdown": "", "rawContent": ""}]),
    )
    with pytest.raises(ExecutionFailure):
        await bad.fetch(normalize_url("https://example.com/linkup"))


async def test_linkup_counts_malformed_name_and_content_without_logging_them(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    logger, stream = structured_test_logger("test.linkup.presentation")
    payload = {
        "results": [
            {"url": "https://example.com/bad-name", "name": [], "content": "PRIVATE_BAD_NAME"},
            {"url": "https://example.com/bad-content", "name": "PRIVATE_NAME", "content": {}},
            {"url": "https://example.com/empty-name", "name": " ", "content": "PRIVATE_CONTENT"},
            {"url": "https://example.com/empty-content", "name": "PRIVATE_NAME", "content": ""},
            {
                "url": "https://example.com/good",
                "name": "Good",
                "content": "PRIVATE_SNIPPET",
                "error": "PRIVATE_CANDIDATE_ERROR",
            },
        ],
        "message": "PRIVATE_ROOT_MESSAGE",
        "error": {"message": "PRIVATE_UNVERIFIED_ERROR"},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        adapter = LinkupAdapter(
            name="linkup",
            api_url="https://linkup.example.test",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(1, 0, 0, 5), provider_name="linkup", logger=logger
            ),
        )
        hits = await adapter.search("query")
    assert hits == [KeywordSearchHit("https://example.com/good", "Good", "PRIVATE_SNIPPET")]
    rendered = stream.getvalue()
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["received"] == 5
    assert summaries[0]["accepted"] == 1
    assert summaries[0]["rejected"] == 4
    assert summaries[0]["rejection_counts"] == {"invalid_record": 4}
    assert "PRIVATE_" not in rendered
    assert "upstream_message" not in rendered
    assert "semantic_result=success" in rendered


@pytest.mark.parametrize(
    ("payload", "message", "semantic"),
    [
        (
            {
                "error": {
                    "code": "blocked",
                    "message": "target blocked",
                    "body": "PRIVATE_ERROR_BODY",
                }
            },
            "page body is empty",
            "error",
        ),
        (
            {
                "markdown": "PRIVATE_BODY",
                "rawContent": "PRIVATE_RAW",
                "error": "PRIVATE_CONTENT_ERROR",
            },
            None,
            "success",
        ),
    ],
)
async def test_fetch_reports_safe_response_diagnostics(
    caplog: pytest.LogCaptureFixture, payload: object, message: str | None, semantic: str
) -> None:
    from tests.support.web_diagnostics import assert_json_fetch_diagnostics

    await assert_json_fetch_diagnostics(
        caplog,
        "linkup",
        lambda executor: LinkupAdapter(
            name="linkup",
            api_url="https://linkup.example.test",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=executor,
        ),
        payload,
        message,
        semantic,
        check_provider_response_category=True,
    )
