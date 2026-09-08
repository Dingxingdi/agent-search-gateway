"""Shared HTTP execution boundary for provider adapters."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from ..errors import ErrorCode, ExecutionFailure, ProtocolFailure
from ..models import RetryPolicy
from ..observability import elapsed_ms, http_endpoint_for_log, log_event
from ..retry import retry_async


class _RetryableStatus(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(str(status_code))
        self.status_code = status_code


class HttpStatusFailure(ExecutionFailure):
    """Internal HTTP failure with a machine-readable terminal status code."""

    def __init__(self, provider_name: str, stage: str, status_code: int) -> None:
        super().__init__(
            ErrorCode.ALL_PROVIDERS_FAILED,
            f"{provider_name}/{stage}: HTTP status {status_code}",
        )
        self.status_code = status_code


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
    ) -> object:
        response, attempt, attempt_started = await self._request_response(
            method,
            url,
            stage=stage,
            headers=headers,
            params=params,
            json_body=json_body,
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
            )
            raise ProtocolFailure(
                ErrorCode.PROTOCOL_ERROR,
                f"{self._provider_name}/{stage}: response was not valid JSON",
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
    ) -> str:
        response, _, _ = await self._request_response(
            method,
            url,
            stage=stage,
            headers=headers,
            params=params,
            json_body=json_body,
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
    ) -> tuple[httpx.Response, int, float]:
        attempt = 0
        attempt_started = self._monotonic()
        log_endpoint = http_endpoint_for_log(url)

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
            if isinstance(exc, _RetryableStatus):
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
                    status=exc.status_code,
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
            response = await self._client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=self._retry_policy.request_timeout_seconds,
            )
            log_event(
                self._logger,
                logging.DEBUG,
                "http_attempt_completed",
                provider=self._provider_name,
                stage=stage,
                endpoint=log_endpoint,
                attempt=attempt,
                status=response.status_code,
                elapsed_ms=elapsed_ms(self._monotonic, attempt_started),
            )
            if response.status_code in {408, 429} or response.status_code >= 500:
                raise _RetryableStatus(response.status_code)
            return response

        try:
            response = await retry_async(
                self._retry_policy,
                operation,
                is_retryable=self._is_retryable,
                sleep=self._sleep,
                before_attempt=before_attempt,
                on_retry=on_retry,
            )
        except _RetryableStatus as exc:
            self._log_failed(
                stage,
                log_endpoint,
                attempt,
                attempt_started,
                "status",
                status=exc.status_code,
            )
            raise self._status_failure(stage, exc.status_code) from exc
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
                status=response.status_code,
            )
            raise self._status_failure(stage, response.status_code)
        return response, attempt, attempt_started

    def _log_failed(
        self,
        stage: str,
        endpoint: str,
        attempt: int,
        started: float,
        category: str,
        *,
        status: int | None = None,
        transport_type: str | None = None,
    ) -> None:
        attempt_elapsed_ms = elapsed_ms(self._monotonic, started)
        details: dict[str, object] = {}
        if status is not None:
            details["status"] = status
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

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        return isinstance(
            exc,
            _RetryableStatus | httpx.TimeoutException | httpx.TransportError,
        )

    def _status_failure(self, stage: str, status_code: int) -> HttpStatusFailure:
        return HttpStatusFailure(self._provider_name, stage, status_code)

    def _execution_failure(self, stage: str, reason: str) -> ExecutionFailure:
        return ExecutionFailure(
            ErrorCode.ALL_PROVIDERS_FAILED,
            f"{self._provider_name}/{stage}: {reason}",
        )
