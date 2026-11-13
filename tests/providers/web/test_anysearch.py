import json
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.web.anysearch import AnySearchAdapter
from tests.support.http import RecordingJsonExecutor
from tests.support.logging import structured_test_logger

_FIXTURE = Path(__file__).parents[2] / "fixtures/providers/anysearch/search.json"


async def test_anysearch_adapter_maps_unified_search_json_as_search_only_provider() -> None:
    executor = RecordingJsonExecutor([json.loads(_FIXTURE.read_text(encoding="utf-8"))])
    adapter = AnySearchAdapter(
        name="anysearch",
        api_url="https://any.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
    )
    assert await adapter.search("query") == [
        KeywordSearchHit("https://example.com/any", "AnySearch result", "AnySearch snippet")
    ]
    request = executor.requests[0]
    assert request.url == "https://any.example.test/v1/search"
    assert request.headers == {"Authorization": "Bearer x"}
    assert request.json_body == {"query": "query", "format": "json", "max_results": 10}
    assert not hasattr(adapter, "fetch")

    malformed = AnySearchAdapter(
        name="anysearch",
        api_url="https://any.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([{"code": 0, "data": {"results": "bad"}}]),
    )
    with pytest.raises(ExecutionFailure):
        await malformed.search("query")

    failed = AnySearchAdapter(
        name="anysearch",
        api_url="https://any.example.test",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([{"code": -1, "message": "failed"}]),
    )
    with pytest.raises(ExecutionFailure):
        await failed.search("query")


@pytest.mark.parametrize("code", [-1, True, False, "0", None])
async def test_anysearch_diagnoses_failure_without_changing_code_validation(code: object) -> None:
    logger, stream = structured_test_logger("test.anysearch.failure")
    payload = {
        "code": code,
        "message": "failed",
        "data": {"results": []},
        "body": {"message": "PRIVATE_BODY"},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0, 0, 5), provider_name="anysearch", logger=logger
        )
        adapter = AnySearchAdapter(
            name="anysearch",
            api_url="https://any.example.test",
            secret=SecretValue("x"),
            http_executor=executor,
        )
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.search("query")
    assert caught.value.message == "anysearch/search: provider reported failure"
    rendered = stream.getvalue()
    assert rendered.count("event=provider_response_diagnostic") == 1
    if code == -1:
        assert "upstream_code=-1" in rendered
        assert "upstream_message=failed" in rendered
        assert "upstream_code" in caught.value.diagnostics
        assert "semantic_result=error" in rendered
    else:
        assert "category=invalid_envelope" in rendered
        assert "upstream_code" not in rendered
        assert "upstream_message" not in rendered
    assert "PRIVATE_" not in rendered
