"""Sequential URL-fetch provider scheduler with typed outcomes."""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..concurrency import join_cleanup
from ..credential_pool import CredentialReservation
from ..errors import ErrorCode, ExecutionFailure
from ..llm.stages import LLMStages, cheap_check
from ..models import FetchOutcome
from ..observability import elapsed_ms, log_event, target_url_for_log
from ..providers.contracts import ReservableFetchProvider, URLFetchCandidate, WebAdapter
from ..providers.diagnostics import diagnostic_fields_from_failure, safe_failure_reason
from ..providers.http import seconds_to_ms
from ..rate_limit import SelectionBudget
from ..url_normalization import NormalizedURL


@dataclass(eq=False, slots=True)
class _Candidate:
    provider: ReservableFetchProvider
    budget: SelectionBudget


class FetchScheduler:
    def __init__(
        self,
        providers: Sequence[ReservableFetchProvider],
        stages: LLMStages,
        *,
        logger: logging.Logger | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        self._providers = tuple(providers)
        self._stages = stages
        self._logger = logger or logging.getLogger(__name__)
        self._monotonic = monotonic
        self._request_timeout_seconds = request_timeout_seconds

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(provider.name for provider in self._providers)

    async def fetch_until_accepted(self, url: NormalizedURL) -> FetchOutcome:
        semantic_failure_seen = False
        failures: list[ExecutionFailure] = []
        remaining = [
            _Candidate(provider, SelectionBudget(self._request_timeout_seconds))
            for provider in self._providers
        ]
        while remaining:
            selected = await self._select_available_provider(remaining, failures)
            if selected is None:
                break
            candidate, first = selected
            remaining.remove(candidate)
            provider = candidate.provider
            try:
                log_event(
                    self._logger,
                    logging.DEBUG,
                    "provider_selected",
                    provider=provider.name,
                    url=target_url_for_log(str(url)),
                    candidate_count=len(remaining) + 1,
                )
                outcome = await self._attempt(candidate, first, url)
            finally:
                await first.release()
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
        self, remaining: list[_Candidate], failures: list[ExecutionFailure]
    ) -> tuple[_Candidate, CredentialReservation[WebAdapter]] | None:
        while remaining:
            for candidate in remaining:
                first = await candidate.provider.try_reserve_fetch()
                if first is not None:
                    return candidate, first

            log_event(
                self._logger, logging.DEBUG, "scheduler_waiting", candidate_count=len(remaining)
            )
            started = self._monotonic()
            waiters = {
                candidate: asyncio.create_task(self._wait_available(candidate, started))
                for candidate in remaining
            }
            try:
                await asyncio.wait(waiters.values(), return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waiters.values():
                    if not waiter.done():
                        waiter.cancel()
                await join_cleanup(asyncio.create_task(self._join_waiters(tuple(waiters.values()))))

            for candidate, waiter in waiters.items():
                if waiter.cancelled():
                    continue
                try:
                    waiter.result()
                except ExecutionFailure as failure:
                    remaining.remove(candidate)
                    failures.append(failure)
                    if failure.reason == "rate_limit_wait_timeout":
                        log_event(
                            self._logger,
                            logging.DEBUG,
                            failure.reason,
                            provider=candidate.provider.name,
                            stage="fetch",
                            attempt=1,
                            elapsed_ms=seconds_to_ms(candidate.budget.waited_seconds),
                            reason=failure.reason,
                        )
        return None

    async def _wait_available(self, candidate: _Candidate, started: float) -> None:
        # The coordinator is already blocked while the observer waits for its first loop turn.
        elapsed = self._monotonic() - started
        remaining = candidate.budget.remaining_seconds
        candidate.budget.charge_wait(elapsed)
        if elapsed <= remaining:
            try:
                await candidate.provider.wait_fetch_available(candidate.budget)
                return
            except asyncio.CancelledError:
                # A losing observer may already have crossed its deadline and be joining
                # children. Preserve that timeout while the coordinator joins all observers.
                if not candidate.budget.past_deadline:
                    raise
        raise ExecutionFailure(
            ErrorCode.ALL_PROVIDERS_FAILED,
            "Provider availability wait timed out",
            reason="rate_limit_wait_timeout",
        )

    @staticmethod
    async def _join_waiters(waiters: Sequence[asyncio.Task[None]]) -> None:
        await asyncio.gather(*waiters, return_exceptions=True)

    async def _attempt(
        self,
        selected: _Candidate,
        first: CredentialReservation[WebAdapter],
        url: NormalizedURL,
    ) -> FetchOutcome:
        provider = selected.provider
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
            candidate = await provider.fetch_reserved(url, first, selected.budget)
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
