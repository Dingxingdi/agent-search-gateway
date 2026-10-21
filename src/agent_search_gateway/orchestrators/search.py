"""Keyword and LLM search workflow orchestration."""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..academic.aggregator import PaperAggregator
from ..concurrency import ProviderQuotaManager
from ..errors import ErrorCode, ExecutionFailure, InputFailure
from ..llm.stages import LLMStages, cheap_check
from ..llm_search_parser import parse_search_markdown
from ..models import LLMInvocation, LLMSearchScope, PaperRecord, SearchRecord
from ..observability import elapsed_ms, log_event, target_url_for_log
from ..paper_search_parser import parse_paper_markdown
from ..providers.contracts import (
    KeywordSearchHit,
    KeywordSearchProvider,
    OAResolver,
    PaperSearchHit,
)
from ..request_ids import validate_request_id
from ..result_writer import ResultWriter
from ..url_normalization import NormalizedURL, normalize_url
from ..url_store import URLStore
from .paper import finalize_paper_hits


@dataclass(frozen=True, slots=True)
class _StagedKeyword:
    url: NormalizedURL
    abstract: str
    provider: str
    raw_content: str = ""
    content: str = ""


class SearchOrchestrator:
    def __init__(
        self,
        *,
        keyword_providers: Sequence[KeywordSearchProvider],
        llm_invocations: Sequence[LLMInvocation],
        quotas: ProviderQuotaManager,
        stages: LLMStages,
        store: URLStore,
        result_writer: ResultWriter,
        paper_aggregator: PaperAggregator | None = None,
        paper_resolver: OAResolver | None = None,
        logger: logging.Logger | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._keyword_providers = tuple(keyword_providers)
        self._llm_invocations = tuple(llm_invocations)
        self.quotas = quotas
        self._stages = stages
        self._store = store
        self._result_writer = result_writer
        self._paper_aggregator = paper_aggregator or PaperAggregator(
            tuple(f"llm:{invocation.provider}" for invocation in self._llm_invocations)
        )
        self._paper_resolver = paper_resolver
        self._logger = logger or logging.getLogger(__name__)
        self._monotonic = monotonic

    async def keyword_search(
        self, query: str, *, request_id: str, provider: str | None = None
    ) -> str:
        validate_request_id(request_id)
        normalized_query = query.strip()
        if not normalized_query:
            raise InputFailure(ErrorCode.EMPTY_QUERY, "Query must not be empty")
        if not self._keyword_providers:
            raise ExecutionFailure(
                ErrorCode.NO_KEYWORD_SEARCH_PROVIDERS,
                "No keyword search providers are enabled",
            )

        selected = self._keyword_providers
        if provider is not None:
            if not provider.strip():
                raise InputFailure(ErrorCode.BAD_REQUEST, "Provider must not be empty")
            selected = tuple(candidate for candidate in selected if candidate.name == provider)
            if not selected:
                raise InputFailure(
                    ErrorCode.BAD_REQUEST,
                    f"No enabled keyword-search provider matches '{provider}'",
                )

        outcomes = await asyncio.gather(
            *(self._run_keyword_pipeline(candidate, normalized_query) for candidate in selected),
            return_exceptions=True,
        )
        completed = [outcome for outcome in outcomes if isinstance(outcome, list)]
        if not completed:
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                "All keyword search provider pipelines failed",
            )

        ordered_urls: list[NormalizedURL] = []
        seen: set[NormalizedURL] = set()
        for outcome in outcomes:
            if not isinstance(outcome, list):
                continue
            for staged in outcome:
                self._store.admit(
                    staged.url,
                    staged.abstract,
                    raw_content=staged.raw_content,
                    content=staged.content,
                )
                if staged.url not in seen:
                    seen.add(staged.url)
                    ordered_urls.append(staged.url)
                else:
                    log_event(
                        self._logger,
                        logging.DEBUG,
                        "candidate_rejected",
                        provider=staged.provider,
                        url=target_url_for_log(str(staged.url)),
                        reason="duplicate",
                    )

        records = [self._record_from_store(url) for url in ordered_urls]
        path = self._result_writer.write_results("keyword", records, request_id=request_id)
        log_event(
            self._logger,
            logging.DEBUG,
            "results_written",
            kind="keyword",
            path=str(path),
            results=len(records),
        )
        return str(path)

    def _select_llm_invocations(
        self, *, provider: str | None, model: str | None
    ) -> tuple[LLMInvocation, ...]:
        """Select from the search namespace without mutating configured invocations."""
        invocations = self._llm_invocations
        if not invocations:
            raise ExecutionFailure(
                ErrorCode.NO_LLM_SEARCH_PROVIDERS,
                "No LLM search providers are configured",
            )
        if provider is not None and not provider.strip():
            raise InputFailure(ErrorCode.BAD_REQUEST, "Provider must not be empty")
        if model is not None and not model.strip():
            raise InputFailure(ErrorCode.BAD_REQUEST, "Model must not be empty")
        if provider is None and model is None:
            return invocations

        provider_matches = (
            invocations
            if provider is None
            else tuple(item for item in invocations if item.provider == provider)
        )
        model_matches = (
            invocations
            if model is None
            else tuple(item for item in invocations if item.model == model)
        )
        if not provider_matches and not model_matches:
            raise InputFailure(
                ErrorCode.BAD_REQUEST,
                f"No LLM search invocation matches provider '{provider}' or model '{model}'",
            )
        if not provider_matches:
            raise InputFailure(
                ErrorCode.BAD_REQUEST,
                f"No LLM search invocation matches provider '{provider}'",
            )
        if not model_matches:
            raise InputFailure(
                ErrorCode.BAD_REQUEST,
                f"No LLM search invocation matches model '{model}'",
            )
        selected = (
            provider_matches
            if model is None
            else tuple(item for item in provider_matches if item.model == model)
        )
        if not selected:
            raise InputFailure(
                ErrorCode.BAD_REQUEST,
                f"No LLM search invocation matches provider '{provider}' with model '{model}'",
            )
        return selected

    async def llm_search(
        self,
        prompt: str,
        *,
        request_id: str,
        scope: LLMSearchScope = "web",
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        validate_request_id(request_id)
        normalized_prompt = prompt.strip()
        if not normalized_prompt:
            raise InputFailure(ErrorCode.EMPTY_QUERY, "Prompt must not be empty")
        if scope not in {"web", "paper", "all"}:
            raise InputFailure(ErrorCode.BAD_REQUEST, "LLM search scope is invalid")
        selected = self._select_llm_invocations(provider=provider, model=model)

        if scope == "web":
            web_records = await self._llm_web_records(normalized_prompt, invocations=selected)
            path = self._result_writer.write_results("llm", web_records, request_id=request_id)
            self._log_results_written(path=str(path), results=len(web_records))
            return str(path)

        if scope == "paper":
            paper_records = await self._llm_paper_records(normalized_prompt, invocations=selected)
            path = self._result_writer.write_paper_results(
                "llm",
                paper_records,
                request_id=request_id,
            )
            self._log_results_written(path=str(path), results=len(paper_records))
            return str(path)

        web_outcome, paper_outcome = await asyncio.gather(
            self._llm_web_records(normalized_prompt, invocations=selected),
            self._llm_paper_records(normalized_prompt, invocations=selected),
            return_exceptions=True,
        )
        if isinstance(web_outcome, BaseException):
            self._log_branch_failure("web", web_outcome)
            mixed_web_records: list[SearchRecord] = []
        else:
            mixed_web_records = web_outcome
        if isinstance(paper_outcome, BaseException):
            self._log_branch_failure("paper", paper_outcome)
            mixed_paper_records: list[PaperRecord] = []
        else:
            mixed_paper_records = paper_outcome
        if isinstance(web_outcome, BaseException) and isinstance(
            paper_outcome,
            BaseException,
        ):
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                "All LLM search branches failed",
            )
        path = self._result_writer.write_mixed_results(
            mixed_web_records,
            mixed_paper_records,
            request_id=request_id,
        )
        self._log_results_written(
            path=str(path),
            results=len(mixed_web_records) + len(mixed_paper_records),
        )
        return str(path)

    async def _llm_web_records(
        self, prompt: str, *, invocations: tuple[LLMInvocation, ...]
    ) -> list[SearchRecord]:
        outcomes = await asyncio.gather(
            *(self._run_llm_pipeline(invocation, prompt) for invocation in invocations),
            return_exceptions=True,
        )
        if not any(isinstance(outcome, list) for outcome in outcomes):
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                "All LLM web search provider pipelines failed",
            )
        ordered_urls: list[NormalizedURL] = []
        seen: set[NormalizedURL] = set()
        for outcome in outcomes:
            if not isinstance(outcome, list):
                continue
            for result in outcome:
                self._store.admit(result.url, result.abstract)
                if result.url not in seen:
                    seen.add(result.url)
                    ordered_urls.append(result.url)
        return [self._record_from_store(url) for url in ordered_urls]

    async def _llm_paper_records(
        self, prompt: str, *, invocations: tuple[LLMInvocation, ...]
    ) -> list[PaperRecord]:
        outcomes = await asyncio.gather(
            *(self._run_llm_paper_pipeline(invocation, prompt) for invocation in invocations),
            return_exceptions=True,
        )
        if not any(isinstance(outcome, list) for outcome in outcomes):
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                "All LLM paper search provider pipelines failed",
            )
        hits: list[PaperSearchHit] = []
        for outcome in outcomes:
            if isinstance(outcome, list):
                hits.extend(outcome)
        return await finalize_paper_hits(
            hits,
            aggregator=self._paper_aggregator,
            resolver=self._paper_resolver,
            store=self._store,
        )

    def _log_results_written(self, *, path: str, results: int) -> None:
        log_event(
            self._logger,
            logging.DEBUG,
            "results_written",
            kind="llm",
            path=path,
            results=results,
        )

    def _log_branch_failure(self, scope: str, exc: BaseException) -> None:
        log_event(
            self._logger,
            logging.DEBUG,
            "llm_search_branch_failed",
            scope=scope,
            error_type=type(exc).__name__,
        )

    async def _run_llm_pipeline(
        self,
        invocation: LLMInvocation,
        prompt: str,
    ) -> list[SearchRecord]:
        started = self._monotonic()
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_started",
            provider=invocation.provider,
            stage="llm_search",
            model=invocation.model,
        )
        try:
            markdown = await self._stages.llm_search_markdown(invocation, prompt)
            records = parse_search_markdown(markdown)
        except asyncio.CancelledError:
            raise
        except ExecutionFailure as exc:
            self._log_provider_failure(invocation.provider, "llm_search", started, exc)
            raise
        except Exception as exc:
            self._log_provider_failure(invocation.provider, "llm_search", started, exc)
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                f"LLM search provider {invocation.provider} returned invalid data",
            ) from exc
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_completed",
            provider=invocation.provider,
            stage="llm_search",
            model=invocation.model,
            output_chars=len(markdown),
            results=len(records),
            elapsed_ms=elapsed_ms(self._monotonic, started),
        )
        return records

    async def _run_llm_paper_pipeline(
        self,
        invocation: LLMInvocation,
        prompt: str,
    ) -> list[PaperSearchHit]:
        started = self._monotonic()
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_started",
            provider=invocation.provider,
            stage="llm_paper_search",
            model=invocation.model,
        )
        try:
            markdown = await self._stages.llm_paper_search_markdown(invocation, prompt)
            hits = parse_paper_markdown(markdown, provider=invocation.provider)
        except asyncio.CancelledError:
            raise
        except ExecutionFailure as exc:
            self._log_provider_failure(invocation.provider, "llm_paper_search", started, exc)
            raise
        except Exception as exc:
            self._log_provider_failure(invocation.provider, "llm_paper_search", started, exc)
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                f"LLM paper search provider {invocation.provider} returned invalid data",
            ) from exc
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_completed",
            provider=invocation.provider,
            stage="llm_paper_search",
            model=invocation.model,
            output_chars=len(markdown),
            results=len(hits),
            elapsed_ms=elapsed_ms(self._monotonic, started),
        )
        return hits

    async def _run_keyword_pipeline(
        self,
        provider: KeywordSearchProvider,
        query: str,
    ) -> list[_StagedKeyword]:
        started = self._monotonic()
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_started",
            provider=provider.name,
            stage="search",
        )
        try:
            async with self.quotas.get_web(provider.name).lease():
                hits = await provider.search(query)
            if not isinstance(hits, list):
                raise TypeError("provider search result must be a list")
        except asyncio.CancelledError:
            raise
        except ExecutionFailure as exc:
            self._log_provider_failure(provider.name, "search", started, exc)
            raise
        except Exception as exc:
            self._log_provider_failure(provider.name, "search", started, exc)
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                f"Keyword provider {provider.name} returned invalid data",
            ) from exc

        staged: list[_StagedKeyword] = []
        hit_failure: ExecutionFailure | None = None
        failed_hits = 0
        try:
            for hit in hits:
                try:
                    staged_hit = await self._stage_keyword_hit(hit, provider=provider.name)
                except ExecutionFailure as exc:
                    # Keyword hits are independent; one failed judge must not discard sibling hits.
                    if hit_failure is None:
                        hit_failure = exc
                    failed_hits += 1
                    continue
                if staged_hit is not None:
                    staged.append(staged_hit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_provider_failure(provider.name, "hit_staging", started, exc)
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                f"Keyword provider {provider.name} returned invalid data",
            ) from exc

        if hit_failure is not None:
            if not staged:
                self._log_provider_failure(provider.name, "judge", started, hit_failure)
                raise hit_failure
            self._log_provider_partial_failure(
                provider.name,
                "judge",
                started,
                hit_failure,
                failed_hits=failed_hits,
            )
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_completed",
            provider=provider.name,
            stage="search",
            hits=len(hits),
            results=len(staged),
            elapsed_ms=elapsed_ms(self._monotonic, started),
        )
        return staged

    async def _stage_keyword_hit(
        self,
        hit: KeywordSearchHit,
        *,
        provider: str,
    ) -> _StagedKeyword | None:
        if not isinstance(hit, KeywordSearchHit):
            raise TypeError("keyword hit has invalid type")
        for value in (hit.url, hit.title, hit.snippet, hit.raw_content, hit.content):
            if not isinstance(value, str):
                raise TypeError("keyword hit fields must be strings")

        abstract = hit.snippet.strip() or hit.title.strip()
        if not abstract:
            try:
                logged_url = target_url_for_log(str(normalize_url(hit.url)))
            except InputFailure:
                logged_url = target_url_for_log(hit.url)
            log_event(
                self._logger,
                logging.DEBUG,
                "candidate_rejected",
                provider=provider,
                url=logged_url,
                reason="empty_abstract",
            )
            return None
        url = normalize_url(hit.url)
        log_event(
            self._logger,
            logging.DEBUG,
            "candidate_accepted",
            provider=provider,
            url=target_url_for_log(str(url)),
            abstract_chars=len(abstract),
        )
        current = self._store.get(url)
        if current is not None and not current.available:
            self._log_body_decision(provider, url, "body_skipped", "stored_unavailable")
            return _StagedKeyword(url=url, abstract=abstract, provider=provider)

        had_body = bool(hit.raw_content or hit.content)
        raw_content = hit.raw_content if hit.raw_content.strip() else ""
        content = hit.content if hit.content.strip() else ""
        candidate = content or raw_content
        if not candidate:
            reason = "cheap_check" if had_body else "no_body"
            event = "body_rejected" if had_body else "body_skipped"
            self._log_body_decision(provider, url, event, reason)
            return _StagedKeyword(url=url, abstract=abstract, provider=provider)
        if not cheap_check(candidate):
            self._log_body_decision(provider, url, "body_rejected", "cheap_check")
            return _StagedKeyword(url=url, abstract=abstract, provider=provider)

        decision = await self._stages.judge(candidate)
        if not decision.ok:
            self._log_body_decision(provider, url, "body_rejected", "judge_rejected")
            return _StagedKeyword(url=url, abstract=abstract, provider=provider)
        log_event(
            self._logger,
            logging.DEBUG,
            "body_accepted",
            provider=provider,
            url=target_url_for_log(str(url)),
            raw_chars=len(raw_content),
            content_chars=len(content),
        )
        return _StagedKeyword(
            url=url,
            abstract=abstract,
            provider=provider,
            raw_content=raw_content,
            content=content,
        )

    def _log_body_decision(
        self,
        provider: str,
        url: NormalizedURL,
        event: str,
        reason: str,
    ) -> None:
        log_event(
            self._logger,
            logging.DEBUG,
            event,
            provider=provider,
            url=target_url_for_log(str(url)),
            reason=reason,
        )

    def _log_provider_failure(
        self,
        provider: str,
        stage: str,
        started: float,
        exc: Exception,
    ) -> None:
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_failed",
            provider=provider,
            stage=stage,
            error_type=type(exc).__name__,
            elapsed_ms=elapsed_ms(self._monotonic, started),
        )

    def _log_provider_partial_failure(
        self,
        provider: str,
        stage: str,
        started: float,
        exc: Exception,
        *,
        failed_hits: int,
    ) -> None:
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_partial_failure",
            provider=provider,
            stage=stage,
            error_type=type(exc).__name__,
            failed_hits=failed_hits,
            elapsed_ms=elapsed_ms(self._monotonic, started),
        )

    def _record_from_store(self, url: NormalizedURL) -> SearchRecord:
        record = self._store.get(url)
        if record is None:
            raise RuntimeError("committed URL disappeared from store")
        return SearchRecord(record.url, record.abstract)
