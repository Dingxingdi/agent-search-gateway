import json
from pathlib import Path

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.web.tavily import TavilyAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor

_FIXTURES = Path(__file__).parents[2] / "fixtures/providers/tavily"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


async def test_tavily_adapter_conforms_to_registered_search_and_fetch_contracts() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json"), _fixture("extract.json")])
    adapter = TavilyAdapter(
        name="tavily",
        api_url="https://tavily.example.test/base/",
        secret=SecretValue("x"),
        http_executor=executor,
    )

    hits = await adapter.search("query")
    assert hits == [
        KeywordSearchHit(
            url="https://example.com/a",
            title="Example result",
            snippet="Search summary",
            raw_content="Full raw body",
            content="",
        )
    ]
    candidate = await adapter.fetch(normalize_url("https://example.com/a"))
    assert candidate == URLFetchCandidate("Extracted raw body", "")

    search_request, fetch_request = executor.requests
    assert search_request.url == "https://tavily.example.test/base/search"
    assert search_request.headers == {"Authorization": "Bearer x"}
    assert search_request.json_body == {"query": "query", "include_raw_content": "markdown"}
    assert fetch_request.url == "https://tavily.example.test/base/extract"
    assert fetch_request.json_body == {"urls": ["https://example.com/a"]}
    assert "URLStore" not in str(adapter.search.__annotations__)

    bad = TavilyAdapter(
        name="tavily",
        api_url="https://tavily.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([{"results": []}]),
    )
    with pytest.raises(ExecutionFailure):
        await bad.fetch(normalize_url("https://example.com/a"))


async def test_tavily_fetch_accepts_normalized_equivalent_url_after_malformed_results() -> None:
    adapter = TavilyAdapter(
        name="tavily",
        api_url="https://tavily.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor(
            [
                {
                    "results": [
                        None,
                        {"url": 1, "raw_content": "ignored"},
                        {
                            "url": " https://EXAMPLE.COM/a ",
                            "raw_content": "Extracted body",
                        },
                    ]
                }
            ]
        ),
    )

    candidate = await adapter.fetch(normalize_url("https://example.com/a"))

    assert candidate == URLFetchCandidate("Extracted body", "")


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({}, "invalid_results_envelope"),
        ({"results": {}}, "invalid_results_envelope"),
        ({"results": []}, "no_matching_result"),
        (
            {"results": [{"url": "https://example.com/other", "raw_content": "body"}]},
            "no_matching_result",
        ),
        (
            {"results": [{"url": "https://example.com/a", "raw_content": ""}]},
            "empty_raw_content",
        ),
        (
            {"results": [{"url": "https://example.com/a"}]},
            "empty_raw_content",
        ),
    ],
)
async def test_tavily_fetch_classifies_unusable_extract_responses(
    payload: object,
    reason: str,
) -> None:
    adapter = TavilyAdapter(
        name="tavily",
        api_url="https://tavily.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([payload]),
    )

    with pytest.raises(ExecutionFailure) as caught:
        await adapter.fetch(normalize_url("https://example.com/a"))

    assert caught.value.reason == reason
    assert reason in caught.value.message


@pytest.mark.parametrize(
    ("results", "reason", "semantic"),
    [
        (
            [
                None,
                {"url": 1},
                {"url": "https://example.com/other"},
                {
                    "url": "https://example.com/a?target=PRIVATE_QUERY",
                    "raw_content": "PRIVATE_BODY",
                },
                {"url": "https://example.com/a", "raw_content": "UNPROCESSED_BODY"},
            ],
            None,
            "success",
        ),
        ([], "no_matching_result", "no_match"),
        (
            [{"url": "https://example.com/a?target=PRIVATE_QUERY", "raw_content": ""}],
            "empty_raw_content",
            "empty",
        ),
        ({}, "invalid_results_envelope", "unknown"),
    ],
)
async def test_fetch_reports_safe_response_diagnostics(
    caplog: pytest.LogCaptureFixture, results: object, reason: str | None, semantic: str
) -> None:
    import logging

    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor
    from agent_search_gateway.providers.http import HttpJsonExecutor

    caplog.set_level(logging.DEBUG)
    target = normalize_url("https://example.com/a?target=PRIVATE_QUERY")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"results": results})
        )
    ) as client:
        adapter = TavilyAdapter(
            name="tavily",
            api_url="https://tavily.example.test",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="tavily"),
        )
        if reason is None:
            candidate = await adapter.fetch(target)
            assert candidate.raw_content == "PRIVATE_BODY"
        else:
            with pytest.raises(ExecutionFailure) as caught:
                await adapter.fetch(target)
            assert caught.value.reason == reason
            assert caught.value.message == f"tavily/fetch: {reason}"
            assert caught.value.diagnostics["semantic_result"] == semantic
    records = [record for record in caplog.records if getattr(record, "gateway_event", None)]
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["semantic_result"] == semantic
    if reason is not None:
        formatter = KeyValueFormatter(SecretRedactor([SecretValue("PRIVATE_AUTH")]))
        rendered = "\n".join(formatter.format(record) for record in records)
        assert "PRIVATE_" not in rendered
        return
    summary = [
        vars(record)["gateway_fields"]
        for record in records
        if getattr(record, "gateway_event", None) == "provider_fetch_summary"
    ][0]
    assert summary["collection"] == "results"
    assert summary["processed"] == 4
    assert summary["unprocessed"] == 1
    assert summary["invalid_records"] == 2
    assert summary["unmatched"] == 1
    formatter = KeyValueFormatter(SecretRedactor([SecretValue("PRIVATE_AUTH")]))
    rendered = "\n".join(formatter.format(record) for record in records)
    assert "PRIVATE_" not in rendered
    assert "UNPROCESSED_BODY" not in rendered
