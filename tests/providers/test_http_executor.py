import io
import logging

import httpx
import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor, SecretValue
from agent_search_gateway.providers.http import HttpJsonExecutor
from tests.support.logging import structured_test_logger


async def _no_sleep(_delay: float) -> None:
    return None


async def test_http_executor_retries_retryable_status_and_hides_sensitive_payloads() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(500, text="SENSITIVE_BODY", request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    stream = io.StringIO()
    logger = logging.getLogger("test.http.executor")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    handler_stream = logging.StreamHandler(stream)
    handler_stream.setFormatter(
        KeyValueFormatter(SecretRedactor([SecretValue("credential-value")]))
    )
    logger.addHandler(handler_stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        result = await executor.request_json(
            "POST",
            "https://endpoint-user:ENDPOINT_PASSWORD_SENTINEL@provider.example.test/search?q=QUERY_PARAMETER_SENTINEL#fragment",
            stage="search",
            headers={"Authorization": "Bearer credential-value"},
            json_body={"query": "SENSITIVE_BODY"},
        )

    assert result == {"ok": True}
    assert attempts == 3
    logged = stream.getvalue()
    lines = logged.splitlines()
    assert sum("event=http_attempt_started" in line for line in lines) == 3
    assert sum("event=http_attempt_completed" in line for line in lines) == 3
    retry_lines = [line for line in lines if "event=http_retrying" in line]
    assert len(retry_lines) == 2
    assert all(line.startswith("WARNING ") for line in retry_lines)
    assert "attempt=1" in retry_lines[0] and "delay_ms=10" in retry_lines[0]
    assert "attempt=2" in retry_lines[1] and "delay_ms=20" in retry_lines[1]
    assert sum("status=500" in line for line in lines) >= 2
    assert any("status=200" in line for line in lines)
    assert all("provider=fake" in line for line in lines)
    assert all("stage=search" in line for line in lines)
    assert all("endpoint=https://provider.example.test/search" in line for line in lines)
    assert "ENDPOINT_PASSWORD_SENTINEL" not in logged
    assert "QUERY_PARAMETER_SENTINEL" not in logged
    assert "#fragment" not in logged
    assert "credential-value" not in logged
    assert "SENSITIVE_BODY" not in logged
    assert "Request(" not in logged


async def test_http_executor_request_text_returns_exact_body_without_json_decode_or_payload_logging(
) -> None:
    body = "# fetched\n<div>TEXT_RESPONSE_MARKER</div>"
    logger, stream = structured_test_logger("tests.http.text-success")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(1, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        result = await executor.request_text(
            "POST",
            "https://endpoint-user:ENDPOINT_MARKER@provider.example.test/fetch?q=QUERY_MARKER#fragment",
            stage="fetch",
            headers={"Authorization": "Bearer [REDACTED_SECRET]"},
            json_body={"url": "TARGET_MARKER"},
        )

    assert result == body
    logged = stream.getvalue()
    lines = logged.splitlines()
    assert sum("event=http_attempt_started" in line for line in lines) == 1
    assert sum("event=http_attempt_completed" in line for line in lines) == 1
    assert all("endpoint=https://provider.example.test/fetch" in line for line in lines)
    for marker in (
        "TEXT_RESPONSE_MARKER",
        "ENDPOINT_MARKER",
        "QUERY_MARKER",
        "TARGET_MARKER",
        "Request(",
    ):
        assert marker not in logged


async def test_http_executor_request_text_retries_retryable_statuses() -> None:
    attempts = 0
    response_bodies = ["ERROR_ONE_MARKER", "ERROR_TWO_MARKER", "FINAL_TEXT_MARKER"]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        status = (500, 429, 200)[attempts]
        body = response_bodies[attempts]
        attempts += 1
        return httpx.Response(status, text=body, request=request)

    logger, stream = structured_test_logger("tests.http.text-retry-status")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        result = await executor.request_text(
            "GET",
            "https://provider.example.test/fetch",
            stage="fetch",
        )

    assert result == "FINAL_TEXT_MARKER"
    assert attempts == 3
    logged = stream.getvalue()
    retry_lines = [line for line in logged.splitlines() if "event=http_retrying" in line]
    assert len(retry_lines) == 2
    assert "category=status" in retry_lines[0] and "status=500" in retry_lines[0]
    assert "category=status" in retry_lines[1] and "status=429" in retry_lines[1]
    assert "delay_ms=10" in retry_lines[0]
    assert "delay_ms=20" in retry_lines[1]
    for body in response_bodies:
        assert body not in logged


async def test_http_executor_request_text_maps_non_retryable_status_without_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, text="TERMINAL_BODY_MARKER", request=request)

    logger, stream = structured_test_logger("tests.http.text-terminal-status")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        with pytest.raises(ExecutionFailure) as caught:
            await executor.request_text(
                "GET",
                "https://provider.example.test/fetch",
                stage="fetch",
            )

    assert attempts == 1
    assert caught.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert "HTTP status 401" in caught.value.message
    assert "TERMINAL_BODY_MARKER" not in caught.value.message
    logged = stream.getvalue()
    assert "category=status" in logged and "status=401" in logged
    assert "TERMINAL_BODY_MARKER" not in logged


async def test_http_executor_request_text_retries_transport_failure() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("TRANSPORT_MARKER", request=request)
        return httpx.Response(200, text="FETCHED_TEXT", request=request)

    logger, stream = structured_test_logger("tests.http.text-transport")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(2, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        result = await executor.request_text(
            "GET",
            "https://provider.example.test/fetch",
            stage="fetch",
        )

    assert result == "FETCHED_TEXT"
    assert attempts == 2
    logged = stream.getvalue()
    assert "event=http_retrying" in logged
    assert "category=transport" in logged
    assert "TRANSPORT_MARKER" not in logged
    assert "FETCHED_TEXT" not in logged


async def test_http_executor_request_text_redacts_userinfo_query_and_fragment() -> None:
    logger, stream = structured_test_logger("tests.http.text-redaction")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(1, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        await executor.request_text(
            "GET",
            "https://name:USERINFO_MARKER@provider.example.test/path?q=QUERY_MARKER&url=TARGET_MARKER#FRAGMENT_MARKER",
            stage="fetch",
        )

    logged = stream.getvalue()
    assert "endpoint=https://provider.example.test/path" in logged
    for marker in ("USERINFO_MARKER", "QUERY_MARKER", "TARGET_MARKER", "FRAGMENT_MARKER"):
        assert marker not in logged


async def test_http_executor_request_text_allows_empty_success_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(1, 0.01, 0.02, 1.0),
            provider_name="fake",
            sleep=_no_sleep,
        )
        result = await executor.request_text(
            "GET",
            "https://provider.example.test/fetch",
            stage="fetch",
        )

    assert result == ""


async def test_http_executor_classifies_invalid_json_as_protocol_failure_without_retry() -> None:
    attempts = 0
    times = iter([0.0, 10.0, 11.0, 12.0])

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, text="not-json", request=request)

    logger, stream = structured_test_logger("tests.http.invalid-json")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
            monotonic=lambda: next(times),
        )
        with pytest.raises(ProtocolFailure) as caught:
            await executor.request_json(
                "GET",
                "https://provider.example.test/data",
                stage="fetch",
            )

    assert caught.value.code is ErrorCode.PROTOCOL_ERROR
    assert attempts == 1
    logged = stream.getvalue()
    assert "event=http_attempt_started" in logged
    assert "event=http_attempt_completed" in logged
    assert "status=200" in logged
    assert "event=http_failed" in logged
    failed_line = next(line for line in logged.splitlines() if "event=http_failed" in line)
    assert "category=decode" in failed_line
    assert "attempt=1" in failed_line
    assert "elapsed_ms=2000" in failed_line
    assert "not-json" not in logged


async def test_http_executor_maps_non_retryable_http_error_to_execution_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="do-not-log-response-body", request=request)

    logger, stream = structured_test_logger("tests.http.terminal-status")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        with pytest.raises(ExecutionFailure) as caught:
            await executor.request_json(
                "GET",
                "https://provider.example.test/data",
                stage="fetch",
            )

    assert caught.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert "do-not-log-response-body" not in caught.value.message
    logged = stream.getvalue()
    assert "event=http_failed" in logged
    assert "category=status" in logged
    assert "status=401" in logged
    assert "elapsed_ms=" in logged
    assert "do-not-log-response-body" not in logged


async def test_http_executor_terminal_transport_failure_includes_elapsed_time() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("TERMINAL_TRANSPORT_SENTINEL", request=request)

    logger, stream = structured_test_logger("tests.http.terminal-transport")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(1, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        with pytest.raises(ExecutionFailure):
            await executor.request_json(
                "GET",
                "https://provider.example.test/data",
                stage="fetch",
            )

    logged = stream.getvalue()
    assert "event=http_failed" in logged
    assert "category=transport" in logged
    assert "elapsed_ms=" in logged
    assert "TERMINAL_TRANSPORT_SENTINEL" not in logged


async def test_http_executor_logs_transport_retry_without_exception_or_payload_text() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("TRANSPORT_DETAIL_SENTINEL", request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    logger, stream = structured_test_logger("tests.http.transport")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(2, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        result = await executor.request_json(
            "POST",
            "https://provider.example.test/search",
            stage="search",
            json_body={"query": "REQUEST_BODY_SENTINEL"},
        )

    assert result == {"ok": True}
    assert attempts == 2
    logged = stream.getvalue()
    assert "event=http_retrying" in logged
    assert "category=transport" in logged
    assert "attempt=1" in logged
    assert "delay_ms=10" in logged
    assert "event=http_attempt_completed" in logged
    assert "status=200" in logged
    assert "TRANSPORT_DETAIL_SENTINEL" not in logged
    assert "REQUEST_BODY_SENTINEL" not in logged


async def test_http_executor_passes_query_params_without_logging_values() -> None:
    seen_url = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_url
        seen_url = str(request.url)
        return httpx.Response(200, json={"ok": True}, request=request)

    logger, stream = structured_test_logger("tests.http.params")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(1, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        result = await executor.request_json(
            "GET",
            "https://provider.example.test/search",
            stage="search",
            params={"q": "QUERY_PARAM_SENTINEL", "email": "CONTACT_SENTINEL@example.test"},
        )

    assert result == {"ok": True}
    assert "QUERY_PARAM_SENTINEL" in seen_url
    assert "CONTACT_SENTINEL" in seen_url
    logged = stream.getvalue()
    assert "endpoint=https://provider.example.test/search" in logged
    assert "QUERY_PARAM_SENTINEL" not in logged
    assert "CONTACT_SENTINEL" not in logged


async def test_http_executor_text_mode_retries_without_json_decoding() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text="retry-body", request=request)
        return httpx.Response(200, text="<feed>not-json</feed>", request=request)

    logger, stream = structured_test_logger("tests.http.text")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(2, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        result = await executor.request_text(
            "GET",
            "https://provider.example.test/feed",
            stage="search",
        )

    assert result == "<feed>not-json</feed>"
    assert attempts == 2
    assert "event=http_retrying" in stream.getvalue()


async def test_http_executor_status_failure_carries_terminal_status_code() -> None:
    from agent_search_gateway.providers.http import HttpStatusFailure

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="DO_NOT_EXPOSE_BODY", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(1, 0.01, 0.02, 1.0),
            provider_name="fake",
            sleep=_no_sleep,
        )
        with pytest.raises(HttpStatusFailure) as caught:
            await executor.request_text(
                "GET",
                "https://provider.example.test/missing",
                stage="resolve",
            )

    assert caught.value.status_code == 404
    assert caught.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert "DO_NOT_EXPOSE_BODY" not in caught.value.message
