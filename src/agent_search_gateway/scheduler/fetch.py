"""Sequential URL-fetch provider scheduler with typed outcomes."""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence

from ..concurrency import CapacityLease, ProviderQuotaManager
from ..errors import ErrorCode, ExecutionFailure
from ..llm.stages import LLMStages, cheap_check
from ..models import FetchOutcome
from ..observability import elapsed_ms, log_event, target_url_for_log
from ..providers.contracts import URLFetchCandidate, URLFetchProvider
from ..providers.diagnostics import diagnostic_fields_from_failure, safe_failure_reason
from ..url_normalization import NormalizedURL


class FetchScheduler:
    def __init__(
        self,
        providers: Sequence[URLFetchProvider],
        quotas: ProviderQuotaManager,
        stages: LLMStages,
        *,
        logger: logging.Logger | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._providers = tuple(providers)
        self._quotas = quotas
        self._stages = stages
        self._logger = logger or logging.getLogger(__name__)
        self._monotonic = monotonic

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(provider.name for provider in self._providers)

    async def fetch_until_accepted(self, url: NormalizedURL) -> FetchOutcome:
        semantic_failure_seen = False
        failures: list[ExecutionFailure] = []
        remaining = list(self._providers)
        while remaining:
            provider, lease = await self._select_available_provider(remaining)
            remaining.remove(provider)
            log_event(
                self._logger,
                logging.DEBUG,
                "provider_selected",
                provider=provider.name,
                url=target_url_for_log(str(url)),
                candidate_count=len(remaining) + 1,
            )
            try:
                async with lease:
                    outcome = await self._attempt(provider, url)
            except asyncio.CancelledError:
                raise
            if outcome.kind == "accepted":
                return FetchOutcome("accepted", outcome.candidate, tuple(failures))
            log_event(
                self._logger,
                logging.DEBUG,
                "provider_fallback",
                provider=provider.name,
                url=target_url_for_log(str(url)),
                outcome=outcome.kind,
                remaining=len(remaining),
            )
            if outcome.kind == "semantic_failure":
                semantic_failure_seen = True
            else:
                failures.extend(outcome.failures)

        if semantic_failure_seen:
            return FetchOutcome("semantic_failure", failures=tuple(failures))
        return FetchOutcome("execution_failure", failures=tuple(failures))

    async def _select_available_provider(
        self,
        remaining: list[URLFetchProvider],
    ) -> tuple[URLFetchProvider, CapacityLease]:
        while True:
            for provider in remaining:
                lease = await self._quotas.get_web(provider.name).try_lease()
                if lease is not None:
                    return provider, lease
            log_event(
                self._logger,
                logging.DEBUG,
                "scheduler_waiting",
                candidate_count=len(remaining),
            )
            await self._quotas.wait_until_any_web_available(
                tuple(provider.name for provider in remaining)
            )

    async def _attempt(
        self,
        provider: URLFetchProvider,
        url: NormalizedURL,
    ) -> FetchOutcome:
        started = self._monotonic()
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_started",
            provider=provider.name,
            stage="fetch",
            url=target_url_for_log(str(url)),
        )
        try:
            candidate = await provider.fetch(url)
            self._validate_candidate(candidate)
        except asyncio.CancelledError as exc:
            self._log_provider_failed(provider.name, url, started, exc)
            raise
        except ExecutionFailure as exc:
            self._log_provider_failed(provider.name, url, started, exc)
            return FetchOutcome("execution_failure", failures=(exc,))
        except Exception as exc:
            self._log_provider_failed(provider.name, url, started, exc)
            failure = ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                f"Fetch provider {provider.name} returned invalid data",
            )
            return FetchOutcome("execution_failure", failures=(failure,))

        try:
            log_event(
                self._logger,
                logging.DEBUG,
                "candidate_accepted",
                provider=provider.name,
                url=target_url_for_log(str(url)),
                raw_chars=len(candidate.raw_content),
                content_chars=len(candidate.content),
            )
            validation_candidate = candidate.content or candidate.raw_content
            if not cheap_check(validation_candidate):
                self._log_body_rejected(provider.name, url, "cheap_check")
                self._log_provider_completed(provider.name, url, started, "semantic_failure")
                return FetchOutcome("semantic_failure")
        except (asyncio.CancelledError, Exception) as exc:
            self._log_provider_failed(provider.name, url, started, exc)
            raise

        try:
            decision = await self._stages.judge(validation_candidate)
        except asyncio.CancelledError as exc:
            self._log_provider_failed(
                provider.name,
                url,
                started,
                exc,
                failed_stage="judge",
                dependency=self._stages.judge_provider,
            )
            raise
        except ExecutionFailure as exc:
            failure = ExecutionFailure(
                exc.code,
                exc.message,
                reason=exc.reason or "judge_execution_failed",
                diagnostics=diagnostic_fields_from_failure(exc),
            )
            self._log_provider_failed(
                provider.name,
                url,
                started,
                failure,
                failed_stage="judge",
                dependency=self._stages.judge_provider,
            )
            raise failure from exc
        except Exception as exc:
            failure = ExecutionFailure(
                ErrorCode.LLM_STAGE_FAILED,
                "Judge LLM stage failed",
                reason="judge_execution_failed",
            )
            self._log_provider_failed(
                provider.name,
                url,
                started,
                failure,
                failed_stage="judge",
                dependency=self._stages.judge_provider,
            )
            raise failure from exc

        if not decision.ok:
            self._log_body_rejected(provider.name, url, "judge_rejected")
            self._log_provider_completed(provider.name, url, started, "semantic_failure")
            return FetchOutcome("semantic_failure")
        log_event(
            self._logger,
            logging.DEBUG,
            "body_accepted",
            provider=provider.name,
            url=target_url_for_log(str(url)),
            raw_chars=len(candidate.raw_content),
            content_chars=len(candidate.content),
        )
        self._log_provider_completed(provider.name, url, started, "accepted")
        return FetchOutcome("accepted", candidate)

    def _log_body_rejected(
        self,
        provider: str,
        url: NormalizedURL,
        reason: str,
    ) -> None:
        log_event(
            self._logger,
            logging.DEBUG,
            "body_rejected",
            provider=provider,
            url=target_url_for_log(str(url)),
            reason=reason,
        )

    def _log_provider_completed(
        self,
        provider: str,
        url: NormalizedURL,
        started: float,
        outcome: str,
    ) -> None:
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_completed",
            provider=provider,
            stage="fetch",
            url=target_url_for_log(str(url)),
            outcome=outcome,
            elapsed_ms=elapsed_ms(self._monotonic, started),
        )

    def _log_provider_failed(
        self,
        provider: str,
        url: NormalizedURL,
        started: float,
        exc: BaseException,
        *,
        failed_stage: str | None = None,
        dependency: str | None = None,
    ) -> None:
        fields = diagnostic_fields_from_failure(exc)
        if failed_stage is not None:
            fields.update(failed_stage=failed_stage, dependency=dependency or "-")
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_failed",
            provider=provider,
            stage="fetch",
            url=target_url_for_log(str(url)),
            error_type=type(exc).__name__,
            elapsed_ms=elapsed_ms(self._monotonic, started),
            reason=safe_failure_reason(exc),
            exc_info=False,
            **fields,
        )

    @staticmethod
    def _validate_candidate(candidate: URLFetchCandidate) -> None:
        if not isinstance(candidate, URLFetchCandidate):
            raise TypeError("fetch candidate has invalid type")
        if not isinstance(candidate.raw_content, str) or not isinstance(candidate.content, str):
            raise TypeError("fetch candidate fields must be strings")
        if candidate.raw_content == "":
            raise ValueError("fetch candidate raw_content must be non-empty")
