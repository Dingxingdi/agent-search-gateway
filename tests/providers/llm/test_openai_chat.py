import asyncio
import json
import logging

import httpx
import pytest

from agent_search_gateway.concurrency import CapacityGate
from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.llm.prompts import focus_summary_messages, judge_messages
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation, RetryPolicy
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.http import HttpJsonExecutor, HttpStatusFailure
from agent_search_gateway.providers.llm.openai_chat import OpenAIChatCompletionsClient
from tests.support.logging import structured_test_logger, without_debug_body_previews


async def _no_sleep(_delay: float) -> None:
    return None


async def test_llm_sends_serialized_json_within_configured_body_limit() -> None:
    messages = judge_messages("HEAD" + '中文🙂"\n\\' * 16000 + "TAIL")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    policy = RetryPolicy(1, 0.01, 0.02, 1.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAIChatCompletionsClient(
            name="openai",
            api_url="https://llm.example.test",
            secret=SecretValue("key"),
            executor=HttpJsonExecutor(http_client, policy, provider_name="openai"),
            quota=CapacityGate(1),
            retry_policy=policy,
        )
        await client.complete_text(LLMInvocation("openai", "model", {}), messages)

    (request,) = requests
    sent_bytes = len(request.content)
    assert sent_bytes <= 131072
    payload = json.loads(request.content)
    assert payload["messages"][0] == messages[0]
    user_content = payload["messages"][1]["content"]
    assert user_content.startswith("Candidate content:\nHEAD")
    assert user_content.endswith("TAIL")
    assert "[... truncated ...]" in user_content


def _input_client(
    http_client: httpx.AsyncClient,
    *,
    request_body_bytes: int,
    quota: CapacityGate | None = None,
    logger: logging.Logger | None = None,
) -> OpenAIChatCompletionsClient:
    policy = RetryPolicy(2, 0.01, 0.02, 1.0)
    return OpenAIChatCompletionsClient(
        name="openai",
        api_url="https://llm.example.test",
        secret=SecretValue("key"),
        executor=HttpJsonExecutor(
            http_client, policy, provider_name="openai", logger=logger, sleep=_no_sleep
        ),
        quota=quota if quota is not None else CapacityGate(1),
        retry_policy=policy,
        request_body_bytes=request_body_bytes,
        logger=logger,
        sleep=_no_sleep,
    )


@pytest.mark.parametrize("source", ["system", "focus", "model", "extra", "unencodable"])
async def test_unfit_llm_input_is_rejected_before_quota_and_http(source: str) -> None:
    private = "PRIVATE_INPUT" * 1000
    model = private if source == "model" else "model"
    extra = {"option": private} if source == "extra" else {}
    messages = focus_summary_messages("content", private if source == "focus" else "focus")
    if source == "system":
        messages = ({"role": "system", "content": private}, *messages[1:])
    if source == "unencodable":
        extra = {"option": private + "\ud800"}
    quota = CapacityGate(1)
    logger, stream = structured_test_logger("tests.openai.input-rejected")

    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("rejected input reached the transport")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as http_client:
        client = _input_client(http_client, request_body_bytes=512, quota=quota, logger=logger)
        with pytest.raises(ExecutionFailure) as caught:
            await client.complete_json(LLMInvocation("openai", model, extra), messages)

    assert caught.value.code == ErrorCode.LLM_STAGE_FAILED
    assert caught.value.reason == "input_budget_unfit"
    assert caught.value.__context__ is None
    assert quota.max_observed_in_use == 0
    logged = stream.getvalue()
    assert logged.count("event=input_rejected") == 1
    assert "reason=input_budget_unfit" in logged and "limit_bytes=512" in logged
    if source == "unencodable":
        assert "request_body_original_bytes=" not in logged
    else:
        original = httpx.Request(
            "POST",
            "https://llm.example.test",
            json={"model": model, "messages": [dict(message) for message in messages], **extra},
        ).content
        assert f"request_body_original_bytes={len(original)}" in logged
    assert "PRIVATE_INPUT" not in logged
    assert "http_attempt_started" not in logged and "upstream_body_preview" not in logged


@pytest.mark.parametrize("slack", [0, 1000])
async def test_small_llm_body_is_byte_identical_at_and_below_limit(slack: int) -> None:
    extra = {"response_format": {"type": "json_object"}, "temperature": 0.0}
    messages = judge_messages('中文🙂"\n\\')
    expected = httpx.Request(
        "POST",
        "https://llm.example.test",
        json={"model": "model", "messages": [dict(message) for message in messages], **extra},
    ).content
    bodies: list[bytes] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = _input_client(http_client, request_body_bytes=len(expected) + slack)
        await client.complete_json(LLMInvocation("openai", "model", extra), messages)

    assert bodies == [expected]


async def test_http_and_protocol_retries_reuse_the_same_prepared_llm_body() -> None:
    bodies: list[bytes] = []
    from logging.handlers import BufferingHandler

    logger, stream = structured_test_logger("tests.openai.nested-retries")
    records = BufferingHandler(100)
    logger.addHandler(records)
    extra = {"temperature": 0.25}

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        if len(bodies) == 1:
            extra["temperature"] = 0.5
            return httpx.Response(429)
        content = "invalid JSON" if len(bodies) == 2 else '{"ok":true}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = _input_client(http_client, request_body_bytes=512, logger=logger)
        result = await client.complete_json(
            LLMInvocation("openai", "model", extra), judge_messages("正文" * 2000)
        )

    for event, pairs in {
        "http_attempt_started": [(1, 1), (1, 2), (2, 1)],
        "http_attempt_completed": [(1, 1), (1, 2), (2, 1)],
        "upstream_body_preview": [(1, 1), (1, 2), (2, 1)],
        "http_retrying": [(1, 1)],
    }.items():
        fields = [
            vars(r)["gateway_fields"] for r in records.buffer if vars(r)["gateway_event"] == event
        ]
        assert [(f.get("protocol_attempt"), f["attempt"]) for f in fields] == pairs
        if event == "upstream_body_preview":
            assert fields[0]["response_body_preview"] is None
            assert "invalid JSON" in fields[1]["response_body_preview"]
            assert "ok" in fields[2]["response_body_preview"]
            assert all(f["request_body_preview"] and f["request_input_truncated"] for f in fields)
    assert stream.getvalue().count("event=upstream_body_preview ") == 3
    assert result == {"ok": True}
    first, second, third = bodies
    assert first == second == third and len(first) <= 512
    assert json.loads(third)["temperature"] == 0.25
    assert b"[... truncated ...]" in first


@pytest.mark.parametrize(
    "http_level,protocol_level,late_debug",
    [
        (logging.DEBUG, logging.INFO, False),
        (logging.INFO, logging.DEBUG, False),
        (logging.INFO, logging.DEBUG, True),
    ],
)
async def test_preview_uses_the_transport_logger_for_credentials_crossing_input_cuts(
    http_level: int, protocol_level: int, late_debug: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_search_gateway import observability
    from agent_search_gateway.body_preview import BodyScrubber
    from agent_search_gateway.input_limits import TruncatableMessage

    secret = "PRIVATE_LEFT" + "x" * 6000 + "PRIVATE_DROPPED_MIDDLE" + "x" * 6000 + "PRIVATE_RIGHT"
    http_logger, stream = structured_test_logger(
        "tests.openai.transport-view", secrets=[SecretValue(secret)]
    )
    protocol_logger, protocol_stream = structured_test_logger(
        "tests.openai.protocol-only", secrets=[SecretValue("DIFFERENT_PRIVATE")]
    )
    http_logger.setLevel(http_level)
    protocol_logger.setLevel(protocol_level)
    if http_level == logging.INFO:

        def unexpected(*args: object) -> None:
            pytest.fail("inactive HTTP logger projected an input")

        monkeypatch.setattr(BodyScrubber, "project", unexpected)
    messages = (TruncatableMessage("", "VISIBLE_HEAD" + secret + "VISIBLE_TAIL"),)
    wires: list[bytes] = []

    def respond(request: httpx.Request) -> httpx.Response:
        wires.append(request.content)
        if late_debug:
            http_logger.setLevel(logging.DEBUG)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    policy = RetryPolicy(1, 0, 0, 1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = OpenAIChatCompletionsClient(
            name="openai",
            api_url="https://llm.test",
            secret=observability.SecretValue("sample"),
            executor=HttpJsonExecutor(
                http_client, policy, provider_name="openai", logger=http_logger
            ),
            quota=CapacityGate(1),
            retry_policy=policy,
            request_body_bytes=600,
            logger=protocol_logger,
        )
        assert await client.complete_text(LLMInvocation("openai", "model", {}), messages) == "ok"
    (wire,) = wires
    assert b"PRIVATE_LEFT" in wire and b"PRIVATE_RIGHT" in wire
    assert b"PRIVATE_DROPPED_MIDDLE" not in wire
    logged = stream.getvalue()
    assert "PRIVATE_" not in logged + protocol_stream.getvalue()
    assert "VISIBLE_" not in protocol_stream.getvalue()
    if late_debug:
        assert "request_body_preview=null" in logged and "request_input_truncated=true" in logged
    elif http_level == logging.DEBUG:
        assert "VISIBLE_HEAD" in logged and "VISIBLE_TAIL" in logged
        assert (
            "request_input_truncated=true" in logged and "request_preview_truncated=false" in logged
        )
    else:
        assert "upstream_body_preview" not in logged


@pytest.mark.parametrize(
    "size,cap,input_cut,preview_cut",
    [
        (20, 1000, False, False),
        (20000, 30000, False, True),
        (20000, 1000, True, False),
        (20000, 12000, True, True),
    ],
)
async def test_llm_preview_input_and_log_limits_follow_actual_sent_bytes(
    size: int, cap: int, input_cut: bool, preview_cut: bool
) -> None:
    from agent_search_gateway.input_limits import TruncatableMessage

    messages = (TruncatableMessage("", "HEAD" + "x" * size + "TAIL"),)
    expected = httpx.Request(
        "POST",
        "https://llm.test",
        json={"model": "model", "messages": [dict(msg) for msg in messages]},
    ).content
    wires: list[bytes] = []
    logger, stream = structured_test_logger("tests.openai.preview-limits")

    def respond(request: httpx.Request) -> httpx.Response:
        wires.append(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = _input_client(transport, request_body_bytes=cap, logger=logger)
        assert await client.complete_text(LLMInvocation("openai", "model", {}), messages) == "ok"
    (wire,) = wires
    (line,) = [s for s in stream.getvalue().splitlines() if "event=upstream_body_preview " in s]
    assert f"request_body_original_bytes={len(expected)} " in line
    assert f"request_body_sent_bytes={len(wire)} " in line
    assert f"request_input_truncated={str(input_cut).lower()} " in line
    assert f"request_preview_truncated={str(preview_cut).lower()} " in line
    assert "HEAD" in line and "TAIL" in line


async def test_concurrent_protocol_attempts_are_local_to_each_completion() -> None:
    from agent_search_gateway.request_ids import bind_request_id

    first_arrived = asyncio.Event()
    second_protocol_round = asyncio.Event()
    counts: dict[str, int] = {}
    logger, stream = structured_test_logger("tests.openai.concurrent")

    async def respond(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        counts[model] = counts.get(model, 0) + 1
        if model == "text":
            first_arrived.set()
            await second_protocol_round.wait()
            content = "text answer"
        elif counts[model] == 1:
            await first_arrived.wait()
            content = "invalid"
        else:
            second_protocol_round.set()
            content = '{"ok":true}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = _input_client(
            transport, request_body_bytes=512, logger=logger, quota=CapacityGate(2)
        )
        with bind_request_id("aaaaaaaa"):
            text_task = asyncio.create_task(
                client.complete_text(LLMInvocation("openai", "text", {}), ())
            )
        with bind_request_id("bbbbbbbb"):
            json_task = asyncio.create_task(
                client.complete_json(LLMInvocation("openai", "json", {}), ())
            )
        text_result, json_result = await asyncio.wait_for(asyncio.gather(text_task, json_task), 2)
        assert text_result == "text answer" and json_result == {"ok": True}
    previews = [s for s in stream.getvalue().splitlines() if "event=upstream_body_preview " in s]
    assert len(previews) == 3
    for request_id, rounds in [("aaaaaaaa", [1]), ("bbbbbbbb", [1, 2])]:
        lines = [s for s in previews if f"request={request_id} " in s]
        assert len(lines) == len(rounds)
        assert all(
            f"protocol_attempt={n} " in s and "attempt=1 " in s
            for n, s in zip(rounds, lines, strict=True)
        )


async def test_server_context_length_rejection_is_terminal_within_byte_limit() -> None:
    bodies: list[bytes] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(400, json={"error": {"code": "context_length_exceeded"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = _input_client(http_client, request_body_bytes=512)
        with pytest.raises(HttpStatusFailure) as caught:
            await client.complete_text(
                LLMInvocation("openai", "model", {}), judge_messages("short")
            )

    assert caught.value.code == ErrorCode.ALL_PROVIDERS_FAILED
    assert caught.value.status_code == 400
    assert len(bodies) == 1 and len(bodies[0]) < 512


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
    assert "PROMPT_SENTINEL" not in without_debug_body_previews(logged)
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
        assert sentinel not in without_debug_body_previews(logged)


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
        assert sentinel not in without_debug_body_previews(logged)
