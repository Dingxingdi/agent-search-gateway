"""Shared HTTP execution boundary for provider adapters."""

import asyncio
import logging
import math
import random
import re
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..errors import ErrorCode, ExecutionFailure, ProtocolFailure
from ..input_limits import PreparedInput, PreviewProjector
from ..models import RetryPolicy
from ..observability import (
    elapsed_ms,
    http_endpoint_for_log,
    log_event,
    log_upstream_body_preview,
    make_input_preview_projector,
)
from ..rate_limit import RateGate, RateLimitWaitTimeout, consume_initial_rate_budget
from ..retry import retry_async
from .diagnostics import (
    best_effort_diagnostics,
    diagnostic_fields_from_failure,
    safe_diagnostic_fields,
)


def seconds_to_ms(seconds: float) -> int:
    """Convert finite durations to milliseconds, including very large rate windows."""
    if seconds >= sys.float_info.max / 1000:
        return int(seconds) * 1000
    return max(0, int(seconds * 1000))


def is_retryable_http_status(status_code: int) -> bool:
    """Return whether the shared HTTP policy retries this response status."""
    return status_code in {408, 429} or status_code >= 500


_HTTP_DATE_PATTERN = re.compile(
    r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), [0-9]{2} [A-Za-z]{3} [0-9]{4} "
    r"|(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), "
    r"[0-9]{2}-[A-Za-z]{3}-[0-9]{2} )"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2} (?:GMT|UTC|[+-][0-9]{4})"
    r"|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Za-z]{3} (?: [0-9]|[0-9]{2}) "
    r"[0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}"
)


def _parse_retry_after(value: str | None, *, now: float) -> float | None:
    if value is None:
        return None
    value = value.strip(" \t")
    if value.isascii() and value.isdecimal():
        seconds = float(value)
    else:
        # email.utils also accepts incomplete dates and ignores trailing tokens.
        if _HTTP_DATE_PATTERN.fullmatch(value) is None:
            return None
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            seconds = date.timestamp() - now
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


class _RetryableStatus(Exception):
    def __init__(
        self,
        status_code: int,
        diagnostics: Mapping[str, object],
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(str(status_code))
        self.status_code = status_code
        self.diagnostics = dict(diagnostics)
        self.retry_after_seconds = retry_after_seconds


class HttpStatusFailure(ExecutionFailure):
    """Internal HTTP failure with a machine-readable terminal status code."""

    def __init__(
        self,
        provider_name: str,
        stage: str,
        status_code: int,
        *,
        diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(
            ErrorCode.ALL_PROVIDERS_FAILED,
            f"{provider_name}/{stage}: HTTP status {status_code}",
            diagnostics=diagnostics,
        )
        self.status_code = status_code


class ProviderResponseFailure(ExecutionFailure):
    """Provider-declared failure containing only inspector-approved safe diagnostics."""

    def __init__(
        self,
        provider_name: str,
        stage: str,
        *,
        status_code: int,
        retryable: bool,
        safe_reason: str,
        diagnostics: Mapping[str, object],
    ) -> None:
        super().__init__(
            ErrorCode.ALL_PROVIDERS_FAILED,
            f"{provider_name}/{stage}: provider response failure: {safe_reason}",
            diagnostics=diagnostics,
        )
        self.status_code = status_code
        self.retryable = retryable
        self.safe_reason = safe_reason


RequestHeaders = Mapping[str, str] | Callable[[], Mapping[str, str]]
ResponseDecoder = Callable[[httpx.Response], object]
ResponseInspector = Callable[[httpx.Response], ProviderResponseFailure | None]
ResponseDiagnosticExtractor = Callable[[httpx.Response], Mapping[str, object]]


class HttpJsonExecutor:
    """Execute JSON or text HTTP requests with one retry and logging policy."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        retry_policy: RetryPolicy,
        *,
        provider_name: str,
        rate_gate: RateGate | None = None,
        logger: logging.Logger | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._rate_gate = rate_gate
        self._retry_policy = retry_policy
        self._provider_name = provider_name
        self._logger = logger or logging.getLogger(__name__)
        self._sleep = sleep
        self._monotonic = monotonic

    def input_preview_projector(self) -> PreviewProjector | None:
        """Prepare input previews with the same redactor that will log the transport."""
        return make_input_preview_projector(self._logger)

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: RequestHeaders | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: object | None = None,
        prepared_body: PreparedInput | None = None,
        protocol_attempt: int | None = None,
        response_inspector: ResponseInspector | None = None,
        response_diagnostics: ResponseDiagnosticExtractor | None = None,
        response_decoder: ResponseDecoder | None = None,
        max_response_bytes: int | None = None,
        preview_bodies: bool = True,
        inherit_client_defaults: bool = True,
    ) -> object:
        """Request JSON, optionally limiting successful response bytes before decoding.

        With ``max_response_bytes``, callers must keep the injected client's response
        hooks empty from each attempt's preflight through completion, including in
        request hooks and concurrent users of the client. The preflight rejects
        response hooks present at the start; it cannot prevent later mutation.
        Stable request-only hooks are supported.

        Incremental enforcement requires a streaming transport. Already-buffered
        successful bodies are size-checked before decoding, but allocations made by
        a supplied transport or hook before the executor receives the response cannot
        be undone. This is not an unconditional memory bound on injected client code.
        """
        response, attempt, attempt_started, diagnostics = await self._request_response(
            method,
            url,
            stage=stage,
            headers=headers,
            params=params,
            json_body=json_body,
            prepared_body=prepared_body,
            protocol_attempt=protocol_attempt,
            max_response_bytes=max_response_bytes,
            preview_bodies=preview_bodies,
            inherit_client_defaults=inherit_client_defaults,
            defer_diagnostic=response_decoder is not None,
            response_inspector=response_inspector,
            response_diagnostics=response_diagnostics,
        )
        failure: ExecutionFailure | None
        try:
            if response_decoder is None:
                return response.json()
            decoded = response_decoder(response)
        except ValueError as exc:
            failure = ProtocolFailure(
                ErrorCode.PROTOCOL_ERROR,
                f"{self._provider_name}/{stage}: response was not valid JSON",
                diagnostics={"category": "decode"},
            )
            failure.__cause__ = exc
        except ExecutionFailure as exc:
            failure = exc
        else:
            failure = None

        endpoint = http_endpoint_for_log(url)
        if response_decoder is not None:
            fields = (
                best_effort_diagnostics(lambda: response_diagnostics(response))
                if response_diagnostics is not None
                else {}
            )
            if failure is not None:
                fields.update(safe_diagnostic_fields(failure.diagnostics))
                fields.setdefault(
                    "category",
                    "protocol" if isinstance(failure, ProtocolFailure) else "provider_response",
                )
                if fields.get("semantic_result") in (None, "success", "accepted"):
                    fields["semantic_result"] = "error"
            else:
                fields.setdefault("semantic_result", "success")
            for name in ("status", "http_status", "response_bytes", "content_type"):
                fields.pop(name, None)
            diagnostics = {**fields, **diagnostics}
            self._log_response_diagnostic(
                stage, endpoint, attempt, attempt_started, diagnostics, protocol_attempt
            )
        if failure is not None:
            failure.diagnostics = diagnostics
            self._log_failed(
                stage,
                endpoint,
                attempt,
                attempt_started,
                str(diagnostics.get("category", "decode"))
                if response_decoder is not None
                else "decode",
                protocol_attempt=protocol_attempt,
                diagnostics=diagnostics,
            )
            raise failure
        return decoded

    async def request_text(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: object | None = None,
        prepared_body: PreparedInput | None = None,
        protocol_attempt: int | None = None,
        response_diagnostics: ResponseDiagnosticExtractor | None = None,
    ) -> str:
        def text_diagnostics(response: httpx.Response) -> Mapping[str, object]:
            fields: dict[str, object] = {
                "response_kind": "text",
                "semantic_result": (
                    "error"
                    if not response.is_success
                    else "success"
                    if response.text.strip()
                    else "empty"
                ),
            }
            if response_diagnostics is not None:
                fields.update(best_effort_diagnostics(lambda: response_diagnostics(response)))
            return fields

        response, _, _, _ = await self._request_response(
            method,
            url,
            stage=stage,
            headers=headers,
            params=params,
            json_body=json_body,
            prepared_body=prepared_body,
            protocol_attempt=protocol_attempt,
            response_diagnostics=text_diagnostics,
        )
        return response.text

    async def _request_response(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: RequestHeaders | None,
        params: Mapping[str, Any] | None,
        json_body: object | None,
        prepared_body: PreparedInput | None = None,
        protocol_attempt: int | None = None,
        response_inspector: ResponseInspector | None = None,
        response_diagnostics: ResponseDiagnosticExtractor | None = None,
        max_response_bytes: int | None = None,
        preview_bodies: bool = True,
        inherit_client_defaults: bool = True,
        defer_diagnostic: bool = False,
    ) -> tuple[httpx.Response, int, float, dict[str, object]]:
        if max_response_bytes is not None and (
            type(max_response_bytes) is not int or max_response_bytes <= 0
        ):
            raise ValueError("max_response_bytes must be a positive integer")
        if json_body is not None and prepared_body is not None:
            raise ValueError("json_body and prepared_body are mutually exclusive")
        attempt = 0
        attempt_started = self._monotonic()
        log_endpoint = http_endpoint_for_log(url)
        protocol_fields = (
            {"protocol_attempt": protocol_attempt} if protocol_attempt is not None else {}
        )
        response_fields: dict[str, object] = {}

        def before_attempt(current_attempt: int) -> None:
            nonlocal attempt
            attempt = current_attempt

        def on_retry(current_attempt: int, exc: BaseException, delay: float) -> None:
            delay_ms = seconds_to_ms(delay)
            attempt_elapsed_ms = elapsed_ms(self._monotonic, attempt_started)
            if isinstance(exc, ProviderResponseFailure):
                details = dict(response_fields)
                details.pop("category", None)
                details.update(reason=exc.safe_reason, retryable=exc.retryable)
                log_event(
                    self._logger,
                    logging.DEBUG,
                    "http_retrying",
                    provider=self._provider_name,
                    stage=stage,
                    endpoint=log_endpoint,
                    attempt=current_attempt,
                    **protocol_fields,
                    delay_ms=delay_ms,
                    delay_source="backoff",
                    elapsed_ms=attempt_elapsed_ms,
                    category="provider_header",
                    retry_scheduled=True,
                    exc_info=False,
                    **details,
                )
                return
            if isinstance(exc, _RetryableStatus):
                details = safe_diagnostic_fields(exc.diagnostics)
                details.pop("category", None)
                log_event(
                    self._logger,
                    logging.WARNING,
                    "http_retrying",
                    provider=self._provider_name,
                    stage=stage,
                    endpoint=log_endpoint,
                    attempt=current_attempt,
                    **protocol_fields,
                    delay_ms=delay_ms,
                    delay_source=(
                        "retry_after" if exc.retry_after_seconds is not None else "backoff"
                    ),
                    elapsed_ms=attempt_elapsed_ms,
                    category="status",
                    exc_info=False,
                    **details,
                )
                return
            log_event(
                self._logger,
                logging.WARNING,
                "http_retrying",
                provider=self._provider_name,
                stage=stage,
                endpoint=log_endpoint,
                attempt=current_attempt,
                **protocol_fields,
                delay_ms=delay_ms,
                delay_source="backoff",
                elapsed_ms=attempt_elapsed_ms,
                category="transport",
                transport_type=type(exc).__name__,
                exc_info=False,
            )

        async def operation() -> httpx.Response:
            nonlocal attempt_started, response_fields
            timeout = self._retry_policy.request_timeout_seconds
            build_request = self._client.build_request if inherit_client_defaults else httpx.Request
            auth = httpx.USE_CLIENT_DEFAULT if inherit_client_defaults else None
            request = build_request(
                method,
                url,
                headers=None if callable(headers) else headers,
                params=params,
                json=json_body,
                content=prepared_body.content if prepared_body is not None else None,
                extensions={"timeout": httpx.Timeout(timeout).as_dict()},
            )

            def start_attempt() -> None:
                nonlocal attempt_started
                if callable(headers):
                    request.headers.update(headers())
                if max_response_bytes is not None:
                    # HTTPX runs response hooks before giving us the stream to bound.
                    if self._client.event_hooks.get("response"):
                        raise ValueError("Bounded requests do not support HTTPX response hooks")
                    request.headers["Accept-Encoding"] = "identity"
                if prepared_body is not None:
                    request.headers["Content-Type"] = "application/json"
                log_event(
                    self._logger,
                    logging.DEBUG,
                    "http_attempt_started",
                    provider=self._provider_name,
                    stage=stage,
                    endpoint=log_endpoint,
                    attempt=attempt,
                    exc_info=False,
                    **protocol_fields,
                )
                attempt_started = self._monotonic()

            if self._rate_gate is None:
                start_attempt()
            else:
                await self._wait_for_rate(self._rate_gate, stage, attempt, start_attempt)
            response_fields = {}
            response = None
            bounded_response: httpx.Response | None = None
            read_failure: ProtocolFailure | None = None
            received_bytes = 0
            body_complete = False
            incomplete_category: str | None = None
            try:
                if max_response_bytes is None:
                    response = await self._client.send(request, auth=auth, follow_redirects=False)
                    received_bytes = len(response.content)
                    body_complete = True
                else:
                    try:
                        loop = asyncio.get_running_loop()
                        deadline = loop.time() + timeout
                        async with asyncio.timeout_at(deadline):
                            response = await self._client.send(
                                request, auth=auth, follow_redirects=False, stream=True
                            )
                        bounded_response = await self._read_bounded_response(
                            response, max_response_bytes, stage, deadline
                        )
                        # Synchronous reads and cleanup may never yield to the timeout callback.
                        if bounded_response.is_success and loop.time() >= deadline:
                            raise TimeoutError
                        response = bounded_response
                        body_complete = response.is_success
                    except ProtocolFailure as exc:
                        if response is None:
                            raise
                        read_failure = exc
                    except TimeoutError as exc:
                        raise httpx.ReadTimeout(
                            "Response deadline exceeded", request=request
                        ) from exc
            except httpx.TransportError:
                incomplete_category = "transport"
                raise
            except asyncio.CancelledError:
                incomplete_category = "cancelled"
                raise
            finally:
                transport_finished = self._monotonic()
                if response is not None and response.is_success and max_response_bytes is not None:
                    if bounded_response is not None:
                        received_bytes = len(bounded_response.content)
                    elif read_failure is not None:
                        count = read_failure.diagnostics.get("response_bytes")
                        received_bytes = count if isinstance(count, int) else 0
                    else:
                        received_bytes = response.num_bytes_downloaded
                attempt_elapsed_ms = elapsed_ms(lambda: transport_finished, attempt_started)
                if self._logger.isEnabledFor(logging.DEBUG):
                    with suppress(Exception):
                        log_upstream_body_preview(
                            self._logger,
                            provider=self._provider_name,
                            stage=stage,
                            endpoint=log_endpoint,
                            attempt=attempt,
                            transport="http",
                            protocol_attempt=protocol_attempt,
                            request_body=(lambda: request.content) if preview_bodies else None,
                            prepared_body=prepared_body if preview_bodies else None,
                            request_body_sent_bytes=len(request.content),
                            request_body_original_bytes=(
                                prepared_body.original_bytes if prepared_body is not None else None
                            ),
                            request_input_truncated=(
                                prepared_body.input_truncated
                                if prepared_body is not None
                                else False
                            ),
                            response_body=(lambda: response.content)
                            if preview_bodies and body_complete and response is not None
                            else None,
                            response_body_bytes=received_bytes,
                            response_incomplete=not body_complete,
                        )
                    # Exclude preview work from later failure/retry/decode timings,
                    # while retaining the existing response-processing duration.
                    attempt_started += self._monotonic() - transport_finished
                if response is not None:
                    response_fields = safe_diagnostic_fields(
                        {
                            "status": response.status_code,
                            "http_status": response.status_code,
                            "response_bytes": received_bytes,
                            "content_type": response.headers.get("content-type"),
                        }
                    )
                    if max_response_bytes is not None and incomplete_category is not None:
                        response_fields.update(
                            category=incomplete_category,
                            semantic_result=(
                                "unknown" if incomplete_category == "cancelled" else "error"
                            ),
                        )
                        self._log_response_diagnostic(
                            stage,
                            log_endpoint,
                            attempt,
                            attempt_started,
                            response_fields,
                            protocol_attempt,
                        )
            context: dict[str, object] = {
                **response_fields,
                **protocol_fields,
                "provider": self._provider_name,
                "stage": stage,
                "endpoint": log_endpoint,
                "attempt": attempt,
                "elapsed_ms": attempt_elapsed_ms,
            }
            log_event(
                self._logger, logging.DEBUG, "http_attempt_completed", exc_info=False, **context
            )
            if read_failure is not None:
                response_fields.update(category="protocol", semantic_result="error")
                read_failure.diagnostics = response_fields
                self._log_response_diagnostic(
                    stage, log_endpoint, attempt, attempt_started, response_fields, protocol_attempt
                )
                self._log_failed(
                    stage,
                    log_endpoint,
                    attempt,
                    attempt_started,
                    "protocol",
                    protocol_attempt=protocol_attempt,
                    diagnostics=response_fields,
                )
                raise read_failure
            failure: ProviderResponseFailure | None = None
            try:
                if response_inspector is not None and (
                    max_response_bytes is None or response.is_success
                ):
                    failure = response_inspector(response)
            finally:
                if not (defer_diagnostic and response.is_success and failure is None):
                    extracted: dict[str, object] = {}
                    try:
                        if response_diagnostics is not None:
                            extracted = best_effort_diagnostics(
                                lambda: response_diagnostics(response)
                            )
                    finally:
                        inspector_fields = (
                            self._provider_response_fields(failure) if failure is not None else {}
                        )
                        selected = {**extracted, **inspector_fields}
                        for name in ("status", "http_status", "response_bytes", "content_type"):
                            selected.pop(name, None)
                        response_fields = {**selected, **response_fields}
                        if max_response_bytes is not None and not response.is_success:
                            response_fields.update(category="http_status", semantic_result="error")
                        elif failure is not None:
                            response_fields.update(
                                category="provider_response", semantic_result="error"
                            )
                        elif (
                            not response.is_success
                            and response_fields.get("semantic_result") == "success"
                        ):
                            response_fields.update(category="http_status", semantic_result="error")
                        diagnostic_context = {**response_fields, **context}
                        log_event(
                            self._logger,
                            logging.DEBUG,
                            "provider_response_diagnostic",
                            exc_info=False,
                            **diagnostic_context,
                        )
            if failure is not None:
                raise failure
            if is_retryable_http_status(response.status_code):
                raise _RetryableStatus(
                    response.status_code,
                    response_fields,
                    _parse_retry_after(response.headers.get("Retry-After"), now=time.time()),
                )
            return response

        try:
            response = await retry_async(
                self._retry_policy,
                operation,
                is_retryable=self._is_retryable,
                sleep=self._sleep,
                before_attempt=before_attempt,
                on_retry=on_retry,
                adjust_delay=self._adjust_retry_delay,
            )
        except ProviderResponseFailure as exc:
            terminal = ProviderResponseFailure(
                self._provider_name,
                stage,
                status_code=exc.status_code,
                retryable=exc.retryable,
                safe_reason=exc.safe_reason,
                diagnostics=response_fields,
            )
            terminal.code, terminal.message, terminal.reason = exc.code, exc.message, exc.reason
            terminal.args = exc.args
            self._log_failed(
                stage,
                log_endpoint,
                attempt,
                attempt_started,
                "provider_header",
                protocol_attempt=protocol_attempt,
                provider_failure=terminal,
            )
            raise terminal from exc
        except _RetryableStatus as exc:
            self._log_failed(
                stage,
                log_endpoint,
                attempt,
                attempt_started,
                "status",
                protocol_attempt=protocol_attempt,
                diagnostics=exc.diagnostics,
            )
            raise self._status_failure(stage, exc.status_code, exc.diagnostics) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            self._log_failed(
                stage,
                log_endpoint,
                attempt,
                attempt_started,
                "transport",
                protocol_attempt=protocol_attempt,
                transport_type=type(exc).__name__,
                diagnostics=response_fields,
            )
            failure = self._execution_failure(stage, "HTTP transport failure")
            failure.diagnostics = response_fields
            raise failure from exc

        if response.status_code < 200 or response.status_code >= 300:
            self._log_failed(
                stage,
                log_endpoint,
                attempt,
                attempt_started,
                "status",
                protocol_attempt=protocol_attempt,
                diagnostics=response_fields,
            )
            raise self._status_failure(stage, response.status_code, response_fields)
        return response, attempt, attempt_started, response_fields

    async def _read_bounded_response(
        self, response: httpx.Response, limit: int, stage: str, deadline: float
    ) -> httpx.Response:
        def check_size(received: int) -> None:
            if received > limit:
                raise ProtocolFailure(
                    ErrorCode.PROTOCOL_ERROR,
                    f"{self._provider_name}/{stage}: response exceeded byte limit",
                    diagnostics={"response_bytes": received},
                )

        close_deadline = deadline
        try:
            async with asyncio.timeout_at(deadline):
                if not response.is_success:
                    return response
                encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                if encoding != "identity":
                    raise ProtocolFailure(
                        ErrorCode.PROTOCOL_ERROR,
                        f"{self._provider_name}/{stage}: unsupported content encoding",
                    )
                if response.is_stream_consumed:
                    check_size(len(response.content))
                    return response
                body = bytearray()
                async for chunk in response.aiter_raw():
                    check_size(len(body) + len(chunk))
                    body.extend(chunk)
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=bytes(body),
                    request=response.request,
                )
        except asyncio.CancelledError:
            # Attempt immediate cleanup without delaying the caller's cancellation.
            close_deadline = 0
            raise
        finally:
            # Reuse the remaining attempt budget and preserve any original read failure.
            with suppress(Exception):
                async with asyncio.timeout_at(close_deadline):
                    await response.aclose()

    async def _wait_for_rate(
        self, gate: RateGate, stage: str, attempt: int, start_attempt: Callable[[], None]
    ) -> None:
        context = {"provider": self._provider_name, "stage": stage, "attempt": attempt}
        initial = consume_initial_rate_budget(gate)
        timeout = (
            self._retry_policy.request_timeout_seconds
            if initial is None
            else initial.remaining_seconds
        )
        previous_wait = 0.0 if initial is None else initial.waited_seconds
        estimate = gate.next_available_in_seconds()
        if estimate > 0:
            log_event(
                self._logger,
                logging.DEBUG,
                "rate_limit_waiting",
                estimated_wait_ms=seconds_to_ms(estimate),
                exc_info=False,
                **context,
            )

        def before_reserve(waited: float) -> None:
            log_event(
                self._logger,
                logging.DEBUG,
                "rate_limit_acquired",
                waited_ms=seconds_to_ms(previous_wait + waited),
                exc_info=False,
                **context,
            )
            start_attempt()

        try:
            await gate.permit(timeout_seconds=timeout, before_reserve=before_reserve)
        except RateLimitWaitTimeout as exc:
            reason = "rate_limit_wait_timeout"
            log_event(
                self._logger,
                logging.DEBUG,
                reason,
                elapsed_ms=seconds_to_ms(previous_wait + exc.waited_seconds),
                reason=reason,
                exc_info=False,
                **context,
            )
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                f"{self._provider_name}/{stage}: {reason}",
                reason=reason,
            ) from exc

    def _log_response_diagnostic(
        self,
        stage: str,
        endpoint: str,
        attempt: int,
        started: float,
        diagnostics: Mapping[str, object],
        protocol_attempt: int | None,
    ) -> None:
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_response_diagnostic",
            provider=self._provider_name,
            stage=stage,
            endpoint=endpoint,
            attempt=attempt,
            elapsed_ms=elapsed_ms(self._monotonic, started),
            exc_info=False,
            **diagnostics,
            **({"protocol_attempt": protocol_attempt} if protocol_attempt is not None else {}),
        )

    def _log_failed(
        self,
        stage: str,
        endpoint: str,
        attempt: int,
        started: float,
        category: str,
        *,
        protocol_attempt: int | None = None,
        transport_type: str | None = None,
        provider_failure: ProviderResponseFailure | None = None,
        diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        attempt_elapsed_ms = elapsed_ms(self._monotonic, started)
        details = safe_diagnostic_fields(diagnostics or {})
        details.pop("category", None)
        if provider_failure is not None:
            details.update(self._provider_failure_details(provider_failure))
            details["retry_scheduled"] = False
            details["terminal_reason"] = (
                "exhausted" if provider_failure.retryable else "non_retryable"
            )
        if protocol_attempt is not None:
            details["protocol_attempt"] = protocol_attempt
        if transport_type is not None:
            details["transport_type"] = transport_type
        log_event(
            self._logger,
            logging.DEBUG,
            "http_failed",
            provider=self._provider_name,
            stage=stage,
            endpoint=endpoint,
            attempt=attempt,
            category=category,
            elapsed_ms=attempt_elapsed_ms,
            exc_info=False,
            **details,
        )

    @staticmethod
    def _provider_response_fields(failure: ProviderResponseFailure) -> dict[str, object]:
        details = safe_diagnostic_fields(failure.diagnostics)
        # The existing Bright Data inspector has already selected these legacy scalars.
        status = failure.diagnostics.get("brightdata_status")
        if type(status) is int and 100 <= status <= 599:
            details["brightdata_status"] = status
        code = failure.diagnostics.get("brightdata_error_code")
        if type(code) is str and code in {
            "captcha",
            "timeout",
            "expect_element",
            "expect_body",
            "missing",
            "invalid",
            "unknown",
        }:
            details["brightdata_error_code"] = code
        return details

    @classmethod
    def _provider_failure_details(cls, failure: ProviderResponseFailure) -> dict[str, object]:
        details = diagnostic_fields_from_failure(failure)
        legacy = cls._provider_response_fields(failure)
        for name in ("brightdata_status", "brightdata_error_code"):
            if name in legacy:
                details[name] = legacy[name]
        details.update(
            status=details.get("status", failure.status_code),
            reason=failure.safe_reason,
            retryable=failure.retryable,
        )
        return details

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _adjust_retry_delay(exc: BaseException, delay: float) -> float:
        if isinstance(exc, _RetryableStatus) and exc.retry_after_seconds is not None:
            return exc.retry_after_seconds
        if isinstance(exc, ProviderResponseFailure) and exc.retryable:
            return delay * random.uniform(0.8, 1.0)
        return delay

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        if isinstance(exc, ProviderResponseFailure):
            return exc.retryable
        return isinstance(
            exc,
            _RetryableStatus | httpx.TimeoutException | httpx.TransportError,
        )

    def _status_failure(
        self,
        stage: str,
        status_code: int,
        diagnostics: Mapping[str, object],
    ) -> HttpStatusFailure:
        return HttpStatusFailure(self._provider_name, stage, status_code, diagnostics=diagnostics)

    def _execution_failure(self, stage: str, reason: str) -> ExecutionFailure:
        return ExecutionFailure(
            ErrorCode.ALL_PROVIDERS_FAILED,
            f"{self._provider_name}/{stage}: {reason}",
        )
