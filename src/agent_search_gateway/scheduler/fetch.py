"""Sequential URL-fetch provider scheduler with typed outcomes."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from ..concurrency import CapacityGate, CapacityLease, ProviderQuotaManager
from ..errors import ErrorCode, ExecutionFailure
from ..llm.stages import LLMStages, cheap_check
from ..models import FetchOutcome
from ..observability import elapsed_ms, log_event, target_url_for_log
from ..providers.contracts import URLFetchCandidate, URLFetchProvider
from ..providers.diagnostics import diagnostic_fields_from_failure, safe_failure_reason
from ..providers.http import seconds_to_ms
from ..rate_limit import RateGate, initial_rate_budget
from ..url_normalization import NormalizedURL


@dataclass(eq=False, slots=True)
class _Candidate:
    provider: URLFetchProvider
    rate: RateGate | None
    remaining_seconds: float
    waited_seconds: float = 0.0


class FetchScheduler:
    def __init__(
        self,
        providers: Sequence[URLFetchProvider],
        quotas: ProviderQuotaManager,
        stages: LLMStages,
        *,
        logger: logging.Logger | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        self._providers = tuple(providers)
        self._quotas = quotas
        self._stages = stages
        self._logger = logger or logging.getLogger(__name__)
        self._monotonic = monotonic
        self._sleep = sleep
        self._request_timeout_seconds = request_timeout_seconds

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(provider.name for provider in self._providers)

    async def fetch_until_accepted(self, url: NormalizedURL) -> FetchOutcome:
        semantic_failure_seen = False
        failures: list[ExecutionFailure] = []
        remaining = [
            _Candidate(
                provider, self._quotas.get_rate("web", provider.name), self._request_timeout_seconds
            )
            for provider in self._providers
        ]
        while remaining:
            selected, expired = await self._select_available_provider(remaining)
            failures.extend(expired)
            if selected is None:
                break
            candidate, lease = selected
            remaining.remove(candidate)
            provider = candidate.provider
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
                    outcome = await self._attempt(candidate, url)
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
        self, remaining: list[_Candidate]
    ) -> tuple[tuple[_Candidate, CapacityLease] | None, list[ExecutionFailure]]:
        failures: list[ExecutionFailure] = []
        while remaining:
            blocked: list[tuple[_Candidate, float]] = []
            full: list[CapacityGate] = []
            for candidate in remaining:
                gate = candidate.rate
                if gate is not None:
                    observed = self._monotonic()
                    delay = gate.next_available_in_seconds()
                    if delay > 0:
                        blocked.append((candidate, observed + delay))
                        continue
                capacity = self._quotas.get_web(candidate.provider.name)
                lease = await capacity.try_lease()
                if lease is not None:
                    return (candidate, lease), failures
                full.append(capacity)

            # Cleanup or another provider can spend the permit we saw at the deadline.
            exhausted = [c for c, _ in blocked if c.remaining_seconds == 0]
            if exhausted:
                for candidate in exhausted:
                    remaining.remove(candidate)
                    failures.append(self._rate_timeout(candidate))
                continue

            log_event(
                self._logger,
                logging.DEBUG,
                "scheduler_waiting",
                candidate_count=len(remaining),
            )
            for candidate, ready_at in blocked:
                log_event(
                    self._logger,
                    logging.DEBUG,
                    "rate_limit_waiting",
                    provider=candidate.provider.name,
                    stage="fetch",
                    attempt=1,
                    estimated_wait_ms=seconds_to_ms(ready_at - self._monotonic()),
                )
            waiters: list[asyncio.Task[None]] = []
            try:
                for capacity in full:
                    waiters.append(asyncio.create_task(capacity.wait_until_available()))
                started = self._monotonic()
                if blocked:
                    earliest = min(
                        min(ready_at, started + c.remaining_seconds) for c, ready_at in blocked
                    )
                    waiters.append(asyncio.create_task(self._wake_at(earliest)))
                done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                woke = self._monotonic()
                for waiter in done:
                    waiter.result()
                for candidate, _ in blocked:
                    failure = self._charge_rate_wait(candidate, started, woke)
                    if failure is not None:
                        remaining.remove(candidate)
                        failures.append(failure)
            finally:
                for waiter in waiters:
                    if not waiter.done():
                        waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
        return None, failures

    async def _wake_at(self, deadline: float) -> None:
        # Creating the timer task and actually starting it can occur on different loop turns.
        delay = deadline - self._monotonic()
        if delay > 0:
            await self._sleep(delay)

    def _charge_rate_wait(
        self, candidate: _Candidate, started: float, woke: float
    ) -> ExecutionFailure | None:
        assert candidate.rate is not None
        deadline = started + candidate.remaining_seconds
        elapsed = woke - started
        candidate.waited_seconds += elapsed
        candidate.remaining_seconds = (
            max(0.0, candidate.remaining_seconds - elapsed) if woke < deadline else 0.0
        )
        if woke < deadline or (
            woke == deadline and candidate.rate.next_available_in_seconds() == 0
        ):
            return None
        return self._rate_timeout(candidate)

    def _rate_timeout(self, candidate: _Candidate) -> ExecutionFailure:
        reason = "rate_limit_wait_timeout"
        name = candidate.provider.name
        log_event(
            self._logger,
            logging.DEBUG,
            reason,
            provider=name,
            stage="fetch",
            attempt=1,
            elapsed_ms=seconds_to_ms(candidate.waited_seconds),
            reason=reason,
        )
        return ExecutionFailure(
            ErrorCode.ALL_PROVIDERS_FAILED, f"{name}/fetch: {reason}", reason=reason
        )

    async def _attempt(
        self,
        selected: _Candidate,
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
            with initial_rate_budget(
                selected.rate,
                remaining_seconds=selected.remaining_seconds,
                waited_seconds=selected.waited_seconds,
            ):
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
