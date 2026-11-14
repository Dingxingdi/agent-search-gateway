import asyncio
import io
import logging

import httpx
import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor, SecretValue
from agent_search_gateway.providers.http import (
    HttpJsonExecutor,
    HttpStatusFailure,
    ProviderResponseFailure,
)
from tests.support.logging import structured_test_logger


async def test_every_received_response_has_bounded_diagnostics() -> None:
    responses = [
        httpx.Response(
            429,
            content=b"rate limit",
            headers={"Content-Type": "text/plain; charset=utf-8", "Content-Length": "999"},
        ),
        httpx.Response(502, content=b"bad gateway", headers={"Content-Type": "text/html"}),
        httpx.Response(200, json={"ok": True}),
    ]
    expected = [(response.status_code, len(response.content)) for response in responses]
    requests: list[httpx.Request] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    logger, stream = structured_test_logger("tests.http.response-diagnostics")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="fake",
            logger=logger,
            sleep=sleep,
        )
        result = await executor.request_json(
            "GET",
            "https://provider.example.test/search",
            stage="search",
        )

    assert result == {"ok": True}
    assert len(requests) == 3
    assert delays == [0.25, 0.5]
    lines = stream.getvalue().splitlines()
    completed = [line for line in lines if "event=http_attempt_completed " in line]
    diagnostics = [line for line in lines if "event=provider_response_diagnostic " in line]
    assert len(completed) == len(diagnostics) == 3
    assert sum("event=http_retrying " in line for line in lines) == 2
    assert not any("event=http_failed " in line for line in lines)
    for events in (completed, diagnostics):
        for attempt, (line, (status, size), media_type) in enumerate(
            zip(events, expected, ("text/plain", "text/html", "application/json"), strict=True), 1
        ):
            assert f"attempt={attempt}" in line
            assert f"status={status}" in line and f"http_status={status}" in line
            assert f"response_bytes={size}" in line
            assert f"content_type={media_type}" in line


@pytest.mark.parametrize("mode,status", [("json", 201), ("text", 202), ("text", 204)])
async def test_response_diagnostics_cover_accepted_success_statuses(mode: str, status: int) -> None:
    response = (
        httpx.Response(status, json={"ok": True})
        if mode == "json"
        else httpx.Response(status, text="accepted")
    )
    logger, stream = structured_test_logger("tests.http.accepted-status")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response)
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0.25, 2.0, 30.0), provider_name="fake", logger=logger
        )
        request = executor.request_json if mode == "json" else executor.request_text
        assert await request("GET", "https://provider.example.test/data", stage="fetch") == (
            {"ok": True} if mode == "json" else "accepted"
        )
    diagnostics = [
        line
        for line in stream.getvalue().splitlines()
        if "event=provider_response_diagnostic " in line
    ]
    assert len(diagnostics) == 1
    assert f"http_status={status}" in diagnostics[0]
    assert f"response_bytes={len(response.content)}" in diagnostics[0]
    assert "event=http_failed " not in stream.getvalue()


async def test_http_status_exhaustion_carries_last_response_diagnostics() -> None:
    responses = [httpx.Response(429, text="first"), httpx.Response(502, text="last response")]
    requests: list[httpx.Request] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    logger, stream = structured_test_logger("tests.http.last-status-diagnostics")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(2, 0.25, 2.0, 30.0),
            provider_name="fake",
            logger=logger,
            sleep=sleep,
        )
        with pytest.raises(HttpStatusFailure) as caught:
            await executor.request_json("GET", "https://provider.example.test/data", stage="search")
    assert caught.value.status_code == 502
    assert caught.value.diagnostics == {
        "status": 502,
        "http_status": 502,
        "response_bytes": 13,
        "content_type": "text/plain",
    }
    assert len(requests) == 2 and delays == [0.25]
    lines = stream.getvalue().splitlines()
    assert sum("event=provider_response_diagnostic " in line for line in lines) == 2
    failed = [line for line in lines if "event=http_failed " in line]
    assert len(failed) == 1
    assert "http_status=502" in failed[0] and "response_bytes=13" in failed[0]
    assert "event=provider_failed " not in stream.getvalue()


@pytest.mark.parametrize(
    ("provider", "recovers"),
    [("arxiv", True), ("arxiv", False), ("dblp", True), ("dblp", False), ("json", False)],
)
async def test_http_status_overrides_successful_content_diagnostics(
    caplog: pytest.LogCaptureFixture, provider: str, recovers: bool
) -> None:
    from agent_search_gateway.providers.academic.arxiv import (
        _response_diagnostics as arxiv_diagnostics,
    )
    from agent_search_gateway.providers.academic.dblp import (
        _response_diagnostics as dblp_diagnostics,
    )

    caplog.set_level(logging.DEBUG)
    body = {
        "arxiv": '<feed xmlns="http://www.w3.org/2005/Atom"/>',
        "dblp": "<result><hits/></result>",
        "json": '{"ok":true}',
    }[provider]
    content_type = "application/json" if provider == "json" else "application/xml"
    response_kind = "object" if provider == "json" else "xml"

    def json_diagnostics(_response: httpx.Response) -> dict[str, object]:
        return {"semantic_result": "success", "response_kind": "object"}

    extractor = {"arxiv": arxiv_diagnostics, "dblp": dblp_diagnostics, "json": json_diagnostics}[
        provider
    ]
    statuses = [500, 200 if recovers else 500]
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            statuses.pop(0), text=body, headers={"Content-Type": content_type}, request=request
        )

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(2, 0.25, 2.0, 30.0), provider_name=provider, sleep=sleep
        )
        request = executor.request_json if provider == "json" else executor.request_text
        if recovers:
            assert (
                await request(
                    "GET",
                    "https://provider.example.test/data",
                    stage="search",
                    response_diagnostics=extractor,
                )
                == body
            )
        else:
            with pytest.raises(HttpStatusFailure) as caught:
                await request(
                    "GET",
                    "https://provider.example.test/data",
                    stage="search",
                    response_diagnostics=extractor,
                )
            assert caught.value.status_code == 500
            assert caught.value.code is ErrorCode.ALL_PROVIDERS_FAILED
            assert caught.value.diagnostics["semantic_result"] == "error"
            assert caught.value.diagnostics["category"] == "http_status"
            assert caught.value.diagnostics["response_kind"] == response_kind
            assert caught.value.diagnostics["response_bytes"] == len(body.encode())
            assert caught.value.diagnostics["content_type"] == content_type

    assert statuses == [] and delays == [0.25]
    events = [
        (getattr(record, "gateway_event", None), vars(record).get("gateway_fields", {}))
        for record in caplog.records
    ]
    diagnostics = [fields for event, fields in events if event == "provider_response_diagnostic"]
    assert len(diagnostics) == 2
    for attempt, fields in enumerate(diagnostics, 1):
        status = 200 if recovers and attempt == 2 else 500
        assert fields["attempt"] == attempt
        assert fields["http_status"] == fields["status"] == status
        assert fields["response_kind"] == response_kind
        assert fields["response_bytes"] == len(body.encode())
        assert fields["content_type"] == content_type
        assert fields["semantic_result"] == ("success" if status == 200 else "error")
        if status == 500:
            assert fields["category"] == "http_status"
    retries = [fields for event, fields in events if event == "http_retrying"]
    failures = [fields for event, fields in events if event == "http_failed"]
    assert len(retries) == 1 and len(failures) == (0 if recovers else 1)
    for fields in [*retries, *failures]:
        assert fields["category"] == "status"
        assert fields["semantic_result"] == "error"


@pytest.mark.parametrize("first_response", [False, True])
async def test_transport_failure_does_not_inherit_response_metadata(first_response: bool) -> None:
    requests: list[httpx.Request] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if first_response and len(requests) == 1:
            return httpx.Response(429, text="OLD_RESPONSE_MARKER")
        raise httpx.ConnectError("TRANSPORT_MARKER", request=request)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    logger, stream = structured_test_logger("tests.http.no-stale-response")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(2, 0.25, 2.0, 30.0),
            provider_name="fake",
            logger=logger,
            sleep=sleep,
        )
        with pytest.raises(ExecutionFailure) as caught:
            await executor.request_json(
                "GET",
                "https://provider.example.test/data",
                stage="search",
                response_diagnostics=lambda _response: {"upstream_code": "earlier_response"},
            )
    assert type(caught.value) is ExecutionFailure
    assert caught.value.diagnostics == {}
    assert isinstance(caught.value.__cause__, httpx.ConnectError)
    assert len(requests) == 2 and delays == [0.25]
    lines = stream.getvalue().splitlines()
    for event in ("http_attempt_completed", "provider_response_diagnostic"):
        assert sum(f"event={event} " in line for line in lines) == int(first_response)
    failed = next(line for line in lines if "event=http_failed " in line)
    assert "category=transport" in failed
    for key in ("status=", "response_bytes=", "content_type=", "upstream_"):
        assert key not in failed
    assert "OLD_RESPONSE_MARKER" not in stream.getvalue()
    assert "TRANSPORT_MARKER" not in stream.getvalue()


async def test_json_decode_failure_carries_response_diagnostics_without_retry() -> None:
    response = httpx.Response(200, text="INVALID_JSON_MARKER")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response

    logger, stream = structured_test_logger("tests.http.decode-diagnostics")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        with pytest.raises(ProtocolFailure) as caught:
            await executor.request_json("GET", "https://provider.example.test/data", stage="search")
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR
    assert caught.value.diagnostics == {
        "status": 200,
        "http_status": 200,
        "response_bytes": len(response.content),
        "content_type": "text/plain",
    }
    assert len(requests) == 1
    lines = stream.getvalue().splitlines()
    assert sum("event=provider_response_diagnostic " in line for line in lines) == 1
    failed = next(line for line in lines if "event=http_failed " in line)
    assert "category=decode" in failed
    assert "http_status=200" in failed and f"response_bytes={len(response.content)}" in failed
    assert "INVALID_JSON_MARKER" not in stream.getvalue()
    assert "event=http_retrying " not in stream.getvalue()


async def test_response_diagnostic_callback_renders_only_bounded_selected_fields() -> None:
    response = httpx.Response(
        200,
        json={"error": {"code": "c" * 129, "message": "界" * 513}, "body": "BODY_MARKER" * 10000},
    )
    inspected: list[httpx.Response] = []

    def diagnostics(value: httpx.Response) -> dict[str, object]:
        inspected.append(value)
        error = value.json()["error"]
        return {
            "upstream_code": error["code"],
            "upstream_message": error["message"],
            "semantic_result": "error",
            "category": "provider_response",
        }

    logger, stream = structured_test_logger("tests.http.bounded-callback")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response)
    ) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        result = await executor.request_json(
            "GET",
            "https://provider.example.test/data",
            stage="search",
            response_diagnostics=diagnostics,
        )
    assert result == response.json()
    assert inspected == [response]
    lines = stream.getvalue().splitlines()
    diagnostic = [line for line in lines if "event=provider_response_diagnostic " in line]
    assert len(diagnostic) == 1
    assert "upstream_code=" + "c" * 127 + "…" in diagnostic[0]
    assert "upstream_message=" + "界" * 511 + "…" in diagnostic[0]
    assert "upstream_message_chars=513" in diagnostic[0]
    assert "upstream_message_bytes=1539" in diagnostic[0]
    assert "upstream_message_truncated=true" in diagnostic[0]
    assert (
        "semantic_result=error" in diagnostic[0] and "category=provider_response" in diagnostic[0]
    )
    assert f"response_bytes={len(response.content)}" in diagnostic[0]
    assert "BODY_MARKER" not in stream.getvalue()
    assert "event=http_retrying " not in stream.getvalue()
    assert "event=http_failed " not in stream.getvalue()


async def test_inspector_failure_merges_response_diagnostics_despite_callback_error() -> None:
    failure = ProviderResponseFailure(
        "fake",
        "search",
        status_code=200,
        retryable=False,
        safe_reason="provider_reported_failure",
        diagnostics={
            "upstream_code": "captcha",
            "brightdata_status": 502,
            "brightdata_error_code": "captcha",
        },
    )
    response = httpx.Response(200, content=b"", headers={"Content-Type": "text/html"})

    def diagnostics(_response: httpx.Response) -> dict[str, object]:
        raise ValueError("DIAGNOSTIC_ERROR_MARKER")

    logger, stream = structured_test_logger("tests.http.inspector-diagnostics")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response)
    ) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await executor.request_json(
                "GET",
                "https://provider.example.test/data",
                stage="search",
                response_inspector=lambda _response: failure,
                response_diagnostics=diagnostics,
            )
    assert caught.value is not failure
    assert caught.value.__cause__ is failure
    assert caught.value.message == failure.message
    assert caught.value.diagnostics["http_status"] == 200
    assert caught.value.diagnostics["response_bytes"] == 0
    assert caught.value.diagnostics["content_type"] == "text/html"
    lines = stream.getvalue().splitlines()
    diagnostic = [line for line in lines if "event=provider_response_diagnostic " in line]
    assert len(diagnostic) == 1
    assert "upstream_code=captcha" in diagnostic[0]
    assert (
        "brightdata_status=502" in diagnostic[0]
        and "brightdata_error_code=captcha" in diagnostic[0]
    )
    assert "http_status=200" in diagnostic[0] and "semantic_result=error" in diagnostic[0]
    assert sum("event=http_attempt_completed " in line for line in lines) == 1
    assert sum("event=http_failed " in line for line in lines) == 1
    assert "event=http_retrying " not in stream.getvalue()
    assert "category=decode" not in stream.getvalue()
    assert "DIAGNOSTIC_ERROR_MARKER" not in stream.getvalue()


@pytest.mark.parametrize(
    "content_type", [None, "UNSAFE_MEDIA_TYPE_MARKER", "Text/HTML; charset=utf-8"]
)
async def test_diagnostic_callback_cannot_override_context_or_transport(
    content_type: str | None,
) -> None:
    response = httpx.Response(
        200, content=b"ok", headers={} if content_type is None else {"Content-Type": content_type}
    )
    logger, stream = structured_test_logger("tests.http.callback-overrides")

    def diagnostics(_response: httpx.Response) -> dict[str, object]:
        return {
            "provider": "OVERRIDE_MARKER",
            "stage": "OVERRIDE_MARKER",
            "attempt": 99,
            "event": "OVERRIDE_MARKER",
            "elapsed_ms": 999,
            "endpoint": "OVERRIDE_MARKER",
            "status": 503,
            "http_status": 503,
            "response_bytes": 999,
            "content_type": "application/forged",
            "upstream_status": 502,
            "category": "unknown",
            "semantic_result": "unknown",
            "body": "BODY_MARKER",
            "upstream_message_chars": 999,
        }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response)
    ) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        assert (
            await executor.request_text(
                "GET",
                "https://provider.example.test/data",
                stage="fetch",
                response_diagnostics=diagnostics,
            )
            == "ok"
        )
    line = next(
        line
        for line in stream.getvalue().splitlines()
        if "event=provider_response_diagnostic " in line
    )
    assert "provider=fake" in line and "stage=fetch" in line and "attempt=1" in line
    assert "status=200" in line and "http_status=200" in line and "response_bytes=2" in line
    assert "upstream_status=502" in line and "category=unknown" in line
    if content_type == "Text/HTML; charset=utf-8":
        assert "content_type=text/html" in line
    else:
        assert "content_type=" not in line
    for marker in (
        "OVERRIDE_MARKER",
        "BODY_MARKER",
        "application/forged",
        "UNSAFE_MEDIA_TYPE_MARKER",
        "upstream_message_chars=",
    ):
        assert marker not in stream.getvalue()


async def test_status_retry_and_final_failure_keep_each_attempt_diagnostics() -> None:
    responses = [
        httpx.Response(429, json={"code": "limited"}),
        httpx.Response(502, json={"code": "unavailable"}),
    ]
    requests: list[httpx.Request] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    def diagnostics(response: httpx.Response) -> dict[str, object]:
        return {
            "upstream_code": response.json()["code"],
            "category": "decode",
            "semantic_result": "error",
        }

    logger, stream = structured_test_logger("tests.http.retry-diagnostics")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(2, 0.25, 2.0, 30.0),
            provider_name="fake",
            logger=logger,
            sleep=sleep,
        )
        with pytest.raises(HttpStatusFailure) as caught:
            await executor.request_json(
                "GET",
                "https://provider.example.test/data",
                stage="search",
                response_diagnostics=diagnostics,
            )
    assert caught.value.status_code == 502
    assert caught.value.diagnostics["http_status"] == 502
    assert len(requests) == 2 and delays == [0.25]
    lines = stream.getvalue().splitlines()
    retry = next(line for line in lines if "event=http_retrying " in line)
    assert (
        "http_status=429" in retry
        and "upstream_code=limited" in retry
        and "category=status" in retry
    )
    failed = next(line for line in lines if "event=http_failed " in line)
    assert (
        "http_status=502" in failed
        and "upstream_code=unavailable" in failed
        and "category=status" in failed
    )
    assert "upstream_code=limited" not in failed
    assert sum("event=provider_response_diagnostic " in line for line in lines) == 2


@pytest.mark.parametrize("mode", ["json", "text"])
async def test_diagnostic_callback_error_cannot_change_success(mode: str) -> None:
    response = (
        httpx.Response(200, json={"ok": True}) if mode == "json" else httpx.Response(200, text="ok")
    )

    def diagnostics(_response: httpx.Response) -> dict[str, object]:
        raise ValueError("CALLBACK_ERROR_MARKER")

    logger, stream = structured_test_logger("tests.http.callback-error")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response)
    ) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        request = executor.request_json if mode == "json" else executor.request_text
        assert await request(
            "GET",
            "https://provider.example.test/data",
            stage="search",
            response_diagnostics=diagnostics,
        ) == ({"ok": True} if mode == "json" else "ok")
    lines = stream.getvalue().splitlines()
    assert sum("event=provider_response_diagnostic " in line for line in lines) == 1
    assert sum("event=http_attempt_completed " in line for line in lines) == 1
    assert "event=http_retrying " not in stream.getvalue()
    assert "CALLBACK_ERROR_MARKER" not in stream.getvalue()


@pytest.mark.parametrize("mode", ["json", "text"])
async def test_callback_cancellation_propagates_after_received_response(mode: str) -> None:
    cancellation = asyncio.CancelledError()

    def diagnostics(_response: httpx.Response) -> dict[str, object]:
        raise cancellation

    logger, stream = structured_test_logger("tests.http.callback-cancellation")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"ok": True}))
    ) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        request = executor.request_json if mode == "json" else executor.request_text
        with pytest.raises(asyncio.CancelledError) as caught:
            await request(
                "GET",
                "https://provider.example.test/data",
                stage="search",
                response_diagnostics=diagnostics,
            )
    assert caught.value is cancellation
    lines = stream.getvalue().splitlines()
    assert sum("event=http_attempt_completed " in line for line in lines) == 1
    assert sum("event=provider_response_diagnostic " in line for line in lines) == 1
    assert "event=http_failed " not in stream.getvalue()


async def test_diagnostic_callback_error_does_not_swallow_raised_business_inspector_error() -> None:
    business_failure = ValueError("BUSINESS_FAILURE_MARKER")

    def inspector(_response: httpx.Response) -> None:
        raise business_failure

    def diagnostics(_response: httpx.Response) -> dict[str, object]:
        raise RuntimeError("DIAGNOSTIC_FAILURE_MARKER")

    logger, stream = structured_test_logger("tests.http.raised-inspector-error")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    ) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        with pytest.raises(ValueError) as caught:
            await executor.request_json(
                "GET",
                "https://provider.example.test/data",
                stage="search",
                response_inspector=inspector,
                response_diagnostics=diagnostics,
            )
    assert caught.value is business_failure
    assert stream.getvalue().count("event=provider_response_diagnostic ") == 1
    assert "category=decode" not in stream.getvalue()
    assert "DIAGNOSTIC_FAILURE_MARKER" not in stream.getvalue()


async def test_sink_failure_does_not_change_http_retry_or_payload() -> None:
    class BrokenSink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise RuntimeError("SINK_ERROR_MARKER")

    responses = [httpx.Response(429), httpx.Response(200, json={"ok": True})]
    logger, _ = structured_test_logger("tests.http.broken-sink")
    logger.handlers[:] = [BrokenSink()]
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: responses.pop(0))
    ) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(2, 0.25, 2.0, 30.0),
            provider_name="fake",
            logger=logger,
            sleep=sleep,
        )
        assert await executor.request_json(
            "GET", "https://provider.example.test/data", stage="search"
        ) == {"ok": True}
    assert responses == [] and delays == [0.25]


@pytest.mark.parametrize("mode", ["json", "text"])
async def test_large_unknown_payloads_do_not_invent_upstream_errors(mode: str) -> None:
    payload = {"nested": {"error": {"message": "BODY_MARKER" * 10000}}}
    response = (
        httpx.Response(200, json=payload)
        if mode == "json"
        else httpx.Response(
            200,
            text="<html>" + "BODY_MARKER" * 10000 + "</html>",
            headers={"Content-Type": "text/html"},
        )
    )
    logger, stream = structured_test_logger("tests.http.unknown-payload")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response)
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0.25, 2.0, 30.0), provider_name="fake", logger=logger
        )
        request = executor.request_json if mode == "json" else executor.request_text
        assert await request(
            "GET",
            "https://provider.example.test/data",
            stage="fetch",
            response_diagnostics=lambda _response: {
                "category": "unknown",
                "upstream_message": payload,
                "response": payload,
            },
        ) == (payload if mode == "json" else response.text)
    line = next(
        line
        for line in stream.getvalue().splitlines()
        if "event=provider_response_diagnostic " in line
    )
    assert f"response_bytes={len(response.content)}" in line and "category=unknown" in line
    assert "upstream_" not in line
    assert "BODY_MARKER" not in stream.getvalue()
    assert len(line) < 512


async def test_reused_inspector_failure_does_not_retain_earlier_callback_fields() -> None:
    failure = ProviderResponseFailure(
        "fake",
        "search",
        status_code=200,
        retryable=True,
        safe_reason="provider_reported_failure",
        diagnostics={"brightdata_error_code": "captcha"},
    )
    responses = [httpx.Response(200, content=b"first"), httpx.Response(200, content=b"")]

    def diagnostics(response: httpx.Response) -> dict[str, object]:
        return {"upstream_code": "FIRST_ATTEMPT"} if response.content else {}

    logger, stream = structured_test_logger("tests.http.reused-inspector")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: responses.pop(0))
    ) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(2, 0.25, 2.0, 30.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await executor.request_json(
                "GET",
                "https://provider.example.test/data",
                stage="search",
                response_inspector=lambda _response: failure,
                response_diagnostics=diagnostics,
            )
    assert caught.value is not failure
    assert caught.value.__cause__ is failure
    assert caught.value.message == failure.message
    assert caught.value.diagnostics["response_bytes"] == 0
    assert "upstream_code" not in caught.value.diagnostics
    lines = stream.getvalue().splitlines()
    diagnostics_lines = [line for line in lines if "event=provider_response_diagnostic " in line]
    assert len(diagnostics_lines) == 2
    assert "upstream_code=FIRST_ATTEMPT" in diagnostics_lines[0]
    assert "upstream_code=" not in diagnostics_lines[1]
    failed = next(line for line in lines if "event=http_failed " in line)
    assert "upstream_code=" not in failed


async def test_terminal_inspector_diagnostics_are_isolated_across_calls() -> None:
    failure = ProviderResponseFailure(
        "fake",
        "search",
        status_code=200,
        retryable=False,
        safe_reason="provider_reported_failure",
        diagnostics={"brightdata_error_code": "captcha"},
    )
    caught_failures: list[ProviderResponseFailure] = []
    logger, stream = structured_test_logger("tests.http.inspector-cross-call")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0.25, 2.0, 30.0), provider_name="fake", logger=logger
        )
        for code in ("FIRST_CALL", None):

            def diagnostics(
                _response: httpx.Response, selected_code: str | None = code
            ) -> dict[str, object]:
                return {"upstream_code": selected_code} if selected_code is not None else {}

            with pytest.raises(ProviderResponseFailure) as caught:
                await executor.request_json(
                    "GET",
                    "https://provider.example.test/data",
                    stage="search",
                    response_inspector=lambda _response: failure,
                    response_diagnostics=diagnostics,
                )
            caught_failures.append(caught.value)
    assert failure.diagnostics == {"brightdata_error_code": "captcha"}
    assert caught_failures[0] is not caught_failures[1]
    assert "upstream_code" in caught_failures[0].diagnostics
    assert "upstream_code" not in caught_failures[1].diagnostics
    for terminal in caught_failures:
        assert terminal.code is failure.code
        assert terminal.message == failure.message
        assert terminal.safe_reason == failure.safe_reason
        assert terminal.retryable is failure.retryable
        assert terminal.status_code == failure.status_code
    lines = [
        line
        for line in stream.getvalue().splitlines()
        if "event=provider_response_diagnostic " in line
    ]
    assert len(lines) == 2 and "upstream_code=FIRST_CALL" in lines[0]
    assert "upstream_code=" not in lines[1]


async def test_json_response_inspector_runs_before_decode() -> None:
    inspected: list[httpx.Response] = []
    response = httpx.Response(200, json={"ok": True})

    def inspect_response(value: httpx.Response) -> None:
        inspected.append(value)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response)
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(3, 0.25, 2.0, 30.0), provider_name="fake", sleep=_no_sleep
        )
        result = await executor.request_json(
            "GET",
            "https://provider.example.test/search",
            stage="search",
            response_inspector=inspect_response,
        )

    assert result == {"ok": True}
    assert inspected == [response]


async def test_json_response_failure_preempts_invalid_json() -> None:
    failure = ProviderResponseFailure(
        "fake",
        "search",
        status_code=200,
        retryable=False,
        safe_reason="fixture_failure",
        diagnostics={"upstream_code": "terminal"},
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    logger, stream = structured_test_logger("tests.http.inspected-failure")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await executor.request_json(
                "GET",
                "https://provider.example.test/search",
                stage="search",
                response_inspector=lambda _response: failure,
            )

    assert caught.value is not failure
    assert caught.value.__cause__ is failure
    assert caught.value.message == failure.message
    assert failure.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert len(requests) == 1
    assert "category=provider_header" in stream.getvalue()
    assert "category=decode" not in stream.getvalue()
    assert "transport_type=" not in stream.getvalue()


@pytest.mark.parametrize("retryable", [False, True])
async def test_json_response_failure_retries_only_when_marked_retryable(retryable: bool) -> None:
    failure = ProviderResponseFailure(
        "fake",
        "search",
        status_code=200,
        retryable=retryable,
        safe_reason="fixture_failure",
        diagnostics={},
    )
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    def inspect_response(_response: httpx.Response) -> ProviderResponseFailure | None:
        return failure if len(requests) == 1 else None

    logger, stream = structured_test_logger("tests.http.inspected-retry")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="fake",
            logger=logger,
            sleep=sleep,
        )
        if retryable:
            result = await executor.request_json(
                "GET",
                "https://provider.example.test/search",
                stage="search",
                response_inspector=inspect_response,
            )
            assert result == {"ok": True}
        else:
            with pytest.raises(ProviderResponseFailure) as caught:
                await executor.request_json(
                    "GET",
                    "https://provider.example.test/search",
                    stage="search",
                    response_inspector=inspect_response,
                )
            assert caught.value is not failure
            assert caught.value.__cause__ is failure
            assert caught.value.message == failure.message

    assert len(requests) == (2 if retryable else 1)
    assert len(sleeps) == (1 if retryable else 0)
    assert "category=provider_header" in stream.getvalue()
    assert "transport_type=" not in stream.getvalue()


async def test_json_response_failures_share_http_attempt_budget() -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []
    failures: list[ProviderResponseFailure] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response((200, 500, 200)[len(requests) - 1])

    def inspect_response(response: httpx.Response) -> ProviderResponseFailure | None:
        if response.status_code != 200:
            return None
        failure = ProviderResponseFailure(
            "fake",
            "search",
            status_code=200,
            retryable=True,
            safe_reason="fixture_failure",
            diagnostics={"fixture_status": 502},
        )
        failures.append(failure)
        return failure

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 37.0),
            provider_name="fake",
            sleep=sleep,
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await executor.request_json(
                "GET",
                "https://provider.example.test/search",
                stage="search",
                response_inspector=inspect_response,
            )

    assert len(requests) == 3
    assert len(failures) == 2
    assert caught.value is not failures[-1]
    assert caught.value.__cause__ is failures[-1]
    assert caught.value.message == failures[-1].message
    assert len(sleeps) == 2
    assert sleeps[1] == 0.5
    for request in requests:
        assert request.extensions["timeout"] == {
            "connect": 37.0,
            "read": 37.0,
            "write": 37.0,
            "pool": 37.0,
        }


async def test_json_response_inspection_is_per_call() -> None:
    inspected: list[httpx.Response] = []
    responses = [
        httpx.Response(200, json={"first": True}),
        httpx.Response(200, json={"second": True}),
        httpx.Response(200, text="plain text"),
    ]

    def inspect_response(response: httpx.Response) -> None:
        inspected.append(response)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: responses.pop(0))
    ) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="fake",
            sleep=_no_sleep,
        )
        assert await executor.request_json(
            "GET",
            "https://provider.example.test/search",
            stage="search",
            response_inspector=inspect_response,
        ) == {"first": True}
        assert await executor.request_json(
            "GET",
            "https://provider.example.test/search",
            stage="search",
        ) == {"second": True}
        assert (
            await executor.request_text(
                "GET",
                "https://provider.example.test/fetch",
                stage="fetch",
            )
            == "plain text"
        )

    assert len(inspected) == 1
    assert responses == []


@pytest.mark.parametrize("retryable", [False, True])
async def test_provider_diagnostics_cannot_override_http_event_fields(retryable: bool) -> None:
    diagnostics: dict[str, object] = dict.fromkeys(
        [
            "provider",
            "stage",
            "endpoint",
            "attempt",
            "status",
            "category",
            "elapsed_ms",
            "delay_ms",
            "transport_type",
            "reason",
            "retryable",
            "retry_scheduled",
            "terminal_reason",
            "request",
            "logger",
            "level",
            "event",
            "exc_info",
        ],
        "OVERRIDE_MARKER",
    )
    diagnostics["upstream_code"] = "safe"
    diagnostics["fixture_code"] = "UNKNOWN_FIELD_MARKER"
    diagnostics.update(http_status=503, response_bytes=999, content_type="application/forged")
    failure = ProviderResponseFailure(
        "fake",
        "search",
        status_code=200,
        retryable=retryable,
        safe_reason="fixture_failure",
        diagnostics=diagnostics,
    )
    diagnostics["upstream_code"] = "MUTATION_MARKER"
    logger, stream = structured_test_logger("tests.http.reserved-provider-details")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    ) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(2, 0.25, 2.0, 30.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await executor.request_json(
                "GET",
                "https://provider.example.test/search",
                stage="search",
                response_inspector=lambda _response: failure,
            )
    assert caught.value is not failure
    assert caught.value.__cause__ is failure
    assert caught.value.message == failure.message
    logged = stream.getvalue()
    assert "OVERRIDE_MARKER" not in logged
    assert "MUTATION_MARKER" not in logged
    assert "fixture_code=" not in logged
    assert "UNKNOWN_FIELD_MARKER" not in logged
    assert "application/forged" not in logged
    events = [
        line
        for line in logged.splitlines()
        if "event=http_retrying" in line or "event=http_failed" in line
    ]
    assert len(events) == (2 if retryable else 1)
    for line in events:
        assert "upstream_code=safe" in line
        assert "category=provider_header" in line
        assert "provider=fake" in line and "stage=search" in line
        assert "status=200" in line and "reason=fixture_failure" in line
        assert "transport_type=" not in line
    assert f"attempt={2 if retryable else 1}" in events[-1]


@pytest.mark.parametrize("origin", ["send", "inspector", "sleep"])
async def test_http_inspection_preserves_cancellation(origin: str) -> None:
    cancellation = asyncio.CancelledError()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if origin == "send":
            raise cancellation
        return httpx.Response(200)

    def inspect_response(_response: httpx.Response) -> ProviderResponseFailure:
        if origin == "inspector":
            raise cancellation
        return ProviderResponseFailure(
            "fake",
            "search",
            status_code=200,
            retryable=True,
            safe_reason="fixture_failure",
            diagnostics={},
        )

    async def sleep(_delay: float) -> None:
        raise cancellation

    logger, stream = structured_test_logger("tests.http.inspection-cancellation")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="fake",
            logger=logger,
            sleep=sleep,
        )
        with pytest.raises(asyncio.CancelledError) as caught:
            await executor.request_json(
                "GET",
                "https://provider.example.test/search",
                stage="search",
                response_inspector=inspect_response,
            )
    assert caught.value is cancellation
    assert len(requests) == 1
    assert "event=http_failed" not in stream.getvalue()


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


async def test_request_text_returns_body_without_json_decode_or_payload_logging() -> None:
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
    assert "transport_type=" not in logged
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


async def test_http_executor_classifies_redirect_as_status_failure_before_json_decode() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(302, text="REDIRECT_BODY_SENTINEL", request=request)

    logger, stream = structured_test_logger("tests.http.redirect-status")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        with pytest.raises(HttpStatusFailure) as caught:
            await executor.request_json(
                "GET",
                "https://provider.example.test/data",
                stage="fetch",
            )

    assert attempts == 1
    assert caught.value.status_code == 302
    logged = stream.getvalue()
    assert "event=http_failed" in logged
    assert "category=status" in logged
    assert "status=302" in logged
    assert "category=decode" not in logged
    assert "REDIRECT_BODY_SENTINEL" not in logged


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


@pytest.mark.parametrize("response_mode", ["json", "text"])
@pytest.mark.parametrize(
    "error_type", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError]
)
@pytest.mark.parametrize("recovers", [False, True], ids=["exhausted", "recovers"])
async def test_http_executor_logs_safe_transport_subtypes(
    response_mode: str,
    error_type: type[httpx.TransportError],
    recovers: bool,
) -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if recovers and attempts == 3:
            if response_mode == "json":
                return httpx.Response(200, json={"body": "RESPONSE_BODY_MARKER"}, request=request)
            return httpx.Response(200, text="RESPONSE_BODY_MARKER", request=request)
        raise error_type("TRANSPORT_MESSAGE_MARKER", request=request)

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    logger, stream = structured_test_logger("tests.http.transport-subtypes")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
            sleep=record_sleep,
        )
        request_method = executor.request_json if response_mode == "json" else executor.request_text
        url = (
            "https://user:USERINFO_MARKER@provider.example.test/search"
            "?q=QUERY_MARKER#FRAGMENT_MARKER"
        )
        if recovers:
            result = await request_method(
                "POST", url, stage="search", json_body={"query": "REQUEST_BODY_MARKER"}
            )
            expected = (
                {"body": "RESPONSE_BODY_MARKER"}
                if response_mode == "json"
                else ("RESPONSE_BODY_MARKER")
            )
            assert result == expected
        else:
            with pytest.raises(ExecutionFailure) as caught:
                await request_method(
                    "POST", url, stage="search", json_body={"query": "REQUEST_BODY_MARKER"}
                )
            assert caught.value.code is ErrorCode.ALL_PROVIDERS_FAILED
            assert isinstance(caught.value.__cause__, error_type)
            assert caught.value.message == "fake/search: HTTP transport failure"

    assert attempts == 3
    assert delays == [0.01, 0.02]
    logged = stream.getvalue()
    lines = logged.splitlines()
    retry_lines = [line for line in lines if "event=http_retrying" in line]
    failed_lines = [line for line in lines if "event=http_failed" in line]
    assert len(retry_lines) == 2
    assert "attempt=1" in retry_lines[0] and "delay_ms=10" in retry_lines[0]
    assert "attempt=2" in retry_lines[1] and "delay_ms=20" in retry_lines[1]
    assert all(line.startswith("WARNING ") for line in retry_lines)
    assert len(failed_lines) == (0 if recovers else 1)
    if failed_lines:
        assert failed_lines[0].startswith("DEBUG ")
        assert "attempt=3" in failed_lines[0]
    for line in [*retry_lines, *failed_lines]:
        assert f"transport_type={error_type.__name__}" in line
        assert "category=transport" in line
        assert "provider=fake" in line and "stage=search" in line
        assert "endpoint=https://provider.example.test/search" in line
        assert "elapsed_ms=" in line
    for line in lines:
        if "event=http_retrying" not in line and "event=http_failed" not in line:
            assert "transport_type=" not in line
    for marker in (
        "TRANSPORT_MESSAGE_MARKER",
        "USERINFO_MARKER",
        "QUERY_MARKER",
        "FRAGMENT_MARKER",
        "REQUEST_BODY_MARKER",
        "RESPONSE_BODY_MARKER",
        "traceback=",
    ):
        assert marker not in logged


@pytest.mark.parametrize("response_mode", ["json", "text"])
@pytest.mark.parametrize("status", [404, 503])
async def test_http_status_failures_do_not_claim_a_transport_subtype(
    response_mode: str, status: int
) -> None:
    logger, stream = structured_test_logger("tests.http.status-without-transport-subtype")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, request=request))
    ) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(2, 0.01, 0.02, 1.0),
            provider_name="fake",
            logger=logger,
            sleep=_no_sleep,
        )
        request_method = executor.request_json if response_mode == "json" else executor.request_text
        with pytest.raises(HttpStatusFailure) as caught:
            await request_method("GET", "https://provider.example.test/search", stage="search")
        assert caught.value.status_code == status

    assert "category=status" in stream.getvalue()
    assert "transport_type=" not in stream.getvalue()


@pytest.mark.parametrize("status", [200, 404])
async def test_request_text_preserves_explicit_business_diagnostics_and_actual_bytes(
    caplog: pytest.LogCaptureFixture, status: int
) -> None:
    caplog.set_level(logging.DEBUG)
    body = b" \xff \n"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, content=body, headers={"Content-Type": "application/xml"}
            )
        )
    ) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="xml")

        def diagnostics(response: httpx.Response) -> dict[str, object]:
            return {
                "response_kind": "xml",
                "semantic_result": "no_match",
                "response_bytes": 999,
                "http_status": 201,
            }

        if status == 200:
            text = await executor.request_text(
                "GET", "https://xml.example.test", stage="fetch", response_diagnostics=diagnostics
            )
            assert text.encode("utf-8") != body
        else:
            with pytest.raises(HttpStatusFailure) as caught:
                await executor.request_text(
                    "GET",
                    "https://xml.example.test",
                    stage="fetch",
                    response_diagnostics=diagnostics,
                )
            assert caught.value.diagnostics["semantic_result"] == "no_match"
            assert caught.value.diagnostics["response_bytes"] == len(body)
    fields = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(fields) == 1
    assert fields[0]["response_kind"] == "xml"
    assert fields[0]["semantic_result"] == "no_match"
    assert fields[0]["response_bytes"] == len(body)
    assert fields[0]["http_status"] == status


async def test_request_text_diagnostic_error_preserves_default_classification(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    def broken(response: httpx.Response) -> dict[str, object]:
        raise RuntimeError("diagnostic only")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="PRIVATE_BODY"))
    ) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="text")
        assert (
            await executor.request_text(
                "GET", "https://text.example.test", stage="fetch", response_diagnostics=broken
            )
            == "PRIVATE_BODY"
        )
    fields = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(fields) == 1
    assert fields[0]["semantic_result"] == "success"
    assert fields[0]["response_kind"] == "text"
