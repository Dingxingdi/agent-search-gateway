"""Firecrawl v2 search and scrape adapter."""

import logging
from collections.abc import Mapping, Sequence

import httpx

from ...errors import ExecutionFailure
from ...observability import SecretValue, log_event
from ...providers.contracts import KeywordSearchHit, URLFetchCandidate
from ...url_normalization import NormalizedURL
from ..diagnostics import CandidateCounts, best_effort_diagnostics
from .common import (
    JsonRequester,
    candidate_rejection_reason,
    endpoint,
    failure,
    non_empty_string,
    optional_string,
    require_list,
    require_object,
    selected_error_fields,
)


def _v2_endpoint(api_url: str, suffix: str) -> str:
    base = api_url.rstrip("/")
    if base.endswith("/v2"):
        return endpoint(base, suffix)
    return endpoint(base, f"/v2/{suffix.lstrip('/')}")


def _page_body(
    result: dict[str, object],
    provider: str,
    stage: str,
    *,
    pdf_parser_enabled: bool,
) -> URLFetchCandidate:
    markdown = optional_string(result.get("markdown"), provider, stage, "markdown")
    raw_html = optional_string(result.get("rawHtml"), provider, stage, "rawHtml")
    raw = raw_html or markdown
    metadata = result.get("metadata")
    content_type = metadata.get("contentType") if isinstance(metadata, dict) else None
    # Firecrawl puts the original PDF bytes in markdown and rawHtml as Base64 without its parser.
    unparsed_pdf = (
        not pdf_parser_enabled
        and isinstance(content_type, str)
        and content_type.partition(";")[0].strip().lower() == "application/pdf"
    )
    if unparsed_pdf or not raw.strip():
        raise failure(
            provider,
            stage,
            "page body is empty",
            diagnostics={"semantic_result": "empty", "response_kind": "object"},
        )
    return URLFetchCandidate(raw_content=raw, content=markdown)


def _search_response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    if not isinstance(payload, dict):
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    if payload.get("success") is not True:
        return {
            "category": "provider_response",
            "semantic_result": "error",
            "response_kind": "object",
            **selected_error_fields(payload.get("error")),
        }
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("web"), list):
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    return {"semantic_result": "success", "response_kind": "object"}


class FirecrawlAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str,
        secret: SecretValue,
        http_executor: JsonRequester,
        search_limit: int = 10,
        search_highlights: bool = True,
        scrape_formats: Sequence[str] = ("markdown",),
        scrape_parsers: Sequence[str] = (),
    ) -> None:
        self.name = name
        self._api_url = api_url
        self._secret = secret
        self._http = http_executor
        self._search_limit = search_limit
        self._search_highlights = search_highlights
        self._scrape_formats = tuple(scrape_formats)
        self._scrape_parsers = tuple(scrape_parsers)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._secret.reveal()}"}

    async def search(self, query: str) -> list[KeywordSearchHit]:
        payload = await self._http.request_json(
            "POST",
            _v2_endpoint(self._api_url, "search"),
            stage="search",
            response_diagnostics=_search_response_diagnostics,
            headers=self._headers,
            json_body={
                "query": query,
                "sources": ["web"],
                "limit": self._search_limit,
                "highlights": self._search_highlights,
            },
        )
        root = self._successful_root(payload, "search")
        data = require_object(root.get("data"), self.name, "search", "data")
        web = require_list(data.get("web"), self.name, "search", "data.web")
        counts = CandidateCounts(len(web))
        hits: list[KeywordSearchHit] = []
        for item in web:
            try:
                result = require_object(item, self.name, "search", "result")
                url = non_empty_string(result.get("url"), self.name, "search", "result.url")
                hits.append(
                    KeywordSearchHit(
                        url=url,
                        title=optional_string(
                            result.get("title"), self.name, "search", "result.title"
                        ),
                        snippet=optional_string(
                            result.get("description"),
                            self.name,
                            "search",
                            "result.description",
                        ),
                        raw_content="",
                        content="",
                    )
                )
            except ExecutionFailure:
                counts.reject(candidate_rejection_reason(item, "url"))
                continue
            counts.accept()
        log_event(
            logging.getLogger(__name__),
            logging.DEBUG,
            "provider_candidates_summary",
            provider=self.name,
            stage="search",
            phase="adapter",
            exc_info=False,
            **counts.as_fields(),
        )
        return hits

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        payload = await self._http.request_json(
            "POST",
            _v2_endpoint(self._api_url, "scrape"),
            stage="fetch",
            response_diagnostics=self._fetch_response_diagnostics,
            headers=self._headers,
            json_body={
                "url": str(url),
                "formats": list(self._scrape_formats),
                "parsers": list(self._scrape_parsers),
            },
        )
        return self._parse_fetch(payload)

    def _fetch_response_diagnostics(self, response: httpx.Response) -> Mapping[str, object]:
        try:
            payload = response.json()
        except ValueError:
            return {"category": "decode", "semantic_result": "unknown"}
        try:
            self._parse_fetch(payload)
        except ExecutionFailure as exc:
            return {"category": "invalid_envelope", "semantic_result": "error", **exc.diagnostics}
        return {"response_kind": "object", "semantic_result": "success"}

    def _parse_fetch(self, payload: object) -> URLFetchCandidate:
        root = self._successful_root(payload, "fetch")
        data = require_object(root.get("data"), self.name, "fetch", "data")
        return _page_body(
            data, self.name, "fetch", pdf_parser_enabled="pdf" in self._scrape_parsers
        )

    def _successful_root(self, payload: object, stage: str) -> dict[str, object]:
        root = require_object(payload, self.name, stage, "response")
        if root.get("success") is not True:
            raise failure(
                self.name,
                stage,
                "provider reported failure",
                diagnostics=best_effort_diagnostics(
                    lambda: {
                        "category": "provider_response",
                        "semantic_result": "error",
                        **selected_error_fields(root.get("error")),
                    }
                ),
            )
        return root
