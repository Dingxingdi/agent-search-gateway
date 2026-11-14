"""dblp publication search API adapter."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from datetime import date

import httpx

from ...academic.normalization import normalize_dblp_key, normalize_doi
from ...errors import ProtocolFailure
from ...observability import log_event
from ..contracts import PaperSearchHit
from ..diagnostics import CandidateCounts
from .common import AcademicHttpExecutor, protocol_failure, reject_item

_DEFAULT_API_URL = "https://dblp.org/search/publ/api"


def _parse_response(body: str) -> ET.Element:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise protocol_failure(
            "dblp",
            "response was not valid XML",
            diagnostics={
                "category": "decode",
                "semantic_result": "unknown",
                "response_kind": "unknown",
            },
        ) from exc
    if root.tag != "result" or root.find("hits") is None:
        raise protocol_failure(
            "dblp",
            "response XML envelope was invalid",
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


class DblpProvider:
    name = "dblp"

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
            params={"q": query, "format": "xml", "h": 10},
            response_diagnostics=_response_diagnostics,
        )
        root = _parse_response(body)
        entries = root.findall("./hits/hit")
        counts = CandidateCounts(len(entries))
        hits: list[PaperSearchHit] = []
        for hit in entries:
            mapped, reason = self._map_hit(hit)
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

    def _map_hit(self, hit: ET.Element) -> tuple[PaperSearchHit | None, str]:
        info = hit.find("info")
        if info is None:
            return None, "invalid_record_shape"
        title_element = info.find("title")
        title = self._element_text(title_element)
        url = self._child_text(info, "url")
        source_id = normalize_dblp_key((info.get("key") or "").strip())
        if source_id is None:
            source_id = normalize_dblp_key(url)
        if source_id is None or not title or not url:
            return None, "missing_required_field"
        authors = tuple(
            author
            for element in info.findall("./authors/author")
            if (author := self._element_text(element))
        )
        doi = ""
        for edition in info.findall("ee"):
            candidate = normalize_doi(self._element_text(edition))
            if candidate is not None:
                doi = candidate
                break
        published_date = self._year_date(self._child_text(info, "year"))
        return PaperSearchHit(
            source=self.name,
            source_id=source_id,
            title=title,
            authors=authors,
            doi=doi,
            published_date=published_date,
            url=url,
            venue=self._child_text(info, "venue"),
        ), ""

    @staticmethod
    def _element_text(element: ET.Element | None) -> str:
        if element is None:
            return ""
        return " ".join("".join(element.itertext()).split())

    @classmethod
    def _child_text(cls, parent: ET.Element, name: str) -> str:
        return cls._element_text(parent.find(name))

    @staticmethod
    def _year_date(raw_year: str) -> date | None:
        try:
            year = int(raw_year)
            if year < 1:
                return None
            return date(year, 1, 1)
        except (TypeError, ValueError):
            return None
