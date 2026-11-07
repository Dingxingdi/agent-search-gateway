"""Resolved prompt-level LLM stages."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TypeVar

from ..errors import ErrorCode, ExecutionFailure
from ..models import AgentInvocation, LLMInvocation, StageDecision
from ..observability import elapsed_ms, log_event
from ..providers.contracts import AgentTextRunner, ChatMessage, LLMClient
from .prompts import (
    content_clean_messages,
    focus_summary_messages,
    judge_messages,
    llm_paper_search_messages,
    llm_search_messages,
    safety_messages,
)

T = TypeVar("T")


def cheap_check(candidate: str) -> bool:
    return bool(candidate.strip())


class LLMStages:
    def __init__(
        self,
        clients: Mapping[str, LLMClient],
        *,
        judge: LLMInvocation,
        safety: LLMInvocation,
        content_clean: LLMInvocation,
        focus_summary: LLMInvocation,
        agent_runners: Mapping[str, AgentTextRunner] | None = None,
        logger: logging.Logger | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clients = dict(clients)
        self._agent_runners = dict(agent_runners or {})
        self._judge = judge
        self._safety = safety
        self._content_clean = content_clean
        self._focus_summary = focus_summary
        self._logger = logger or logging.getLogger(__name__)
        self._monotonic = monotonic

    @property
    def judge_provider(self) -> str:
        return self._judge.provider

    async def judge(self, candidate: str) -> StageDecision:
        return await self._run_decision_stage(
            self._judge,
            "judge",
            input_chars=len(candidate),
            operation=lambda: self._client(self._judge.provider).complete_json(
                self._judge,
                judge_messages(candidate),
            ),
        )

    async def safety(self, content: str) -> StageDecision:
        return await self._run_decision_stage(
            self._safety,
            "safety",
            input_chars=len(content),
            operation=lambda: self._client(self._safety.provider).complete_json(
                self._safety,
                safety_messages(content),
            ),
        )

    async def content_clean(self, raw_content: str) -> str:
        return await self._run_text_stage(
            self._content_clean,
            "content_clean",
            input_chars=len(raw_content),
            operation=lambda: self._client(self._content_clean.provider).complete_text(
                self._content_clean,
                content_clean_messages(raw_content),
            ),
        )

    async def focus_summary(self, content: str, focus: str) -> str:
        normalized_focus = focus.strip()
        if not normalized_focus:
            raise ExecutionFailure(ErrorCode.LLM_STAGE_FAILED, "focus must be non-empty")
        return await self._run_text_stage(
            self._focus_summary,
            "focus_summary",
            input_chars=len(content),
            focus_chars=len(normalized_focus),
            operation=lambda: self._client(self._focus_summary.provider).complete_text(
                self._focus_summary,
                focus_summary_messages(content, normalized_focus),
            ),
        )

    async def llm_search_markdown(self, invocation: LLMInvocation, prompt: str) -> str:
        return await self._run_text_stage(
            invocation,
            "llm_search",
            input_chars=len(prompt),
            operation=lambda: self._client(invocation.provider).complete_text(
                invocation,
                llm_search_messages(prompt),
            ),
        )

    async def llm_paper_search_markdown(
        self,
        invocation: LLMInvocation,
        prompt: str,
    ) -> str:
        return await self._run_text_stage(
            invocation,
            "llm_paper_search",
            input_chars=len(prompt),
            operation=lambda: self._client(invocation.provider).complete_text(
                invocation,
                llm_paper_search_messages(prompt),
            ),
        )

    async def agent_search_markdown(self, invocation: AgentInvocation, prompt: str) -> str:
        return await self._agent_markdown(invocation, llm_search_messages(prompt))

    async def agent_paper_search_markdown(self, invocation: AgentInvocation, prompt: str) -> str:
        return await self._agent_markdown(invocation, llm_paper_search_messages(prompt))

    async def _agent_markdown(
        self, invocation: AgentInvocation, messages: Sequence[ChatMessage]
    ) -> str:
        runner = self._agent_runners.get(invocation.provider)
        if runner is None:
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                "Agent provider is not initialized",
                reason="execution_failure",
            )
        text = await runner(invocation.model, messages)
        if not isinstance(text, str) or not text.strip():
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED, "Agent returned empty text", reason="empty_response"
            )
        return text.strip()

    async def _run_decision_stage(
        self,
        invocation: LLMInvocation,
        stage: str,
        *,
        input_chars: int,
        operation: Callable[[], Awaitable[Mapping[str, object]]],
    ) -> StageDecision:
        started = self._stage_started(invocation, stage, input_chars=input_chars)
        try:
            decision = self._parse_decision(await operation())
        except asyncio.CancelledError:
            self._stage_cancelled(invocation, stage, started)
            raise
        except Exception as exc:
            self._stage_failed(invocation, stage, started, exc)
            raise
        log_event(
            self._logger,
            logging.DEBUG,
            "llm_stage_completed",
            provider=invocation.provider,
            stage=stage,
            model=invocation.model,
            ok=decision.ok,
            reason_present=bool(decision.reason),
            elapsed_ms=elapsed_ms(self._monotonic, started),
        )
        return decision

    async def _run_text_stage(
        self,
        invocation: LLMInvocation,
        stage: str,
        *,
        input_chars: int,
        operation: Callable[[], Awaitable[str]],
        focus_chars: int = 0,
    ) -> str:
        started = self._stage_started(
            invocation,
            stage,
            input_chars=input_chars,
            focus_chars=focus_chars,
        )
        try:
            text = self._require_non_empty(await operation(), stage.replace("_", "-"))
        except asyncio.CancelledError:
            self._stage_cancelled(invocation, stage, started)
            raise
        except Exception as exc:
            self._stage_failed(invocation, stage, started, exc)
            raise
        log_event(
            self._logger,
            logging.DEBUG,
            "llm_stage_completed",
            provider=invocation.provider,
            stage=stage,
            model=invocation.model,
            output_chars=len(text),
            elapsed_ms=elapsed_ms(self._monotonic, started),
        )
        return text

    def _stage_started(
        self,
        invocation: LLMInvocation,
        stage: str,
        *,
        input_chars: int,
        focus_chars: int = 0,
    ) -> float:
        started = self._monotonic()
        log_event(
            self._logger,
            logging.DEBUG,
            "llm_stage_started",
            provider=invocation.provider,
            stage=stage,
            model=invocation.model,
            input_chars=input_chars,
            focus_present=focus_chars > 0,
            focus_chars=focus_chars,
        )
        return started

    def _stage_failed(
        self,
        invocation: LLMInvocation,
        stage: str,
        started: float,
        exc: Exception,
    ) -> None:
        log_event(
            self._logger,
            logging.DEBUG,
            "llm_stage_failed",
            provider=invocation.provider,
            stage=stage,
            model=invocation.model,
            error_type=type(exc).__name__,
            elapsed_ms=elapsed_ms(self._monotonic, started),
        )

    def _stage_cancelled(self, invocation: LLMInvocation, stage: str, started: float) -> None:
        log_event(
            self._logger,
            logging.DEBUG,
            "llm_stage_cancelled",
            provider=invocation.provider,
            stage=stage,
            model=invocation.model,
            elapsed_ms=elapsed_ms(self._monotonic, started),
        )

    def _client(self, provider: str) -> LLMClient:
        client = self._clients.get(provider)
        if client is None:
            raise ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED,
                f"LLM provider is not initialized: {provider}",
            )
        return client

    @staticmethod
    def _require_non_empty(text: str, stage: str) -> str:
        normalized = text.strip()
        if not normalized:
            raise ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED,
                f"{stage} returned empty text",
            )
        return normalized

    @staticmethod
    def _parse_decision(payload: Mapping[str, object]) -> StageDecision:
        ok = payload.get("ok")
        if not isinstance(ok, bool):
            raise ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED,
                "LLM decision response requires boolean ok",
            )
        reason = payload.get("reason", "")
        if not isinstance(reason, str):
            raise ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED,
                "LLM decision reason must be a string",
            )
        return StageDecision(ok=ok, reason=reason.strip())
