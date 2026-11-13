import json
import logging
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import (
    DiagnosticText,
    KeyValueFormatter,
    SecretRedactor,
    SecretValue,
)
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.web.firecrawl import FirecrawlAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor

_FIXTURES = Path(__file__).parents[2] / "fixtures/providers/firecrawl"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


async def test_firecrawl_adapter_maps_v2_search_and_scrape_payloads() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json"), _fixture("scrape.json")])
    adapter = FirecrawlAdapter(
        name="firecrawl",
        api_url="https://fire.example.test/v2",
        secret=SecretValue("x"),
        http_executor=executor,
    )
    hits = await adapter.search("query")
    assert hits == [
        KeywordSearchHit(
            "https://example.com/fire",
            "Firecrawl result",
            "Firecrawl snippet",
            "<main>Raw HTML</main>",
            "Clean markdown",
        )
    ]
    candidate = await adapter.fetch(normalize_url("https://example.com/fire"))
    assert candidate == URLFetchCandidate("<article>Fetched raw</article>", "Fetched markdown")

    search_request, fetch_request = executor.requests
    assert search_request.url == "https://fire.example.test/v2/search"
    assert fetch_request.url == "https://fire.example.test/v2/scrape"
    assert search_request.headers == {"Authorization": "Bearer x"}
    assert search_request.json_body == {
        "query": "query",
        "sources": ["web"],
        "scrapeOptions": {"formats": ["markdown", "rawHtml"]},
    }
    assert fetch_request.json_body == {
        "url": "https://example.com/fire",
        "formats": ["markdown", "rawHtml"],
    }

    markdown_only = FirecrawlAdapter(
        name="firecrawl",
        api_url="https://fire.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor(
            [{"success": True, "data": {"markdown": "only markdown"}}]
        ),
    )
    assert await markdown_only.fetch(
        normalize_url("https://example.com/fire")
    ) == URLFetchCandidate("only markdown", "only markdown")

    bad = FirecrawlAdapter(
        name="firecrawl",
        api_url="https://fire.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([{"success": False}]),
    )
    with pytest.raises(ExecutionFailure):
        await bad.search("query")


async def test_firecrawl_search_failure_carries_selected_safe_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    payload = {
        "success": False,
        "error": {"code": "quota", "message": "quota exhausted", "body": "PRIVATE_ERROR_BODY"},
        "data": {"web": [{"markdown": "PRIVATE_MARKDOWN", "error": "PRIVATE_CONTENT_ERROR"}]},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        adapter = FirecrawlAdapter(
            name="firecrawl",
            api_url="https://fire.example.test",
            secret=SecretValue("x"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(1, 0, 0, 5), provider_name="firecrawl"
            ),
        )
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.search("query")
    assert caught.value.message == "firecrawl/search: provider reported failure"
    message = caught.value.diagnostics.get("upstream_message")
    assert isinstance(message, DiagnosticText)
    assert message.text == "quota exhausted"
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["semantic_result"] == "error"
    formatter = KeyValueFormatter(SecretRedactor())
    rendered = "\n".join(formatter.format(record) for record in caplog.records)
    assert 'upstream_message="quota exhausted"' in rendered
    assert "upstream_code=quota" in rendered
    assert "PRIVATE_" not in rendered
    assert "event=provider_candidates_summary" not in rendered
