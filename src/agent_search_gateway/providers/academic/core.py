"""CORE works search API discovery adapter."""

from __future__ import annotations

import logging
from collections.abc import Mapping

import httpx

from ...academic.normalization import normalize_core_id
from ...observability import SecretValue
from ..contracts import PaperSearchHit
from .common import (
    AcademicHttpExecutor,
    as_list,
    as_mapping,
    join_url,
    json_candidates_diagnostics,
    map_paper_candidates,
    nonnegative_int,
    parse_iso_date,
    protocol_failure,
    text,
)

_DEFAULT_API_URL = "https://api.core.ac.uk/v3"


def _response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    return json_candidates_diagnostics(response, "results")


class CoreProvider:
    name = "core"

    def __init__(
        self,
        executor: AcademicHttpExecutor,
        *,
        api_key: SecretValue,
        api_url: str = _DEFAULT_API_URL,
    ) -> None:
        self._executor = executor
        self._api_url = api_url
        self._credential = api_key

    async def search(self, query: str) -> list[PaperSearchHit]:
        payload = await self._executor.request_json(
            "GET",
            join_url(self._api_url, "search/works/"),
            stage="paper_search",
            headers=self._request_headers(),
            params={"q": query, "limit": 10, "offset": 0},
            response_diagnostics=_response_diagnostics,
        )
        envelope = as_mapping(payload)
        results = as_list(envelope.get("results")) if envelope is not None else None
        if results is None:
            raise protocol_failure(self.name, "response results envelope was invalid")
        return map_paper_candidates(
            self.name, results, self._map_item, logger=logging.getLogger(__name__)
        )

    def _request_headers(self) -> dict[str, str]:
        reveal = self._credential.reveal
        return {"Authorization": f"Bearer {reveal()}"}

    def _map_item(self, value: object) -> tuple[PaperSearchHit | None, str]:
        item = as_mapping(value)
        if item is None:
            return None, "invalid_record_shape"
        raw_id = item.get("id")
        source_id = normalize_core_id(str(raw_id)) if isinstance(raw_id, str | int) else None
        title = text(item.get("title"))
        if source_id is None or not title:
            return None, "missing_required_field"
        repository = as_mapping(item.get("repository")) or {}
        return PaperSearchHit(
            source=self.name,
            source_id=source_id,
            title=title,
            authors=self._authors(item.get("authors")),
            abstract=text(item.get("abstract")),
            doi=text(item.get("doi")),
            published_date=parse_iso_date(item.get("publishedDate")),
            url=f"https://core.ac.uk/works/{source_id}",
            pdf_url=self._pdf_url(item),
            venue=text(repository.get("name")),
            topics=self._topics(item),
            citation_count=nonnegative_int(item.get("citationCount")),
        ), ""

    @staticmethod
    def _authors(value: object) -> tuple[str, ...]:
        authors: list[str] = []
        for raw_author in as_list(value) or []:
            if isinstance(raw_author, str):
                name = raw_author.strip()
            else:
                author = as_mapping(raw_author)
                name = text(author.get("name")) if author is not None else ""
            if name:
                authors.append(name)
        return tuple(authors)

    @staticmethod
    def _pdf_url(item: object) -> str:
        mapping = as_mapping(item)
        if mapping is None:
            return ""
        direct = text(mapping.get("downloadUrl"))
        if direct:
            return direct
        for raw_url in as_list(mapping.get("fullTextUrls")) or []:
            candidate = text(raw_url)
            if candidate:
                return candidate
        return ""

    @staticmethod
    def _topics(item: object) -> tuple[str, ...]:
        mapping = as_mapping(item)
        if mapping is None:
            return ()
        topics: list[str] = []
        seen: set[str] = set()
        for key in ("subjects", "tags"):
            for raw_topic in as_list(mapping.get(key)) or []:
                topic = text(raw_topic)
                folded = topic.casefold()
                if topic and folded not in seen:
                    seen.add(folded)
                    topics.append(topic)
        return tuple(topics)
