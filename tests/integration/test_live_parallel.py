import os

import httpx
import pytest

from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.web.parallel import ParallelAdapter
from agent_search_gateway.url_normalization import normalize_url

pytestmark = pytest.mark.skipif(
    os.environ.get("WEB_SEARCH_RUN_INTEGRATION") != "1",
    reason="live provider integration tests are opt-in",
)

_PARALLEL_ENV = "PARALLEL_" + "API_KEY"
_POLICY = RetryPolicy(3, 0.25, 2.0, 30.0)


async def test_live_parallel_search_response_shape() -> None:
    private_value = os.environ.get(_PARALLEL_ENV)
    if not private_value:
        pytest.skip(f"{_PARALLEL_ENV} is not set")

    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=None)) as client:
        executor = HttpJsonExecutor(
            client,
            _POLICY,
            provider_name="parallel",
        )
        adapter = ParallelAdapter(
            name="parallel",
            api_url="https://api.parallel.ai",
            secret=SecretValue(private_value),
            http_executor=executor,
        )
        hits = await adapter.search("OpenAI official website")

    assert isinstance(hits, list)
    assert hits
    for hit in hits:
        assert isinstance(hit.url, str)
        assert isinstance(hit.title, str)
        assert isinstance(hit.snippet, str)


async def test_live_parallel_extract_response_shape() -> None:
    private_value = os.environ.get(_PARALLEL_ENV)
    if not private_value:
        pytest.skip(f"{_PARALLEL_ENV} is not set")

    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=None)) as client:
        executor = HttpJsonExecutor(
            client,
            _POLICY,
            provider_name="parallel",
        )
        adapter = ParallelAdapter(
            name="parallel",
            api_url="https://api.parallel.ai",
            secret=SecretValue(private_value),
            http_executor=executor,
        )
        candidate = await adapter.fetch(normalize_url("https://example.com/"))

    assert isinstance(candidate.raw_content, str)
    assert candidate.raw_content.strip()
    assert candidate.content == candidate.raw_content
