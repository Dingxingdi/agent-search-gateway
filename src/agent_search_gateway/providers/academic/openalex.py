"""OpenAlex works API discovery adapter."""

from __future__ import annotations

import logging
from collections.abc import Mapping

import httpx

from ...academic.normalization import normalize_openalex_id
from ...observability import SecretValue, log_event
from ..contracts import PaperSearchHit
from ..diagnostics import CandidateCounts
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

_DEFAULT_API_URL = "https://api.openalex.org"


def reconstruct_abstract(value: object) -> str:
    """Reconstruct OpenAlex's inverted abstract index by numeric position."""

    if not isinstance(value, dict):
        return ""
    positioned: list[tuple[int, str]] = []
    for raw_word, raw_positions in value.items():
        if not isinstance(raw_word, str) or not isinstance(raw_positions, list):
            continue
        for raw_position in raw_positions:
            if (
                isinstance(raw_position, bool)
                or not isinstance(raw_position, int)
                or raw_position < 0
            ):
                continue
            positioned.append((raw_position, raw_word))
    positioned.sort(key=lambda item: (item[0], item[1]))
    return " ".join(word for _, word in positioned)


def _response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    envelope = as_mapping(payload)
    results = as_list(envelope.get("results")) if envelope is not None else None
    if results is None:
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    return {"semantic_result": "success", "response_kind": "object"}


class OpenAlexProvider:
    name = "openalex"

    def __init__(
        self,
        executor: AcademicHttpExecutor,
        *,
        api_url: str = _DEFAULT_API_URL,
        contact_email: SecretValue | None = None,
    ) -> None:
        self._executor = executor
        self._api_url = api_url
        self._contact = contact_email

    async def search(self, query: str) -> list[PaperSearchHit]:
        params: dict[str, object] = {"search": query, "per_page": 10}
        if self._contact is not None:
            reveal = self._contact.reveal
            params["mailto"] = reveal()
        payload = await self._executor.request_json(
            "GET",
            join_url(self._api_url, "works"),
            stage="paper_search",
            params=params,
            response_diagnostics=_response_diagnostics,
        )
        envelope = as_mapping(payload)
        results = as_list(envelope.get("results")) if envelope is not None else None
        if results is None:
            raise protocol_failure(self.name, "response results envelope was invalid")
        counts = CandidateCounts(len(results))
        hits: list[PaperSearchHit] = []
        for item in results:
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

    def _map_item(self, value: object) -> tuple[PaperSearchHit | None, str]:
        item = as_mapping(value)
        if item is None:
            return None, "invalid_record_shape"
        source_id = normalize_openalex_id(text(item.get("id")))
        title = text(item.get("title"))
        if source_id is None or not title:
            return None, "missing_required_field"
        primary = as_mapping(item.get("primary_location")) or {}
        oa = as_mapping(item.get("open_access")) or {}
        landing = text(primary.get("landing_page_url")) or f"https://openalex.org/{source_id}"
        pdf_url = text(primary.get("pdf_url"))
        authors = self._authors(item.get("authorships"))
        topics = self._concepts(item.get("concepts"))
        raw_is_oa = oa.get("is_oa")
        is_oa = raw_is_oa if isinstance(raw_is_oa, bool) else None
        return PaperSearchHit(
            source=self.name,
            source_id=source_id,
            title=title,
            authors=authors,
            abstract=reconstruct_abstract(item.get("abstract_inverted_index")),
            doi=text(item.get("doi")),
            published_date=parse_iso_date(item.get("publication_date")),
            url=landing,
            pdf_url=pdf_url,
            topics=topics,
            citation_count=nonnegative_int(item.get("cited_by_count")),
            is_open_access=is_oa,
            oa_status=text(oa.get("oa_status")),
            license=text(primary.get("license")),
        ), ""

    @staticmethod
    def _authors(value: object) -> tuple[str, ...]:
        result: list[str] = []
        for raw_authorship in as_list(value) or []:
            authorship = as_mapping(raw_authorship)
            author = as_mapping(authorship.get("author")) if authorship is not None else None
            name = text(author.get("display_name")) if author is not None else ""
            if name:
                result.append(name)
        return tuple(result)

    @staticmethod
    def _concepts(value: object) -> tuple[str, ...]:
        result: list[str] = []
        for raw_concept in as_list(value) or []:
            concept = as_mapping(raw_concept)
            name = text(concept.get("display_name")) if concept is not None else ""
            if name:
                result.append(name)
        return tuple(result)
