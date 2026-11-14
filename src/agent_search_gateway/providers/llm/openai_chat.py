"""OpenAI-compatible chat-completions LLM adapter."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence

import httpx

from ...concurrency import CapacityGate
from ...errors import ErrorCode, ExecutionFailure, ProtocolFailure
from ...models import LLMInvocation, RetryPolicy
from ...observability import SecretValue, log_event
from ...retry import retry_async
from ..contracts import ChatMessage
from ..diagnostics import best_effort_diagnostics, diagnostic_fields_from_failure
from ..http import HttpJsonExecutor

_RESERVED_EXTRA_BODY_KEYS = frozenset({"model", "messages"})


def _payload_diagnostics(payload: object) -> Mapping[str, object]:
    if not isinstance(payload, dict):
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    error = payload.get("error")
    if isinstance(error, dict):
        return {
            "category": "provider_response",
            "semantic_result": "error",
            "response_kind": "object",
            "upstream_code": error.get("code"),
            "upstream_type": error.get("type"),
            "upstream_message": error.get("message"),
        }
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return {"semantic_result": "success", "response_kind": "object"}
    return {"category": "invalid_envelope", "semantic_result": "unknown", "response_kind": "object"}


def _response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    return _payload_diagnostics(payload)


class OpenAIChatCompletionsClient:
    def __init__(
        self,
        *,
        name: str,
        api_url: str,
        secret: SecretValue,
        executor: HttpJsonExecutor,
        quota: CapacityGate,
        retry_policy: RetryPolicy,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self.name = name
        self._endpoint = f"{api_url.rstrip('/')}/v1/chat/completions"
        self._secret = secret
        self._executor = executor
        self._quota = quota
        self._retry_policy = retry_policy
        self._sleep = sleep
        self._logger = logger or logging.getLogger(__name__)

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        self._validate_invocation(invocation)

        async def operation() -> str:
            payload = await self._request_once(invocation, messages)
            return self._extract_content(payload)

        async with self._quota.lease():
            result = await retry_async(
                self._retry_policy,
                operation,
                is_retryable=lambda exc: isinstance(exc, ProtocolFailure),
                sleep=self._sleep,
                before_attempt=lambda attempt: self._log_protocol_attempt(
                    invocation,
                    messages,
                    attempt,
                ),
                on_retry=lambda attempt, exc, delay: self._log_protocol_retry(
                    invocation,
                    messages,
                    attempt,
                    exc,
                    delay,
                ),
            )
        self._log_protocol_completed(invocation, messages, output_chars=len(result))
        return result

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]:
        self._validate_invocation(invocation)

        async def operation() -> Mapping[str, object]:
            payload = await self._request_once(invocation, messages)
            content = self._extract_content(payload)
            try:
                decoded = json.loads(content)
            except json.JSONDecodeError as exc:
                raise self._protocol_failure(
                    "content was not valid JSON",
                    diagnostics={
                        "category": "invalid_json",
                        "semantic_result": "unknown",
                        "response_kind": "object",
                    },
                ) from exc
            if not isinstance(decoded, dict):
                raise self._protocol_failure(
                    "JSON content must be an object",
                    diagnostics={
                        "category": "invalid_envelope",
                        "semantic_result": "unknown",
                        "response_kind": "object",
                    },
                )
            return decoded

        async with self._quota.lease():
            result = await retry_async(
                self._retry_policy,
                operation,
                is_retryable=lambda exc: isinstance(exc, ProtocolFailure),
                sleep=self._sleep,
                before_attempt=lambda attempt: self._log_protocol_attempt(
                    invocation,
                    messages,
                    attempt,
                ),
                on_retry=lambda attempt, exc, delay: self._log_protocol_retry(
                    invocation,
                    messages,
                    attempt,
                    exc,
                    delay,
                ),
            )
        output_chars = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        self._log_protocol_completed(invocation, messages, output_chars=output_chars)
        return result

    def _log_protocol_attempt(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
        attempt: int,
    ) -> None:
        message_count, input_chars, extra_body_keys = self._protocol_metadata(invocation, messages)
        log_event(
            self._logger,
            logging.DEBUG,
            "llm_protocol_attempt_started",
            provider=self.name,
            stage="llm",
            model=invocation.model,
            message_count=message_count,
            input_chars=input_chars,
            extra_body_keys=extra_body_keys,
            attempt=attempt,
        )

    def _log_protocol_retry(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
        attempt: int,
        exc: BaseException,
        delay: float,
    ) -> None:
        message_count, input_chars, extra_body_keys = self._protocol_metadata(invocation, messages)
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_retrying",
            provider=self.name,
            stage="llm",
            model=invocation.model,
            message_count=message_count,
            input_chars=input_chars,
            extra_body_keys=extra_body_keys,
            attempt=attempt,
            error_type=type(exc).__name__,
            delay_ms=max(0, int(delay * 1000)),
            reason="protocol_retry",
            next_attempt=attempt + 1,
            retry_scope="protocol",
            exc_info=False,
            **diagnostic_fields_from_failure(exc),
        )

    def _log_protocol_completed(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
        *,
        output_chars: int,
    ) -> None:
        message_count, input_chars, extra_body_keys = self._protocol_metadata(invocation, messages)
        log_event(
            self._logger,
            logging.DEBUG,
            "llm_protocol_completed",
            provider=self.name,
            stage="llm",
            model=invocation.model,
            message_count=message_count,
            input_chars=input_chars,
            extra_body_keys=extra_body_keys,
            output_chars=output_chars,
        )

    @staticmethod
    def _protocol_metadata(
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> tuple[int, int, str]:
        return (
            len(messages),
            sum(len(value) for message in messages for value in message.values()),
            ",".join(sorted(invocation.extra_body)) or "-",
        )

    async def aclose(self) -> None:
        await self._executor.aclose()

    def _validate_invocation(self, invocation: LLMInvocation) -> None:
        if invocation.provider != self.name:
            raise ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED,
                f"LLM invocation provider {invocation.provider} does not match {self.name}",
            )
        collisions = _RESERVED_EXTRA_BODY_KEYS.intersection(invocation.extra_body)
        if collisions:
            raise ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED,
                f"LLM extra_body cannot override: {', '.join(sorted(collisions))}",
            )

    async def _request_once(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> object:
        body: dict[str, object] = {
            "model": invocation.model,
            "messages": [dict(message) for message in messages],
        }
        body.update(invocation.extra_body)
        return await self._executor.request_json(
            "POST",
            self._endpoint,
            stage="llm",
            headers={
                "Authorization": f"Bearer {self._secret.reveal()}",
                "Content-Type": "application/json",
            },
            json_body=body,
            response_diagnostics=_response_diagnostics,
        )

    def _extract_content(self, payload: object) -> str:
        diagnostics = best_effort_diagnostics(lambda: _payload_diagnostics(payload))
        if not isinstance(payload, dict):
            raise self._protocol_failure("response must be an object", diagnostics=diagnostics)
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise self._protocol_failure("response choices are missing", diagnostics=diagnostics)
        first = choices[0]
        if not isinstance(first, dict):
            raise self._protocol_failure("response choice is invalid", diagnostics=diagnostics)
        message = first.get("message")
        if not isinstance(message, dict):
            raise self._protocol_failure("response message is missing", diagnostics=diagnostics)
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise self._protocol_failure("response content is empty", diagnostics=diagnostics)
        return content

    def _protocol_failure(
        self, reason: str, *, diagnostics: Mapping[str, object] | None = None
    ) -> ProtocolFailure:
        return ProtocolFailure(
            ErrorCode.PROTOCOL_ERROR, f"{self.name}/llm: {reason}", diagnostics=diagnostics
        )
