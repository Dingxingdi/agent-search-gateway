"""arXiv Atom API discovery adapter."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Mapping

import httpx

from ...academic.normalization import normalize_arxiv_id, normalize_doi
from ...errors import ProtocolFailure
from ...observability import log_event
from ..contracts import PaperSearchHit
from ..diagnostics import CandidateCounts
from .common import AcademicHttpExecutor, parse_iso_date, protocol_failure, reject_item

_ATOM = "http://www.w3.org/2005/Atom"
_ARXIV = "http://arxiv.org/schemas/atom"
_DEFAULT_API_URL = "https://export.arxiv.org/api/query"


def _parse_response(body: str) -> ET.Element:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise protocol_failure(
            "arxiv",
            "response was not valid Atom XML",
            diagnostics={
                "category": "decode",
                "semantic_result": "unknown",
                "response_kind": "unknown",
            },
        ) from exc
    if root.tag != f"{{{_ATOM}}}feed":
        raise protocol_failure(
            "arxiv",
            "response Atom envelope was invalid",
            diagnostics={
                "category": "invalid_envelope",
                "semantic_result": "unknown",
                "response_kind": "xml",
            },
        )
    return root


def _response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    try:
        _parse_response(response.text)
    except ProtocolFailure as exc:
        return exc.diagnostics
    return {"semantic_result": "success", "response_kind": "xml"}


class ArxivProvider:
    name = "arxiv"

    def __init__(
        self,
        executor: AcademicHttpExecutor,
        *,
        api_url: str = _DEFAULT_API_URL,
    ) -> None:
        self._executor = executor
        self._api_url = api_url

    async def search(self, query: str) -> list[PaperSearchHit]:
        body = await self._executor.request_text(
            "GET",
            self._api_url,
            stage="paper_search",
            params={
                "search_query": f"all:{query}",
                "max_results": 10,
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
            response_diagnostics=_response_diagnostics,
        )
        root = _parse_response(body)
        entries = root.findall(f"{{{_ATOM}}}entry")
        counts = CandidateCounts(len(entries))
        hits: list[PaperSearchHit] = []
        for entry in entries:
            mapped = self._map_entry(entry)
            if mapped is None:
                reject_item(self.name, "missing_required_field")
                counts.reject("missing_required_field")
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

    def _map_entry(self, entry: ET.Element) -> PaperSearchHit | None:
        raw_id = self._child_text(entry, _ATOM, "id")
        source_id = normalize_arxiv_id(raw_id)
        title = self._clean(self._child_text(entry, _ATOM, "title"))
        if source_id is None or not title:
            return None

        landing_url = raw_id
        pdf_url = ""
        for link in entry.findall(f"{{{_ATOM}}}link"):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            if link.get("rel") == "alternate":
                landing_url = href
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf_url = href
        if not landing_url:
            return None

        authors = tuple(
            name
            for author in entry.findall(f"{{{_ATOM}}}author")
            if (name := self._clean(self._child_text(author, _ATOM, "name")))
        )
        topics = tuple(
            term
            for category in entry.findall(f"{{{_ATOM}}}category")
            if (term := (category.get("term") or "").strip())
        )
        return PaperSearchHit(
            source=self.name,
            source_id=source_id,
            title=title,
            authors=authors,
            abstract=self._clean(self._child_text(entry, _ATOM, "summary")),
            doi=normalize_doi(self._child_text(entry, _ARXIV, "doi")) or "",
            arxiv_id=source_id,
            published_date=parse_iso_date(self._child_text(entry, _ATOM, "published")),
            updated_date=parse_iso_date(self._child_text(entry, _ATOM, "updated")),
            url=landing_url,
            pdf_url=pdf_url,
            topics=topics,
        )

    @staticmethod
    def _child_text(parent: ET.Element, namespace: str, name: str) -> str:
        child = parent.find(f"{{{namespace}}}{name}")
        return child.text if child is not None and child.text is not None else ""

    @staticmethod
    def _clean(value: str) -> str:
        return " ".join(value.split())
