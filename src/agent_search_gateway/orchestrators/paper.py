"""Direct academic paper discovery orchestration and shared paper finalization."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence

from ..academic.aggregator import PaperAggregator
from ..academic.enrichment import enrich_paper_records
from ..concurrency import ProviderQuotaManager
from ..errors import ErrorCode, ExecutionFailure, InputFailure
from ..models import PaperRecord
from ..observability import elapsed_ms, log_event
from ..providers.contracts import AcademicSearchProvider, OAResolver, PaperSearchHit
from ..request_ids import validate_request_id
from ..result_writer import ResultWriter
from ..url_store import URLStore


async def finalize_paper_hits(
    hits: Sequence[PaperSearchHit],
    *,
    aggregator: PaperAggregator,
    resolver: OAResolver | None,
    store: URLStore,
) -> list[PaperRecord]:
    """Apply the one shared aggregate -> OA enrich -> landing admission policy."""

    records = aggregator.aggregate(hits)
    enriched = await enrich_paper_records(records, resolver)
    for record in enriched:
        admission_abstract = record.abstract.strip() or record.title.strip()
        store.admit(record.url, admission_abstract)
    return enriched


class PaperSearchOrchestrator:
    def __init__(
        self,
        *,
        providers: Sequence[AcademicSearchProvider],
        quotas: ProviderQuotaManager,
        aggregator: PaperAggregator,
        resolver: OAResolver | None,
        store: URLStore,
        result_writer: ResultWriter,
        logger: logging.Logger | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.providers = tuple(providers)
        self.quotas = quotas
        self.aggregator = aggregator
        self.resolver = resolver
        self.store = store
        self.result_writer = result_writer
        self._logger = logger or logging.getLogger(__name__)
        self._monotonic = monotonic

    async def paper_search(
        self, query: str, *, request_id: str, provider: str | None = None
    ) -> str:
        validate_request_id(request_id)
        normalized_query = query.strip()
        if not normalized_query:
            raise InputFailure(ErrorCode.EMPTY_QUERY, "Query must not be empty")
        if not self.providers:
            raise ExecutionFailure(
                ErrorCode.NO_ACADEMIC_SEARCH_PROVIDERS,
                "No academic search providers are enabled",
            )

        selected = self.providers
        if provider is not None:
            if not provider.strip():
                raise InputFailure(ErrorCode.BAD_REQUEST, "Provider must not be empty")
            selected = tuple(candidate for candidate in selected if candidate.name == provider)
            if not selected:
                raise InputFailure(
                    ErrorCode.BAD_REQUEST,
                    f"No enabled paper-search provider matches '{provider}'",
                )

        outcomes = await asyncio.gather(
            *(self._run_provider(candidate, normalized_query) for candidate in selected),
            return_exceptions=True,
        )
        if not any(isinstance(outcome, list) for outcome in outcomes):
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                "All academic search provider pipelines failed",
            )

        hits: list[PaperSearchHit] = []
        for outcome in outcomes:
            if isinstance(outcome, list):
                hits.extend(outcome)
        records = await finalize_paper_hits(
            hits,
            aggregator=self.aggregator,
            resolver=self.resolver,
            store=self.store,
        )
        path = self.result_writer.write_paper_results(
            "paper",
            records,
            request_id=request_id,
        )
        log_event(
            self._logger,
            logging.DEBUG,
            "results_written",
            kind="paper",
            path=str(path),
            results=len(records),
        )
        return str(path)

    async def _run_provider(
        self,
        provider: AcademicSearchProvider,
        query: str,
    ) -> list[PaperSearchHit]:
        started = self._monotonic()
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_started",
            provider=provider.name,
            stage="paper_search",
        )
        try:
            async with self.quotas.get_academic(provider.name).lease():
                hits = await provider.search(query)
            if not isinstance(hits, list):
                raise TypeError("academic provider search result must be a list")
            if any(not isinstance(hit, PaperSearchHit) for hit in hits):
                raise TypeError("academic provider search result contains an invalid item")
        except asyncio.CancelledError:
            raise
        except ExecutionFailure as exc:
            self._log_provider_failure(provider.name, started, exc)
            raise
        except Exception as exc:
            self._log_provider_failure(provider.name, started, exc)
            raise ExecutionFailure(
                ErrorCode.ALL_PROVIDERS_FAILED,
                f"Academic provider {provider.name} returned invalid data",
            ) from exc
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_completed",
            provider=provider.name,
            stage="paper_search",
            results=len(hits),
            elapsed_ms=elapsed_ms(self._monotonic, started),
        )
        return hits

    def _log_provider_failure(
        self,
        provider: str,
        started: float,
        exc: BaseException,
    ) -> None:
        if isinstance(exc, ExecutionFailure) and exc.reason:
            log_event(
                self._logger,
                logging.DEBUG,
                "provider_failed",
                provider=provider,
                stage="paper_search",
                error_type=type(exc).__name__,
                elapsed_ms=elapsed_ms(self._monotonic, started),
                reason=exc.reason,
            )
            return
        log_event(
            self._logger,
            logging.DEBUG,
            "provider_failed",
            provider=provider,
            stage="paper_search",
            error_type=type(exc).__name__,
            elapsed_ms=elapsed_ms(self._monotonic, started),
        )
