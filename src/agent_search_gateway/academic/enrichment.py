"""Post-deduplication open-access enrichment for paper records."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

from ..errors import ExecutionFailure, ProtocolFailure
from ..models import OAResolution, PaperRecord
from ..observability import log_event
from ..providers.contracts import OAResolver
from ..providers.diagnostics import best_effort_diagnostics, diagnostic_fields_from_failure

_LOGGER = logging.getLogger(__name__)


async def enrich_paper_records(
    records: list[PaperRecord],
    resolver: OAResolver | None,
) -> list[PaperRecord]:
    """Enrich DOI-bearing papers without allowing resolver failures to drop records."""

    if resolver is None:
        return list(records)
    cache: dict[str, OAResolution | None] = {}
    enriched: list[PaperRecord] = []
    for record in records:
        doi = record.identifiers.doi
        if not doi:
            enriched.append(record)
            continue
        if doi not in cache:
            try:
                log_event(
                    _LOGGER,
                    logging.DEBUG,
                    "provider_started",
                    provider=resolver.name,
                    stage="oa_resolve",
                )
                cache[doi] = await resolver.resolve(doi)
                log_event(
                    _LOGGER,
                    logging.DEBUG,
                    "provider_completed",
                    provider=resolver.name,
                    stage="oa_resolve",
                    outcome="no_match" if cache[doi] is None else "resolved",
                )
            except asyncio.CancelledError:
                log_event(
                    _LOGGER,
                    logging.DEBUG,
                    "provider_failed",
                    provider=resolver.name,
                    stage="oa_resolve",
                    category="cancelled",
                    reason="cancelled",
                    error_type="CancelledError",
                )
                raise
            except Exception as exc:
                log_event(
                    _LOGGER,
                    logging.DEBUG,
                    "provider_failed",
                    provider=resolver.name,
                    stage="oa_resolve",
                    category=_failure_category(exc),
                    reason="resolver_failed",
                    error_type=type(exc).__name__,
                    exc_info=False,
                    **diagnostic_fields_from_failure(exc),
                )
                log_event(
                    _LOGGER,
                    logging.DEBUG,
                    "paper_enrichment_failed",
                    resolver=resolver.name,
                    stage="oa_resolve",
                    error_type=type(exc).__name__,
                )
                cache[doi] = None
        resolution = cache[doi]
        enriched.append(_apply_resolution(record, resolution))
    return enriched


def _failure_category(exc: BaseException) -> object:
    category = "protocol" if isinstance(exc, ProtocolFailure) else "unknown"
    if isinstance(exc, ExecutionFailure):
        return best_effort_diagnostics(lambda: exc.diagnostics).get("category", category)
    return category


def _apply_resolution(record: PaperRecord, resolution: OAResolution | None) -> PaperRecord:
    if resolution is None:
        return record
    return replace(
        record,
        pdf_url=record.pdf_url or resolution.pdf_url,
        is_open_access=(
            record.is_open_access
            if record.is_open_access is not None
            else resolution.is_open_access
        ),
        oa_status=record.oa_status or resolution.oa_status,
        license=record.license or resolution.license,
    )
