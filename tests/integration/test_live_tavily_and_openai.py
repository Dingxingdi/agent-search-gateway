import os

import httpx
import pytest

from agent_search_gateway.concurrency import CapacityGate
from agent_search_gateway.models import LLMInvocation, RetryPolicy
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.llm.openai_chat import OpenAIChatCompletionsClient
from agent_search_gateway.providers.web.tavily import TavilyAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("WEB_SEARCH_RUN_INTEGRATION") != "1",
    reason="live provider integration tests are opt-in",
)

_TAVILY_ENV = "TAVILY_" + "API_KEY"
_OPENAI_ENV = "OPENAI_" + "API_KEY"
_POLICY = RetryPolicy(3, 0.25, 2.0, 30.0)


async def test_live_tavily_search_response_shape() -> None:
    private_value = os.environ.get(_TAVILY_ENV)
    if not private_value:
        pytest.skip(f"{_TAVILY_ENV} is not set")

    async with httpx.AsyncClient() as client:
        executor = HttpJsonExecutor(
            client,
            _POLICY,
            provider_name="tavily",
        )
        adapter = TavilyAdapter(
            name="tavily",
            api_url="https://api.tavily.com",
            secret=SecretValue(private_value),
            http_executor=executor,
        )
        hits = await adapter.search("OpenAI official website")

    assert isinstance(hits, list)
    for hit in hits:
        assert isinstance(hit.url, str)
        assert isinstance(hit.title, str)
        assert isinstance(hit.snippet, str)


async def test_live_openai_chat_response_shape() -> None:
    private_value = os.environ.get(_OPENAI_ENV)
    if not private_value:
        pytest.skip(f"{_OPENAI_ENV} is not set")

    client = httpx.AsyncClient()
    executor = HttpJsonExecutor(
        client,
        _POLICY,
        provider_name="openai-live",
    )
    llm = OpenAIChatCompletionsClient(
        name="openai-live",
        api_url="https://api.openai.com",
        secret=SecretValue(private_value),
        executor=executor,
        quota=CapacityGate(1),
        retry_policy=_POLICY,
    )
    try:
        text = await llm.complete_text(
            LLMInvocation(
                provider="openai-live",
                model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                extra_body={},
            ),
            ({"role": "user", "content": "Reply with the word OK."},),
        )
    finally:
        await llm.aclose()

    assert isinstance(text, str)
    assert text.strip()
