import json
import logging
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.web.brave import BraveAdapter
from tests.support.http import RecordingJsonExecutor

_FIXTURE = Path(__file__).parents[2] / "fixtures/providers/brave/search.json"


async def test_brave_adapter_maps_web_results_as_search_only_provider() -> None:
    executor = RecordingJsonExecutor([json.loads(_FIXTURE.read_text(encoding="utf-8"))])
    adapter = BraveAdapter(
        name="brave",
        api_url="https://brave.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
    )
    assert await adapter.search("hello world") == [
        KeywordSearchHit("https://example.com/brave", "Brave result", "Brave snippet")
    ]
    request = executor.requests[0]
    assert request.method == "GET"
    assert request.url == "https://brave.example.test/res/v1/web/search?q=hello+world&count=10"
    assert request.headers == {"X-Subscription-Token": "x"}
    assert request.json_body is None
    assert not hasattr(adapter, "fetch")

    empty = BraveAdapter(
        name="brave",
        api_url="https://brave.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([{}]),
    )
    assert await empty.search("query") == []

    malformed = BraveAdapter(
        name="brave",
        api_url="https://brave.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor(
            [{"web": {"results": [{"url": 1, "title": "x", "description": "y"}]}}]
        ),
    )
    assert await malformed.search("query") == []


@pytest.mark.parametrize(
    "payload", [{}, {"web": None}, {"web": {}}, {"error": {"message": "PRIVATE_UNVERIFIED"}}]
)
async def test_brave_empty_branches_report_zero_candidates_without_invented_errors(
    payload: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        adapter = BraveAdapter(
            name="brave",
            api_url="https://brave.example.test",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="brave"),
        )
        assert await adapter.search("query") == []
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["received"] == summaries[0]["accepted"] == summaries[0]["rejected"] == 0
    diagnostic = next(
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    )
    assert "upstream_message" not in diagnostic
    assert "upstream_code" not in diagnostic
