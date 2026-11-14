"""Semantic Scholar Graph API discovery adapter."""

from __future__ import annotations

import logging
from collections.abc import Mapping

import httpx

from ...observability import SecretValue, log_event
from ..contracts import PaperSearchHit
from ..diagnostics import CandidateCounts, best_effort_diagnostics, safe_diagnostic_fields
from .common import (
    AcademicHttpExecutor,
    as_list,
    as_mapping,
    join_url,
    nonnegative_int,
    parse_iso_date,
    protocol_failure,
    reject_item,
    text,
)

_DEFAULT_API_URL = "https://api.semanticscholar.org/graph/v1"
_FIELDS = (
    "title,abstract,citationCount,authors,url,publicationDate,"
    "externalIds,fieldsOfStudy,openAccessPdf"
)


def _response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    return _payload_diagnostics(payload)


def _payload_diagnostics(payload: object) -> Mapping[str, object]:
    envelope = as_mapping(payload)
    data = as_list(envelope.get("data")) if envelope is not None else None
    if data is not None:
        return {"semantic_result": "success", "response_kind": "object"}
    error = envelope.get("error") if envelope is not None else None
    if isinstance(error, str):
        return safe_diagnostic_fields(
            {
                "category": "provider_response",
                "semantic_result": "error",
                "response_kind": "object",
                "upstream_message": error,
            }
        )
    return {"category": "invalid_envelope", "semantic_result": "unknown"}


class SemanticScholarProvider:
    name = "semantic_scholar"

    def __init__(
        self,
        executor: AcademicHttpExecutor,
        *,
        api_url: str = _DEFAULT_API_URL,
        api_key: SecretValue | None = None,
    ) -> None:
        self._executor = executor
        self._api_url = api_url
        self._credential = api_key

    async def search(self, query: str) -> list[PaperSearchHit]:
        payload = await self._executor.request_json(
            "GET",
            join_url(self._api_url, "paper/search"),
            stage="paper_search",
            headers=self._request_headers(),
            params={"query": query, "limit": 10, "fields": _FIELDS},
            response_diagnostics=_response_diagnostics,
        )
        envelope = as_mapping(payload)
        if envelope is None:
            reason = "invalid_data_envelope"
            raise protocol_failure(
                self.name,
                reason,
                reason=reason,
                diagnostics=best_effort_diagnostics(lambda: _payload_diagnostics(payload)),
            )
        if "data" not in envelope:
            reason = "missing_data_envelope"
            raise protocol_failure(
                self.name,
                reason,
                reason=reason,
                diagnostics=best_effort_diagnostics(lambda: _payload_diagnostics(payload)),
            )
        data = as_list(envelope.get("data"))
        if data is None:
            reason = "invalid_data_envelope"
            raise protocol_failure(
                self.name,
                reason,
                reason=reason,
                diagnostics=best_effort_diagnostics(lambda: _payload_diagnostics(payload)),
            )
        counts = CandidateCounts(len(data))
        hits: list[PaperSearchHit] = []
        for item in data:
            mapped, reason = self._map_item(item)
            if mapped is None:
                reject_item(self.name, reason)
                counts.reject(reason)
            else:
                hits.append(mapped)
                counts.accept()
        log_event(
            logging.getLogger(__name__),
            logging.DEBUG,
            "provider_candidates_summary",
            provider=self.name,
            stage="paper_search",
            phase="adapter",
            exc_info=False,
            **counts.as_fields(),
        )
        return hits

    def _request_headers(self) -> dict[str, str] | None:
        if self._credential is None:
            return None
        reveal = self._credential.reveal
        return {"x-api-key": reveal()}

    def _map_item(self, value: object) -> tuple[PaperSearchHit | None, str]:
        item = as_mapping(value)
        if item is None:
            return None, "invalid_record_shape"
        paper_id = text(item.get("paperId"))
        title = text(item.get("title"))
        url = text(item.get("url"))
        if not paper_id or not title or not url:
            return None, "missing_required_field"
        external = as_mapping(item.get("externalIds")) or {}
        authors_raw = as_list(item.get("authors")) or []
        authors = tuple(
            name
            for author in authors_raw
            if (mapping := as_mapping(author)) is not None and (name := text(mapping.get("name")))
        )
        topics = tuple(
            topic
            for raw_topic in (as_list(item.get("fieldsOfStudy")) or [])
            if (topic := text(raw_topic))
        )
        oa = as_mapping(item.get("openAccessPdf"))
        pdf_url = text(oa.get("url")) if oa is not None else ""
        return PaperSearchHit(
            source=self.name,
            source_id=paper_id,
            title=title,
            authors=authors,
            abstract=text(item.get("abstract")),
            doi=text(external.get("DOI")),
            arxiv_id=text(external.get("ArXiv")),
            published_date=parse_iso_date(item.get("publicationDate")),
            url=url,
            pdf_url=pdf_url,
            topics=topics,
            citation_count=nonnegative_int(item.get("citationCount")),
            is_open_access=True if pdf_url else None,
        ), ""
