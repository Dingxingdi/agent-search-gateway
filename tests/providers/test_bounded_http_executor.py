import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from agent_search_gateway.config import ResolvedRatePolicy, ResolvedRateWindow
from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.input_limits import PreparedInput
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.providers.http import HttpJsonExecutor, HttpStatusFailure
from agent_search_gateway.providers.web.common import JsonRequester
from agent_search_gateway.rate_limit import RateGate, SelectionBudget, initial_rate_budget
from tests.support.http import RecordingHttpExecutor, RecordingJsonExecutor
from tests.support.logging import structured_test_logger


async def test_response_decoder_receives_response_headers_and_content() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-format": "sample"}, content=b"123")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(2, 0, 0, 1), provider_name="test")
        result = await executor.request_json(
            "GET",
            "https://example.test",
            stage="search",
            response_decoder=lambda response: (response.headers["x-format"], response.content),
        )
        assert result == ("sample", b"123")
        assert await executor.request_json("GET", "https://example.test", stage="search") == 123


class RecordingStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.reads = 0
        self.closed = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed += 1


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}])
@pytest.mark.parametrize("overflow", [False, True])
async def test_bounded_response_enforces_actual_byte_limit(
    headers: dict[str, str], overflow: bool
) -> None:
    stream = RecordingStream(b"12", *((b"3", b"unread") if overflow else ()))
    sends = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return httpx.Response(200, headers={**headers, "x-format": "raw"}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(2, 0, 0, 1), provider_name="test")
        call = executor.request_json(
            "GET",
            "https://example.test",
            stage="search",
            max_response_bytes=2,
            response_decoder=lambda r: (r.headers["x-format"], r.content),
        )
        if overflow:
            with pytest.raises(ProtocolFailure):
                await call
        else:
            assert await call == ("raw", b"12")
    assert stream.reads == (2 if overflow else 1)
    assert sends == stream.closed == 1


@pytest.mark.parametrize("failure_type", [httpx.ReadError, httpx.ReadTimeout])
async def test_partial_read_diagnostic_survives_successful_retry(
    failure_type: type[httpx.TransportError],
) -> None:
    partial = b"PRIVATE_PARTIAL"
    failure = failure_type("PRIVATE_FAILURE")
    logger, log = structured_test_logger("tests.bounded.partial-retry")
    calls: list[str] = []

    class FailingStream(RecordingStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield partial
            raise failure

    stream = FailingStream()
    gate = RateGate(ResolvedRatePolicy(windows=(ResolvedRateWindow(2, 60),)))

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append("send")
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=stream if calls.count("send") == 1 else RecordingStream(b"1"),
        )

    def decode(response: httpx.Response) -> object:
        calls.append("decode")
        return response.json()

    def diagnostics(response: httpx.Response) -> dict[str, object]:
        calls.append("diagnostics")
        return {"semantic_result": "success"}

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(2, 0, 0, 1), provider_name="test", logger=logger, rate_gate=gate
        )
        assert (
            await executor.request_json(
                "GET",
                "https://example.test",
                stage="search",
                max_response_bytes=64,
                preview_bodies=False,
                response_decoder=decode,
                response_diagnostics=diagnostics,
            )
            == 1
        )
    assert calls == ["send", "send", "decode", "diagnostics"]
    assert stream.closed == 1
    rendered = log.getvalue()
    response_events = [
        line for line in rendered.splitlines() if "event=provider_response_diagnostic " in line
    ]
    assert len(response_events) == 2
    first, second = response_events
    for field in (
        "attempt=1",
        "status=200",
        "http_status=200",
        f"response_bytes={len(partial)}",
        "content_type=application/json",
        "category=transport",
        "semantic_result=error",
    ):
        assert field in first
    assert "attempt=2" in second and "semantic_result=success" in second
    assert rendered.count("event=http_retrying ") == 1
    assert rendered.count("event=http_attempt_started ") == 2
    assert rendered.count("event=rate_limit_acquired ") == 2
    assert rendered.count("event=upstream_body_preview ") == 2
    assert "event=http_failed " not in rendered
    assert "PRIVATE_" not in rendered


@pytest.mark.parametrize("attempts", [1, 2])
async def test_exhausted_partial_read_retains_safe_response_fields(attempts: int) -> None:
    partial = b"PRIVATE_PARTIAL"
    logger, log = structured_test_logger("tests.bounded.partial-terminal")
    streams: list[RecordingStream] = []
    failures: list[httpx.ReadError] = []

    class FailingStream(RecordingStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield partial
            failure = httpx.ReadError("PRIVATE_FAILURE")
            failures.append(failure)
            raise failure

    def respond(request: httpx.Request) -> httpx.Response:
        stream = FailingStream()
        streams.append(stream)
        return httpx.Response(
            200, headers={"Content-Type": "application/json; charset=utf-8"}, stream=stream
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(attempts, 0, 0, 1), provider_name="test", logger=logger
        )
        with pytest.raises(ExecutionFailure) as caught:
            await executor.request_json(
                "GET",
                "https://example.test",
                stage="search",
                max_response_bytes=64,
                preview_bodies=False,
                response_decoder=lambda r: pytest.fail("incomplete response decoded"),
                response_diagnostics=lambda r: pytest.fail("incomplete response inspected"),
            )
    assert caught.value.__cause__ is failures[-1]
    assert len(streams) == attempts and all(s.closed == 1 for s in streams)
    expected = {
        "status": 200,
        "http_status": 200,
        "response_bytes": len(partial),
        "content_type": "application/json",
        "category": "transport",
        "semantic_result": "error",
    }
    assert caught.value.diagnostics == expected
    rendered = log.getvalue()
    assert rendered.count("event=provider_response_diagnostic ") == attempts
    assert rendered.count("event=http_retrying ") == attempts - 1
    assert rendered.count("event=upstream_body_preview ") == attempts
    (terminal,) = [line for line in rendered.splitlines() if "event=http_failed " in line]
    for name, value in expected.items():
        assert f"{name}={value}" in terminal
    assert "transport_type=ReadError" in terminal
    assert "PRIVATE_" not in rendered and "PRIVATE_" not in str(caught.value)


@pytest.mark.parametrize("phase", ["send", "stream"])
@pytest.mark.parametrize("broken_close", [False, True])
async def test_cancelled_partial_response_has_one_safe_diagnostic(
    phase: str,
    broken_close: bool,
) -> None:
    partial = b"PRIVATE_PARTIAL"
    logger, log = structured_test_logger("tests.bounded.partial-cancel")
    started = asyncio.Event()
    cancellations: list[asyncio.CancelledError] = []
    sends = 0

    async def wait_for_cancellation() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            cancellations.append(exc)
            raise

    class WaitingStream(RecordingStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield partial
            await wait_for_cancellation()

        async def aclose(self) -> None:
            await super().aclose()
            if broken_close:
                raise RuntimeError("PRIVATE_CLOSE_FAILURE")

    stream = WaitingStream()

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        if phase == "send":
            await wait_for_cancellation()
        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(2, 0, 0, 1), provider_name="test", logger=logger
        )
        task = asyncio.create_task(
            executor.request_json(
                "GET",
                "https://example.test",
                stage="search",
                max_response_bytes=64,
                preview_bodies=False,
                response_decoder=lambda r: pytest.fail("incomplete response decoded"),
                response_diagnostics=lambda r: pytest.fail("incomplete response inspected"),
            )
        )
        async with asyncio.timeout(1):
            await started.wait()
            task.cancel("PRIVATE_CANCEL")
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
    assert caught.value is cancellations[0]
    assert sends == 1 and stream.closed == (phase == "stream")
    rendered = log.getvalue()
    diagnostics = [
        line for line in rendered.splitlines() if "event=provider_response_diagnostic " in line
    ]
    assert len(diagnostics) == (phase == "stream")
    if phase == "stream":
        (diagnostic,) = diagnostics
        for field in (
            "status=200",
            "http_status=200",
            f"response_bytes={len(partial)}",
            "content_type=application/json",
            "category=cancelled",
            "semantic_result=unknown",
        ):
            assert field in diagnostic
    assert rendered.count("event=upstream_body_preview ") == 1
    assert "event=http_retrying " not in rendered and "event=http_failed " not in rendered
    assert "PRIVATE_" not in rendered


@pytest.mark.parametrize(
    "failure_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError]
)
@pytest.mark.parametrize("after_partial", [False, True])
async def test_preheader_failure_has_no_response_or_stale_http_fields(
    failure_type: type[httpx.TransportError],
    after_partial: bool,
) -> None:
    logger, log = structured_test_logger("tests.bounded.preheader")
    terminal_error = failure_type("PRIVATE_TLS_OR_TRANSPORT_FAILURE")
    sends = 0

    class FailingStream(RecordingStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"PRIVATE_PARTIAL"
            raise httpx.ReadError("PRIVATE_PARTIAL_FAILURE")

    stream = FailingStream()

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        if after_partial and sends == 1:
            return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=stream)
        raise terminal_error

    attempts = 2 if after_partial else 1
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(attempts, 0, 0, 1), provider_name="test", logger=logger
        )
        with pytest.raises(ExecutionFailure) as caught:
            await executor.request_json(
                "GET",
                "https://example.test",
                stage="search",
                max_response_bytes=64,
                preview_bodies=False,
                response_decoder=lambda r: pytest.fail("incomplete response decoded"),
                response_diagnostics=lambda r: pytest.fail("incomplete response inspected"),
            )
    assert sends == attempts
    assert caught.value.__cause__ is terminal_error and caught.value.diagnostics == {}
    assert stream.closed == after_partial
    rendered = log.getvalue()
    assert rendered.count("event=provider_response_diagnostic ") == after_partial
    assert rendered.count("event=upstream_body_preview ") == attempts
    assert rendered.count("event=http_retrying ") == attempts - 1
    (terminal,) = [line for line in rendered.splitlines() if "event=http_failed " in line]
    assert f"transport_type={failure_type.__name__}" in terminal
    for field in ("status=", "http_status=", "response_bytes=", "content_type="):
        assert field not in terminal
    assert "PRIVATE_" not in rendered


@pytest.mark.parametrize("limit", [0, -1])
async def test_bounded_response_requires_positive_limit(limit: int) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid bound consumed a send")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(2, 0, 0, 1), provider_name="test")
        with pytest.raises(ValueError, match="max_response_bytes"):
            await executor.request_json(
                "GET", "https://example.test", stage="search", max_response_bytes=limit
            )


@pytest.mark.parametrize("status", [401, 402, 403, 429, 503])
async def test_bounded_error_response_skips_body_and_preserves_status_policy(status: int) -> None:
    class SlowBody(RecordingStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            self.reads += 1
            await asyncio.Event().wait()
            yield b"unread"

    stream = SlowBody() if status == 429 else RecordingStream(b"too big for limit")
    delays: list[float] = []
    logger, log = structured_test_logger("tests.bounded.status")
    sends = 0

    async def sleep(delay: float) -> None:
        delays.append(delay)

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        if sends == 1:
            return httpx.Response(status, headers={"Retry-After": "7"}, stream=stream)
        return httpx.Response(200, content=b"1")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(2, 0, 0, 1), provider_name="test", sleep=sleep, logger=logger
        )
        async with asyncio.timeout(0.25):
            call = executor.request_json(
                "GET", "https://example.test", stage="search", max_response_bytes=1
            )
            if status in (429, 503):
                assert await call == 1
                assert sends == 2 and delays == [7]
            else:
                with pytest.raises(HttpStatusFailure) as caught:
                    await call
                assert caught.value.status_code == status
                assert sends == 1 and not delays
    assert stream.reads == 0 and stream.closed == 1
    diagnostic = next(
        line for line in log.getvalue().splitlines() if "event=provider_response_diagnostic" in line
    )
    assert f"http_status={status}" in diagnostic and "response_bytes=0" in diagnostic


@pytest.mark.parametrize(
    "body", [b'{"nested":"PRIVATE_RESPONSE"}', b"data: PRIVATE_EVENT\n\n", b'{"PRIVATE_INVALID"']
)
@pytest.mark.parametrize("prepared", [False, True])
async def test_private_body_preview_omits_nested_payloads(body: bytes, prepared: bool) -> None:
    logger, log = structured_test_logger("tests.bounded.private")
    sent: list[bytes] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        return httpx.Response(200, stream=RecordingStream(body))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0, 0, 1), provider_name="test", logger=logger
        )
        raw = b'{"text":"PRIVATE_PREPARED"}'
        assert (
            await executor.request_json(
                "POST",
                "https://example.test",
                stage="search",
                json_body=None if prepared else {"nested": {"text": "PRIVATE_REQUEST"}},
                prepared_body=PreparedInput(raw, len(raw), False) if prepared else None,
                response_decoder=lambda r: r.content,
                max_response_bytes=128,
                preview_bodies=False,
            )
            == body
        )
    rendered = log.getvalue()
    assert "PRIVATE_" not in rendered
    (preview,) = [line for line in rendered.splitlines() if "event=upstream_body_preview" in line]
    assert "request_body_preview=null" in preview and "response_body_preview=null" in preview
    assert f"request_body_sent_bytes={len(sent[0])}" in preview
    assert f"response_body_bytes={len(body)}" in preview


@pytest.mark.parametrize("bounded", [False, True])
async def test_private_body_preview_retains_input_truncation_metadata(bounded: bool) -> None:
    logger, log = structured_test_logger("tests.bounded.private.metadata")
    raw = b'{"text":"PRIVATE_PREPARED"}'
    original_bytes = len(raw) + 100

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.content == raw
        return httpx.Response(200, content=b"{}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0, 0, 1), provider_name="test", logger=logger
        )
        assert (
            await executor.request_json(
                "POST",
                "https://example.test",
                stage="search",
                prepared_body=PreparedInput(raw, original_bytes, True),
                max_response_bytes=128 if bounded else None,
                preview_bodies=False,
            )
            == {}
        )

    rendered = log.getvalue()
    assert "PRIVATE_" not in rendered
    (preview,) = [line for line in rendered.splitlines() if "event=upstream_body_preview" in line]
    assert "request_body_preview=null" in preview and "response_body_preview=null" in preview
    assert f"request_body_original_bytes={original_bytes}" in preview
    assert f"request_body_sent_bytes={len(raw)}" in preview
    assert "request_input_truncated=true" in preview


@pytest.mark.parametrize("status", [200, 403])
async def test_bounded_request_rejects_response_hooks_before_dispatch(status: int) -> None:
    stream = RecordingStream(b"1", b"2", b"3")
    events: list[str] = []

    async def on_request(request: httpx.Request) -> None:
        events.append("request hook")

    async def buffer_response(response: httpx.Response) -> None:
        events.append("response hook")
        await response.aread()

    def respond(request: httpx.Request) -> httpx.Response:
        events.append("send")
        return httpx.Response(status if request.method == "POST" else 200, stream=stream)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        event_hooks={"request": [on_request], "response": [buffer_response]},
    ) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(2, 0, 0, 1), provider_name="test")
        with pytest.raises(ValueError, match="response hooks"):
            await executor.request_json(
                "POST", "https://example.test", stage="search", max_response_bytes=2
            )
        assert events == [] and stream.reads == 0
        assert await executor.request_json("GET", "https://example.test", stage="search") == 123
    assert events == ["request hook", "send", "response hook"]


@pytest.mark.parametrize(
    "phase,status",
    [
        ("headers", 200),
        ("buffered", 200),
        ("body", 200),
        ("close", 200),
        ("close", 302),
        ("close", 403),
    ],
)
async def test_bounded_request_finishes_synchronously_after_deadline(
    phase: str, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    real_time = loop.time
    offset = 0.0
    monkeypatch.setattr(loop, "time", lambda: real_time() + offset)

    class LateStream(RecordingStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal offset
            if phase == "body":
                offset += 2
            yield b"123"

        async def aclose(self) -> None:
            nonlocal offset
            await super().aclose()
            if phase == "close":
                offset += 2

    stream = LateStream()
    logger, log = structured_test_logger("tests.bounded.sync-deadline")

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal offset
        if phase in ("headers", "buffered"):
            offset += 2
        if phase == "buffered":
            return httpx.Response(status, content=b"123")
        return httpx.Response(status, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0, 0, 1), provider_name="test", logger=logger
        )
        with pytest.raises(ExecutionFailure) as caught:
            await executor.request_json(
                "POST", "https://example.test", stage="search", max_response_bytes=3
            )
    assert stream.closed == (phase != "buffered")
    (preview,) = [
        line for line in log.getvalue().splitlines() if "event=upstream_body_preview " in line
    ]
    assert "response_body_preview=null" in preview
    assert caught.value.diagnostics["http_status"] == status
    if status == 200:
        assert isinstance(caught.value.__cause__, httpx.ReadTimeout)
        assert caught.value.diagnostics["response_bytes"] == 3
        assert "response_body_bytes=3" in preview
    else:
        assert isinstance(caught.value, HttpStatusFailure)
        assert caught.value.status_code == status


@pytest.mark.parametrize("phase", ["send", "stream"])
async def test_bounded_response_deadline_covers_whole_attempt(phase: str) -> None:
    class HeartbeatStream(RecordingStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            while True:
                self.reads += 1
                yield b":"
                await asyncio.sleep(0.001)

    streams: list[HeartbeatStream] = []
    sends = 0
    logger, log = structured_test_logger("tests.bounded.deadline")

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        if phase == "send":
            await asyncio.Event().wait()
        stream = HeartbeatStream()
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(2, 0, 0, 0.03), provider_name="test", logger=logger
        )
        async with asyncio.timeout(1):
            with pytest.raises(ExecutionFailure) as caught:
                await executor.request_json(
                    "GET",
                    "https://example.test",
                    stage="search",
                    max_response_bytes=4096,
                    preview_bodies=False,
                )
    assert isinstance(caught.value.__cause__, httpx.ReadTimeout)
    assert sends == 2
    assert all(s.reads > 0 and s.closed == 1 for s in streams)
    rendered = log.getvalue()
    assert rendered.count("event=upstream_body_preview") == 2
    diagnostics = [
        line for line in rendered.splitlines() if "event=provider_response_diagnostic " in line
    ]
    assert len(diagnostics) == (2 if phase == "stream" else 0)
    if phase == "stream":
        assert caught.value.diagnostics["http_status"] == 200
        received = caught.value.diagnostics["response_bytes"]
        assert isinstance(received, int) and received > 0
        assert all(
            "category=transport" in line
            and "semantic_result=error" in line
            and "http_status=200" in line
            for line in diagnostics
        )
    else:
        assert caught.value.diagnostics == {}


@pytest.mark.parametrize("cancel", [False, True])
async def test_bounded_response_preserves_read_failure_or_cancellation_during_close(
    cancel: bool,
) -> None:
    started = asyncio.Event()

    class BrokenStream(RecordingStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"x"
            started.set()
            if cancel:
                await asyncio.Event().wait()
            raise httpx.ReadError("broken stream")

        async def aclose(self) -> None:
            await super().aclose()
            raise RuntimeError("broken cleanup")

    stream = BrokenStream()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 1), provider_name="test")
        task = asyncio.create_task(
            executor.request_json(
                "GET", "https://example.test", stage="search", max_response_bytes=8
            )
        )
        if cancel:
            async with asyncio.timeout(1):
                await started.wait()
                task.cancel("caller cancelled")
                with pytest.raises(asyncio.CancelledError, match="caller cancelled"):
                    await task
        else:
            with pytest.raises(ExecutionFailure) as caught:
                await task
            assert isinstance(caught.value.__cause__, httpx.ReadError)
    assert stream.closed == 1


@pytest.mark.parametrize("failure", ["deadline", "cancel", "read_error"])
async def test_stalled_close_preserves_read_outcome_within_budget(failure: str) -> None:
    reading = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()
    read_error = httpx.ReadError("read failed")

    class StalledStream(RecordingStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"x"
            reading.set()
            if failure == "read_error":
                raise read_error
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            await super().aclose()
            closing.set()
            await release.wait()

    stream = StalledStream()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as client:
        timeout = 5 if failure == "cancel" else 0.02
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, timeout), provider_name="test")
        task = asyncio.create_task(
            executor.request_json(
                "GET", "https://example.test", stage="search", max_response_bytes=8
            )
        )
        try:
            async with asyncio.timeout(1):
                await reading.wait()
                if failure == "cancel":
                    task.cancel("caller cancelled")
                await closing.wait()
            done, _ = await asyncio.wait({task}, timeout=0.25)
        finally:
            release.set()
            (outcome,) = await asyncio.gather(task, return_exceptions=True)
    assert task in done
    assert stream.closed == 1
    if failure == "cancel":
        assert isinstance(outcome, asyncio.CancelledError)
    else:
        assert isinstance(outcome, ExecutionFailure)
        assert outcome.diagnostics["response_bytes"] == 1
        if failure == "read_error":
            assert outcome.__cause__ is read_error
        else:
            assert isinstance(outcome.__cause__, httpx.ReadTimeout)


async def test_bounded_response_rejects_compression_before_first_chunk() -> None:
    stream = RecordingStream(b"PRIVATE_INVALID_GZIP")
    encodings: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        encodings.append(request.headers["Accept-Encoding"])
        if len(encodings) == 1:
            return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)
        return httpx.Response(200, json=True)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(2, 0, 0, 1), provider_name="test")
        with pytest.raises(ProtocolFailure) as caught:
            await executor.request_json(
                "GET",
                "https://example.test",
                stage="search",
                headers=lambda: {"Accept-Encoding": "gzip"},
                max_response_bytes=128,
            )
        assert "PRIVATE" not in str(caught.value)
        assert (
            await executor.request_json(
                "GET", "https://example.test", stage="search", headers={"Accept-Encoding": "gzip"}
            )
            is True
        )
    assert stream.reads == 0 and stream.closed == 1
    assert encodings == ["identity", "gzip"]


@pytest.mark.parametrize(
    "outcome", ["valid", "invalid_json", "invalid_envelope", "rpc_error", "tool_error"]
)
async def test_custom_decoder_emits_one_post_decode_diagnostic(outcome: str) -> None:
    logger, log = structured_test_logger("tests.bounded.diagnostic")
    order: list[str] = []
    result = object()

    def respond(request: httpx.Request) -> httpx.Response:
        order.append("send")
        return httpx.Response(200, content=b"123")

    def decode(response: httpx.Response) -> object:
        order.append("decode")
        if outcome != "valid":
            failure_type = ProtocolFailure if outcome.startswith("invalid") else ExecutionFailure
            raise failure_type(
                ErrorCode.PROTOCOL_ERROR,
                "PRIVATE_DECODER_DETAIL",
                diagnostics={
                    "category": category,
                    "upstream_code": outcome,
                    "response_bytes": 99,
                    "semantic_result": "error",
                },
            )
        return result

    def diagnostics(response: httpx.Response) -> dict[str, object]:
        order.append("diagnostics")
        return {
            "semantic_result": "success",
            "http_status": 300,
            "response_bytes": 17,
            "content_type": "text/plain",
            "untrusted": "PRIVATE_OTHER",
        }

    category = {"invalid_json": "decode", "invalid_envelope": "invalid_envelope"}.get(
        outcome, "provider_response"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(3, 0, 0, 1), provider_name="test", logger=logger
        )
        call = executor.request_json(
            "GET",
            "https://example.test",
            stage="search",
            response_decoder=decode,
            response_diagnostics=diagnostics,
            preview_bodies=False,
            protocol_attempt=4,
        )
        if outcome == "valid":
            assert await call is result
        else:
            with pytest.raises(ExecutionFailure) as caught:
                await call
            assert caught.value.diagnostics["http_status"] == 200
    assert order == ["send", "decode", "diagnostics"]
    rendered = log.getvalue()
    (diagnostic,) = [
        line for line in rendered.splitlines() if "event=provider_response_diagnostic" in line
    ]
    assert "http_status=200" in diagnostic and "response_bytes=3" in diagnostic
    assert "protocol_attempt=4" in diagnostic and "content_type=" not in diagnostic
    assert "PRIVATE" not in rendered and "event=http_attempt_completed" in rendered
    assert rendered.count("event=http_failed") == (outcome != "valid")
    if outcome == "valid":
        assert "semantic_result=success" in diagnostic
    else:
        assert f"category={category}" in diagnostic and "semantic_result=error" in diagnostic


@pytest.mark.parametrize("encoded,received", [(False, 4), (True, 0)])
@pytest.mark.parametrize("broken_close", [False, True])
async def test_bounded_read_failure_diagnostics_before_decoder(
    encoded: bool, received: int, broken_close: bool
) -> None:
    logger, log = structured_test_logger("tests.bounded.read-failure")

    class ProtocolStream(RecordingStream):
        async def aclose(self) -> None:
            await super().aclose()
            if broken_close:
                raise RuntimeError("PRIVATE_CLOSE_FAILURE")

    stream = ProtocolStream(b"123", b"4", b"unread")
    headers = {"Content-Encoding": "gzip"} if encoded else {}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers=headers, stream=stream)
        )
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(2, 0, 0, 1), provider_name="test", logger=logger
        )
        with pytest.raises(ProtocolFailure) as caught:
            await executor.request_json(
                "GET",
                "https://example.test",
                stage="search",
                response_decoder=lambda r: pytest.fail("decoder reached"),
                response_diagnostics=lambda r: pytest.fail("incomplete response inspected"),
                preview_bodies=False,
                max_response_bytes=3,
            )
    assert caught.value.diagnostics["response_bytes"] == received
    rendered = log.getvalue()
    for event in (
        "http_attempt_completed",
        "http_failed",
        "provider_response_diagnostic",
        "upstream_body_preview",
    ):
        (line,) = [line for line in rendered.splitlines() if f"event={event} " in line]
        if event == "upstream_body_preview":
            assert f"response_body_bytes={received}" in line
            assert f"response_preview_truncated={str(received > 0).lower()}" in line
        else:
            assert "http_status=200" in line and f"response_bytes={received}" in line
    assert "semantic_result=error" in rendered and "semantic_result=success" not in rendered


@pytest.mark.parametrize("kind", ["json", "http"])
async def test_recording_requesters_accept_options_with_predecoded_results(kind: str) -> None:
    result = object()
    requester: JsonRequester = (
        RecordingJsonExecutor([result])
        if kind == "json"
        else RecordingHttpExecutor(json_responses=[result])
    )
    assert (
        await requester.request_json(
            "GET",
            "https://example.test",
            stage="search",
            response_decoder=lambda r: pytest.fail("recording responses are already decoded"),
            max_response_bytes=2,
            preview_bodies=False,
        )
        is result
    )


async def test_parallel_http_branches_take_separate_permits_without_spending_owner_budget() -> None:
    from tests.support.controlled import ManualClock

    clock = ManualClock()
    gate = RateGate(
        ResolvedRatePolicy(min_interval_seconds=1), monotonic=clock.monotonic, sleep=clock.sleep
    )
    sends: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sends.append(clock.now)
        return httpx.Response(200, stream=RecordingStream(b"1"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0, 0, 2), provider_name="example", rate_gate=gate
        )

        async def branch() -> object:
            return await executor.request_json(
                "GET", "https://example.test", stage="search", max_response_bytes=1
            )

        with initial_rate_budget(gate, SelectionBudget(0)):
            async with asyncio.timeout(2), asyncio.TaskGroup() as group:
                first = group.create_task(branch())
                second = group.create_task(branch())
                while not clock.sleepers:
                    await asyncio.sleep(0)
                assert sends == [0]
                await clock.advance_to(1)
            assert (first.result(), second.result()) == (1, 1)
            async with asyncio.timeout(2):
                with pytest.raises(ExecutionFailure) as caught:
                    await branch()
    assert caught.value.reason == "rate_limit_wait_timeout"
    assert sends == [0, 1]
    assert gate.next_available_in_seconds() == 1


async def test_bounded_attempt_deadline_starts_after_rate_permit() -> None:
    gate = RateGate(ResolvedRatePolicy(windows=(ResolvedRateWindow(1, 0.06),)))
    await gate.permit(timeout_seconds=1)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=RecordingStream(b"1"))
        )
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0, 0, 0.01), provider_name="test", rate_gate=gate
        )
        with initial_rate_budget(gate, SelectionBudget(1, 0)):
            assert (
                await executor.request_json(
                    "GET", "https://example.test", stage="search", max_response_bytes=1
                )
                == 1
            )
