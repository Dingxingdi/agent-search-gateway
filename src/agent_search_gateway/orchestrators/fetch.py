"""URL fetch workflow orchestration and URL-state mutation."""

import logging
import time
from collections.abc import Callable

from ..concurrency import SingleflightGroup
from ..errors import UNAVAILABLE_MESSAGE, ErrorCode, ExecutionFailure, InputFailure
from ..llm.stages import LLMStages, cheap_check
from ..models import URLRecord, URLStatus
from ..observability import elapsed_ms, log_event, target_url_for_log
from ..scheduler.fetch import FetchScheduler
from ..url_normalization import NormalizedURL, normalize_url
from ..url_store import URLStore


class FetchOrchestrator:
    def __init__(
        self,
        *,
        store: URLStore,
        scheduler: FetchScheduler,
        stages: LLMStages,
        logger: logging.Logger | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._stages = stages
        self._logger = logger or logging.getLogger(__name__)
        self._monotonic = monotonic
        self._request_singleflight: SingleflightGroup[tuple[NormalizedURL, str | None], str] = (
            SingleflightGroup()
        )

    async def url_fetch(self, url: str, focus: str | None = None) -> str:
        normalized_url = normalize_url(url)
        normalized_focus = focus.strip() if focus is not None and focus.strip() else None
        key = (normalized_url, normalized_focus)
        focus_present = normalized_focus is not None
        focus_chars = len(normalized_focus or "")
        return await self._request_singleflight.do(
            key,
            lambda: self._serialized_url_fetch(normalized_url, normalized_focus),
            on_leader=lambda: log_event(
                self._logger,
                logging.DEBUG,
                "singleflight_leader",
                url=target_url_for_log(str(normalized_url)),
                focus_present=focus_present,
                focus_chars=focus_chars,
            ),
            on_follower=lambda: log_event(
                self._logger,
                logging.DEBUG,
                "singleflight_joined",
                url=target_url_for_log(str(normalized_url)),
                focus_present=focus_present,
                focus_chars=focus_chars,
            ),
        )

    async def _serialized_url_fetch(
        self,
        normalized_url: NormalizedURL,
        normalized_focus: str | None,
    ) -> str:
        lock_started = self._monotonic()
        async with self._store.serialization(normalized_url):
            log_event(
                self._logger,
                logging.DEBUG,
                "url_lock_acquired",
                url=target_url_for_log(str(normalized_url)),
                wait_ms=elapsed_ms(self._monotonic, lock_started),
            )
            snapshot = self._store.get(normalized_url)
            if snapshot is None:
                self._log_state(normalized_url, "url_not_admitted")
                raise InputFailure(ErrorCode.URL_NOT_ADMITTED, "URL was not admitted by search")
            final = await self._prepare_content(snapshot)
            if final.status is URLStatus.UNAVAILABLE:
                self._log_state(normalized_url, "stored_unavailable")
                return UNAVAILABLE_MESSAGE
            if normalized_focus is None:
                self._log_final(normalized_url, final.content, "content")
                return final.content
            summary = await self._focus_summary(final.content, normalized_focus)
            self._log_final(normalized_url, summary, "focus_summary")
            return summary

    async def _prepare_content(self, snapshot: URLRecord) -> URLRecord:
        url = snapshot.url
        while True:
            match snapshot.status:
                case URLStatus.NEED_JUDGE:
                    snapshot = await self._judge_cached_candidate(snapshot)
                case URLStatus.NEED_FETCH:
                    snapshot = await self._fetch_candidate(url)
                case URLStatus.NEED_CLEAN:
                    cleaned = await self._content_clean(snapshot.raw_content)
                    snapshot = self._store.set_clean_content(url, cleaned)
                    log_event(
                        self._logger,
                        logging.DEBUG,
                        "body_accepted",
                        url=target_url_for_log(str(url)),
                        reason="content_cleaned",
                        content_chars=len(cleaned),
                    )
                case URLStatus.NEED_SAFETY:
                    snapshot = await self._check_safety(snapshot)
                case URLStatus.READY | URLStatus.UNAVAILABLE:
                    return snapshot

    async def _judge_cached_candidate(self, snapshot: URLRecord) -> URLRecord:
        candidate = snapshot.content or snapshot.raw_content
        if not cheap_check(candidate):
            reason = "cheap_check"
        elif not (await self._stages.judge(candidate)).ok:
            reason = "judge_rejected"
        else:
            return self._store.accept_candidate(
                snapshot.url, raw_content=snapshot.raw_content, content=snapshot.content
            )
        rejected = self._store.reject_candidate(snapshot.url)
        self._log_rejected(snapshot.url, reason)
        return rejected

    async def _fetch_candidate(self, url: NormalizedURL) -> URLRecord:
        self._log_state(url, "provider_fetch_required")
        if not self._scheduler.provider_names:
            raise ExecutionFailure(
                ErrorCode.NO_URL_FETCH_PROVIDERS,
                "No URL fetch providers are enabled",
            )
        outcome = await self._scheduler.fetch_until_accepted(url)
        if outcome.kind == "execution_failure":
            cause = outcome.failures[-1] if outcome.failures else None
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                "All URL fetch provider pipelines failed",
            ) from cause
        if outcome.kind == "semantic_failure":
            rejected = self._store.mark_unavailable(url)
            self._log_rejected(url, "fetch_semantic_failure")
            return rejected
        candidate = outcome.candidate
        if candidate is None:
            raise RuntimeError("accepted fetch outcome did not contain a candidate")
        accepted = self._store.accept_candidate(
            url, raw_content=candidate.raw_content, content=candidate.content
        )
        log_event(
            self._logger,
            logging.DEBUG,
            "body_accepted",
            url=target_url_for_log(str(url)),
            reason="provider_fetch",
            raw_chars=len(candidate.raw_content),
            content_chars=len(candidate.content),
        )
        return accepted

    async def _content_clean(self, raw_content: str) -> str:
        try:
            cleaned = await self._stages.content_clean(raw_content)
        except Exception as exc:
            raise ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED,
                "Content-clean LLM stage failed",
            ) from exc
        if not isinstance(cleaned, str) or not cleaned.strip():
            raise ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED, "Content-clean returned invalid text"
            )
        return cleaned

    async def _check_safety(self, snapshot: URLRecord) -> URLRecord:
        try:
            decision = await self._stages.safety(snapshot.content)
        except ExecutionFailure as exc:
            raise ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED,
                "Safety LLM stage failed",
            ) from exc
        if decision.ok:
            return self._store.mark_ready(snapshot.url)
        rejected = self._store.mark_unavailable(snapshot.url)
        self._log_rejected(snapshot.url, "safety_rejected")
        return rejected

    async def _focus_summary(self, content: str, focus: str) -> str:
        try:
            return await self._stages.focus_summary(content, focus)
        except ExecutionFailure as exc:
            raise ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED,
                "Focus-summary LLM stage failed",
            ) from exc

    def _log_state(self, url: NormalizedURL, reason: str) -> None:
        log_event(
            self._logger,
            logging.DEBUG,
            "body_skipped",
            url=target_url_for_log(str(url)),
            reason=reason,
        )

    def _log_rejected(self, url: NormalizedURL, reason: str) -> None:
        log_event(
            self._logger,
            logging.DEBUG,
            "body_rejected",
            url=target_url_for_log(str(url)),
            reason=reason,
        )

    def _log_final(self, url: NormalizedURL, text: str, reason: str) -> None:
        log_event(
            self._logger,
            logging.DEBUG,
            "body_accepted",
            url=target_url_for_log(str(url)),
            reason=reason,
            output_chars=len(text),
        )
