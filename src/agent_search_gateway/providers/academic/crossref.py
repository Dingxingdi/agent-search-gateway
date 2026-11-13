"""Crossref works API discovery adapter."""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Mapping
from datetime import date

import httpx

from ...academic.normalization import normalize_doi
from ...observability import SecretValue, log_event
from ..contracts import PaperSearchHit
from ..diagnostics import CandidateCounts
from .common import (
    AcademicHttpExecutor,
    as_list,
    as_mapping,
    join_url,
    nonnegative_int,
    protocol_failure,
    reject_item,
    text,
)

_DEFAULT_API_URL = "https://api.crossref.org"
_TAG_RE = re.compile(r"<[^>]+>")
_DATE_KEYS = ("published-print", "published-online", "published", "issued", "created")


def _response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    envelope = as_mapping(payload)
    message = as_mapping(envelope.get("message")) if envelope is not None else None
    items = as_list(message.get("items")) if message is not None else None
    if items is None:
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    return {"semantic_result": "success", "response_kind": "object"}


class CrossrefProvider:
    name = "crossref"

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
        params: dict[str, object] = {
            "query": query,
            "rows": 10,
            "sort": "relevance",
            "order": "desc",
        }
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
        message = as_mapping(envelope.get("message")) if envelope is not None else None
        items = as_list(message.get("items")) if message is not None else None
        if items is None:
            raise protocol_failure(self.name, "response message.items envelope was invalid")
        counts = CandidateCounts(len(items))
        hits: list[PaperSearchHit] = []
        for item in items:
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
        doi = normalize_doi(text(item.get("DOI")))
        title = self._first_string(item.get("title"))
        if doi is None or not title:
            return None, "missing_required_field"
        authors = self._authors(item.get("author"))
        venue = self._first_string(item.get("container-title"))
        url = text(item.get("URL")) or f"https://doi.org/{doi}"
        return PaperSearchHit(
            source=self.name,
            source_id=doi,
            title=title,
            authors=authors,
            abstract=self._abstract(item.get("abstract")),
            doi=doi,
            published_date=self._date(item),
            url=url,
            pdf_url=self._pdf_url(item.get("link")),
            venue=venue,
            citation_count=nonnegative_int(item.get("is-referenced-by-count")),
        ), ""

    @staticmethod
    def _first_string(value: object) -> str:
        values = as_list(value) or []
        for raw in values:
            candidate = text(raw)
            if candidate:
                return candidate
        return ""

    @staticmethod
    def _authors(value: object) -> tuple[str, ...]:
        authors: list[str] = []
        for raw_author in as_list(value) or []:
            author = as_mapping(raw_author)
            if author is None:
                continue
            given = text(author.get("given"))
            family = text(author.get("family"))
            name = " ".join(part for part in (given, family) if part)
            if name:
                authors.append(name)
        return tuple(authors)

    @staticmethod
    def _abstract(value: object) -> str:
        raw = text(value)
        if not raw:
            return ""
        return " ".join(html.unescape(_TAG_RE.sub(" ", raw)).split())

    @classmethod
    def _date(cls, item: object) -> date | None:
        mapping = as_mapping(item)
        if mapping is None:
            return None
        for key in _DATE_KEYS:
            date_mapping = as_mapping(mapping.get(key))
            parts_rows = (
                as_list(date_mapping.get("date-parts")) if date_mapping is not None else None
            )
            if not parts_rows:
                continue
            parts = as_list(parts_rows[0])
            if not parts or isinstance(parts[0], bool) or not isinstance(parts[0], int):
                continue
            year = parts[0]
            month = (
                parts[1]
                if len(parts) > 1 and isinstance(parts[1], int) and not isinstance(parts[1], bool)
                else 1
            )
            day = (
                parts[2]
                if len(parts) > 2 and isinstance(parts[2], int) and not isinstance(parts[2], bool)
                else 1
            )
            try:
                return date(year, month, day)
            except ValueError:
                continue
        return None

    @staticmethod
    def _pdf_url(value: object) -> str:
        for raw_link in as_list(value) or []:
            link = as_mapping(raw_link)
            if link is None:
                continue
            candidate = text(link.get("URL"))
            content_type = text(link.get("content-type")).casefold()
            if candidate and content_type == "application/pdf":
                return candidate
        return ""
