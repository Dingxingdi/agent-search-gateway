"""Shared HTTP execution boundary for provider adapters."""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from ..errors import ErrorCode, ExecutionFailure, ProtocolFailure
from ..models import RetryPolicy
from ..observability import elapsed_ms, http_endpoint_for_log, log_event
from ..retry import retry_async
from .diagnostics import (
    best_effort_diagnostics,
    diagnostic_fields_from_failure,
    safe_diagnostic_fields,
)


def is_retryable_http_status(status_code: int) -> bool:
    """Return whether the shared HTTP policy retries this response status."""
    return status_code in {408, 429} or status_code >= 500


class _RetryableStatus(Exception):
    def __init__(self, status_code: int, diagnostics: Mapping[str, object]) -> None:
        super().__init__(str(status_code))
        self.status_code = status_code
        self.diagnostics = dict(diagnostics)


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
        logger: logging.Logger | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._retry_policy = retry_policy
        self._provider_name = provider_name
        self._logger = logger or logging.getLogger(__name__)
        self._sleep = sleep
        self._monotonic = monotonic

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: object | None = None,
        response_inspector: ResponseInspector | None = None,
        response_diagnostics: ResponseDiagnosticExtractor | None = None,
    ) -> object:
        response, attempt, attempt_started, diagnostics = await self._request_response(
            method,
            url,
            stage=stage,
            headers=headers,
            params=params,
            json_body=json_body,
            response_inspector=response_inspector,
            response_diagnostics=response_diagnostics,
        )
        try:
            return response.json()
        except ValueError as exc:
            self._log_failed(
                stage,
                http_endpoint_for_log(url),
                attempt,
                attempt_started,
                "decode",
                diagnostics=diagnostics,
            )
            raise ProtocolFailure(
                ErrorCode.PROTOCOL_ERROR,
                f"{self._provider_name}/{stage}: response was not valid JSON",
                diagnostics=diagnostics,
            ) from exc

    async def request_text(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: object | None = None,
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
            response_diagnostics=text_diagnostics,
        )
        return response.text

    async def _request_response(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: Mapping[str, str] | None,
        params: Mapping[str, Any] | None,
        json_body: object | None,
        response_inspector: ResponseInspector | None = None,
        response_diagnostics: ResponseDiagnosticExtractor | None = None,
    ) -> tuple[httpx.Response, int, float, dict[str, object]]:
        attempt = 0
        attempt_started = self._monotonic()
        log_endpoint = http_endpoint_for_log(url)
        response_fields: dict[str, object] = {}

        def before_attempt(current_attempt: int) -> None:
            nonlocal attempt, attempt_started
            attempt = current_attempt
            attempt_started = self._monotonic()
            log_event(
                self._logger,
                logging.DEBUG,
                "http_attempt_started",
                provider=self._provider_name,
                stage=stage,
                endpoint=log_endpoint,
                attempt=attempt,
            )

        def on_retry(current_attempt: int, exc: BaseException, delay: float) -> None:
            delay_ms = max(0, int(delay * 1000))
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
                    delay_ms=delay_ms,
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
                    delay_ms=delay_ms,
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
                delay_ms=delay_ms,
                elapsed_ms=attempt_elapsed_ms,
                category="transport",
                transport_type=type(exc).__name__,
            )

        async def operation() -> httpx.Response:
            nonlocal response_fields
            response = await self._client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=self._retry_policy.request_timeout_seconds,
            )
            response_fields = safe_diagnostic_fields(
                {
                    "status": response.status_code,
                    "http_status": response.status_code,
                    "response_bytes": len(response.content),
                    "content_type": response.headers.get("content-type"),
                }
            )
            context: dict[str, object] = {
                **response_fields,
                "provider": self._provider_name,
                "stage": stage,
                "endpoint": log_endpoint,
                "attempt": attempt,
                "elapsed_ms": elapsed_ms(self._monotonic, attempt_started),
            }
            log_event(
                self._logger, logging.DEBUG, "http_attempt_completed", exc_info=False, **context
            )
            failure: ProviderResponseFailure | None = None
            try:
                if response_inspector is not None:
                    failure = response_inspector(response)
            finally:
                extracted: dict[str, object] = {}
                try:
                    if response_diagnostics is not None:
                        extracted = best_effort_diagnostics(lambda: response_diagnostics(response))
                finally:
                    inspector_fields = (
                        self._provider_response_fields(failure) if failure is not None else {}
                    )
                    selected = {**extracted, **inspector_fields}
                    for name in ("status", "http_status", "response_bytes", "content_type"):
                        selected.pop(name, None)
                    response_fields = {**selected, **response_fields}
                    if failure is not None:
                        response_fields.update(
                            category="provider_response", semantic_result="error"
                        )
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
                raise _RetryableStatus(response.status_code, response_fields)
            return response

        try:
            response = await retry_async(
                self._retry_policy,
                operation,
                is_retryable=self._is_retryable,
                sleep=self._sleep,
                before_attempt=before_attempt,
                on_retry=on_retry,
                adjust_delay=self._adjust_retry_delay if response_inspector is not None else None,
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
                transport_type=type(exc).__name__,
            )
            raise self._execution_failure(stage, "HTTP transport failure") from exc

        if response.status_code < 200 or response.status_code >= 300:
            self._log_failed(
                stage,
                log_endpoint,
                attempt,
                attempt_started,
                "status",
                diagnostics=response_fields,
            )
            raise self._status_failure(stage, response.status_code, response_fields)
        return response, attempt, attempt_started, response_fields

    def _log_failed(
        self,
        stage: str,
        endpoint: str,
        attempt: int,
        started: float,
        category: str,
        *,
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
