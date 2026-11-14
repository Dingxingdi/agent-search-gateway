import asyncio
import json

import httpx
import pytest

from agent_search_gateway.concurrency import CapacityGate
from agent_search_gateway.errors import ExecutionFailure, ProtocolFailure
from agent_search_gateway.llm.stages import LLMStages
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
        if attempts == 3:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "invalid JSON"}}]},
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

    assert attempts == 4
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
    started = [line for line in lines if "event=llm_protocol_attempt_started" in line]
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
        "event=provider_retrying" in line
        and "reason=protocol_retry" in line
        and "attempt=1" in line
        for line in lines
    )
    assert any(
        "event=llm_protocol_completed" in line and "output_chars=23" in line for line in lines
    )
    assert "event=provider_failed" not in logged and "event=provider_completed" not in logged
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


async def test_openai_root_api_error_diagnostics_survive_protocol_failure() -> None:
    secret = SecretValue("API_SECRET")
    logger, stream = structured_test_logger("tests.openai.api-error", secrets=[secret])
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200,
            json={
                "error": {
                    "code": "quota_exceeded",
                    "type": "insufficient_quota",
                    "message": "Quota exhausted API_SECRET https://api.example.test/error?token=hidden",
                }
            },
            request=request,
        )

    policy = RetryPolicy(2, 0.01, 0.02, 1.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAIChatCompletionsClient(
            name="openai",
            api_url="https://llm.example.test",
            secret=secret,
            executor=HttpJsonExecutor(http_client, policy, provider_name="openai", logger=logger),
            quota=CapacityGate(1),
            retry_policy=policy,
            sleep=_no_sleep,
            logger=logger,
        )
        with pytest.raises(ProtocolFailure) as caught:
            await client.complete_text(
                LLMInvocation("openai", "model", {}),
                ({"role": "user", "content": "PROMPT_SENTINEL"},),
            )

    assert attempts == 2
    assert str(caught.value) == "openai/llm: response choices are missing"
    assert caught.value.diagnostics.get("upstream_code") is not None
    logged = stream.getvalue()
    diagnostics = [
        line for line in logged.splitlines() if "event=provider_response_diagnostic" in line
    ]
    assert len(diagnostics) == 2
    assert all("http_status=200" in line for line in diagnostics)
    assert all("upstream_code=quota_exceeded" in line for line in diagnostics)
    assert all("upstream_type=insufficient_quota" in line for line in diagnostics)
    assert all("upstream_status=" not in line for line in diagnostics)
    assert all("category=provider_response" in line for line in diagnostics)
    assert "Quota exhausted" in logged
    assert "API_SECRET" not in logged and "token=hidden" not in logged
    assert "PROMPT_SENTINEL" not in logged
    assert "event=provider_failed" not in logged and "event=provider_completed" not in logged


@pytest.mark.parametrize(
    ("stage", "content", "attempts", "category", "diagnostic_category"),
    [
        ("judge", "ANSWER_SENTINEL invalid JSON", 2, "protocol", "invalid_json"),
        ("judge", '["ANSWER_SENTINEL"]', 2, "protocol", "invalid_envelope"),
        ("judge", '{"ok": "yes", "reason": "ANSWER_SENTINEL"}', 1, "execution_failure", None),
        ("safety", '{"ok": true, "reason": ["ANSWER_SENTINEL"]}', 1, "execution_failure", None),
        ("content_clean", "   ", 2, "protocol", None),
        ("focus_summary", "", 2, "protocol", None),
    ],
)
async def test_openai_stage_failures_keep_safe_stage_and_category(
    stage: str, content: str, attempts: int, category: str, diagnostic_category: str | None
) -> None:
    logger, stream = structured_test_logger("tests.openai.stage-failures")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}, request=request
        )

    invocation = LLMInvocation("openai", "model", {})
    policy = RetryPolicy(2, 0.01, 0.02, 1.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAIChatCompletionsClient(
            name="openai",
            api_url="https://llm.example.test",
            secret=SecretValue("secret"),
            executor=HttpJsonExecutor(http_client, policy, provider_name="openai", logger=logger),
            quota=CapacityGate(1),
            retry_policy=policy,
            sleep=_no_sleep,
            logger=logger,
        )
        stages = LLMStages(
            {"openai": client},
            judge=invocation,
            safety=invocation,
            content_clean=invocation,
            focus_summary=invocation,
            logger=logger,
        )
        with pytest.raises(ExecutionFailure) as caught:
            if stage == "focus_summary":
                await stages.focus_summary("PROMPT_SENTINEL", "FOCUS_SENTINEL")
            else:
                await getattr(stages, stage)("PROMPT_SENTINEL")

    assert calls == attempts
    logged = stream.getvalue()
    failed = [line for line in logged.splitlines() if "event=llm_stage_failed" in line]
    assert len(failed) == 1
    assert f"stage={stage}" in failed[0] and f"category={category}" in failed[0]
    assert "provider=openai" in failed[0] and "model=model" in failed[0]
    retries = [line for line in logged.splitlines() if "event=provider_retrying" in line]
    assert len(retries) == attempts - 1
    if diagnostic_category is not None:
        assert caught.value.diagnostics == {
            "category": diagnostic_category,
            "semantic_result": "unknown",
            "response_kind": "object",
        }
        assert all(
            "semantic_result=unknown" in line and "response_kind=object" in line
            for line in [*retries, *failed]
        )
    assert "event=provider_completed" not in logged and "event=provider_failed" not in logged
    for sentinel in ("ANSWER_SENTINEL", "PROMPT_SENTINEL", "FOCUS_SENTINEL"):
        assert sentinel not in logged


async def test_openai_model_content_error_keys_are_never_api_diagnostics() -> None:
    logger, stream = structured_test_logger("tests.openai.model-content")
    content = {
        "ok": True,
        "error": {"code": "MODEL_CODE_SENTINEL", "message": "MODEL_ERROR_SENTINEL"},
        "message": "MODEL_MESSAGE_SENTINEL",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(content)}}]}, request=request
        )

    policy = RetryPolicy(2, 0.01, 0.02, 1.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAIChatCompletionsClient(
            name="openai",
            api_url="https://llm.example.test",
            secret=SecretValue("secret"),
            executor=HttpJsonExecutor(http_client, policy, provider_name="openai", logger=logger),
            quota=CapacityGate(1),
            retry_policy=policy,
            sleep=_no_sleep,
            logger=logger,
        )
        assert (
            await client.complete_json(
                LLMInvocation("openai", "model", {}),
                ({"role": "user", "content": "PROMPT_SENTINEL"},),
            )
            == content
        )

    logged = stream.getvalue()
    diagnostic = [
        line for line in logged.splitlines() if "event=provider_response_diagnostic" in line
    ]
    assert len(diagnostic) == 1 and "semantic_result=success" in diagnostic[0]
    assert "upstream_code=" not in logged and "upstream_message=" not in logged
    assert "event=provider_retrying" not in logged
    for sentinel in (
        "MODEL_CODE_SENTINEL",
        "MODEL_ERROR_SENTINEL",
        "MODEL_MESSAGE_SENTINEL",
        "PROMPT_SENTINEL",
    ):
        assert sentinel not in logged
