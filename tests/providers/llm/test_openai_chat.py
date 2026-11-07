import asyncio
import json

import httpx
import pytest

from agent_search_gateway.concurrency import CapacityGate
from agent_search_gateway.errors import ExecutionFailure, ProtocolFailure
from agent_search_gateway.models import LLMInvocation, RetryPolicy
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.llm.openai_chat import OpenAIChatCompletionsClient
from tests.support.logging import structured_test_logger


async def _no_sleep(_delay: float) -> None:
    return None


async def test_openai_chat_builds_request_retries_invalid_shape_and_parses_text_and_json() -> None:
    requests: list[httpx.Request] = []
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        requests.append(request)
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, json={"choices": []}, request=request)
        if attempts == 2:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "answer"}}]},
                request=request,
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
            request=request,
        )

    policy = RetryPolicy(3, 0.01, 0.02, 1.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        executor = HttpJsonExecutor(
            http_client,
            policy,
            provider_name="openai-main",
            sleep=_no_sleep,
        )
        client = OpenAIChatCompletionsClient(
            name="openai-main",
            api_url="https://llm.example.test/base",
            secret=SecretValue("x"),
            executor=executor,
            quota=CapacityGate(1),
            retry_policy=policy,
            sleep=_no_sleep,
        )
        invocation = LLMInvocation(
            provider="openai-main",
            model="model-a",
            extra_body={"temperature": 0.2},
        )
        messages = ({"role": "user", "content": "question"},)

        assert await client.complete_text(invocation, messages) == "answer"
        assert await client.complete_json(invocation, messages) == {"ok": True}

    assert attempts == 3
    first = requests[0]
    assert str(first.url) == "https://llm.example.test/base/v1/chat/completions"
    assert first.headers["authorization"] == "Bearer x"
    payload = json.loads(first.content)
    assert payload == {
        "model": "model-a",
        "messages": [{"role": "user", "content": "question"}],
        "temperature": 0.2,
    }


async def test_openai_protocol_debug_events_exclude_messages_responses_and_credentials() -> None:
    attempts = 0
    secret = SecretValue("CREDENTIAL_SENTINEL")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, json={"choices": []}, request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "MODEL_RESPONSE_SENTINEL"}}]},
            request=request,
        )

    policy = RetryPolicy(2, 0.01, 0.02, 1.0)
    logger, stream = structured_test_logger(
        "tests.openai.protocol-events",
        secrets=[secret],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        executor = HttpJsonExecutor(
            http_client,
            policy,
            provider_name="openai-main",
            sleep=_no_sleep,
        )
        client = OpenAIChatCompletionsClient(
            name="openai-main",
            api_url="https://llm.example.test",
            secret=secret,
            executor=executor,
            quota=CapacityGate(1),
            retry_policy=policy,
            sleep=_no_sleep,
            logger=logger,
        )
        result = await client.complete_text(
            LLMInvocation(
                "openai-main",
                "safe-model-name",
                {"temperature": 0.2, "response_format": {"type": "text"}},
            ),
            ({"role": "user", "content": "USER_MESSAGE_SENTINEL"},),
        )

    assert result == "MODEL_RESPONSE_SENTINEL"
    logged = stream.getvalue()
    lines = logged.splitlines()
    started = [line for line in lines if "event=provider_started" in line]
    assert len(started) == 2
    assert "attempt=1" in started[0]
    assert "attempt=2" in started[1]
    assert all("provider=openai-main" in line for line in started)
    assert all("stage=llm" in line for line in started)
    assert all("model=safe-model-name" in line for line in started)
    assert all("message_count=1" in line for line in started)
    assert all("input_chars=" in line for line in started)
    assert all("extra_body_keys=response_format,temperature" in line for line in started)
    assert any(
        "event=provider_failed" in line and "reason=protocol_retry" in line and "attempt=1" in line
        for line in lines
    )
    assert any("event=provider_completed" in line and "output_chars=23" in line for line in lines)
    assert "USER_MESSAGE_SENTINEL" not in logged
    assert "MODEL_RESPONSE_SENTINEL" not in logged
    assert "CREDENTIAL_SENTINEL" not in logged


async def test_openai_chat_rejects_reserved_extra_body_and_respects_quota() -> None:
    policy = RetryPolicy(2, 0.01, 0.02, 1.0)
    quota = CapacityGate(1)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        executor = HttpJsonExecutor(
            http_client,
            policy,
            provider_name="openai-main",
            sleep=_no_sleep,
        )
        client = OpenAIChatCompletionsClient(
            name="openai-main",
            api_url="https://llm.example.test",
            secret=SecretValue("x"),
            executor=executor,
            quota=quota,
            retry_policy=policy,
            sleep=_no_sleep,
        )
        messages = ({"role": "user", "content": "question"},)
        invocation = LLMInvocation("openai-main", "model-a", {})
        first = asyncio.create_task(client.complete_text(invocation, messages))
        await entered.wait()
        second = asyncio.create_task(client.complete_text(invocation, messages))
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        assert tuple(await asyncio.gather(first, second)) == ("ok", "ok")

        with pytest.raises(ExecutionFailure):
            await client.complete_text(
                LLMInvocation("openai-main", "model-a", {"model": "override"}),
                messages,
            )

    assert quota.max_observed_in_use == 1


async def test_openai_chat_exhausts_invalid_response_as_protocol_execution_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []}, request=request)

    policy = RetryPolicy(2, 0.01, 0.02, 1.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        executor = HttpJsonExecutor(
            http_client,
            policy,
            provider_name="openai-main",
            sleep=_no_sleep,
        )
        client = OpenAIChatCompletionsClient(
            name="openai-main",
            api_url="https://llm.example.test",
            secret=SecretValue("x"),
            executor=executor,
            quota=CapacityGate(1),
            retry_policy=policy,
            sleep=_no_sleep,
        )
        with pytest.raises(ProtocolFailure):
            await client.complete_text(
                LLMInvocation("openai-main", "model-a", {}),
                ({"role": "user", "content": "q"},),
            )
