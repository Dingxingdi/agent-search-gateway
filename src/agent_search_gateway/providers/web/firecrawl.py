"""Firecrawl v2 search and scrape adapter."""

import logging
from collections.abc import Mapping

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
) -> URLFetchCandidate:
    markdown = optional_string(result.get("markdown"), provider, stage, "markdown")
    raw_html = optional_string(result.get("rawHtml"), provider, stage, "rawHtml")
    raw = raw_html or markdown
    if not raw.strip():
        raise failure(provider, stage, "page body is empty")
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
    ) -> None:
        self.name = name
        self._api_url = api_url
        self._secret = secret
        self._http = http_executor

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
                "scrapeOptions": {"formats": ["markdown", "rawHtml"]},
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
                markdown = optional_string(
                    result.get("markdown"), self.name, "search", "result.markdown"
                )
                raw_html = optional_string(
                    result.get("rawHtml"), self.name, "search", "result.rawHtml"
                )
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
                        raw_content=raw_html or markdown,
                        content=markdown,
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
            headers=self._headers,
            json_body={"url": str(url), "formats": ["markdown", "rawHtml"]},
        )
        root = self._successful_root(payload, "fetch")
        data = require_object(root.get("data"), self.name, "fetch", "data")
        return _page_body(data, self.name, "fetch")

    def _successful_root(self, payload: object, stage: str) -> dict[str, object]:
        root = require_object(payload, self.name, stage, "response")
        if root.get("success") is not True:
            raise failure(
                self.name,
                stage,
                "provider reported failure",
                diagnostics=best_effort_diagnostics(
                    lambda: selected_error_fields(root.get("error"))
                )
                if stage == "search"
                else None,
            )
        return root
