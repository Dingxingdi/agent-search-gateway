"""ScrapeGraphAI v2 Search and Scrape adapter."""

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
    configured_string,
    endpoint,
    failure,
    non_empty_string,
    optional_string,
    require_list,
    require_object,
    selected_error_fields,
)


def _search_error_fields(root: Mapping[str, object]) -> Mapping[str, object]:
    if root.get("error") is not None:
        return selected_error_fields(root["error"])
    detail = root.get("detail")
    return selected_error_fields(detail) if isinstance(detail, str) else {}


def _search_response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    if not isinstance(payload, dict):
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    if payload.get("error") is not None or payload.get("detail") is not None:
        return {
            "category": "provider_response",
            "semantic_result": "error",
            "response_kind": "object",
            **_search_error_fields(payload),
        }
    if not isinstance(payload.get("results"), list):
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    return {"semantic_result": "success", "response_kind": "object"}


class ScrapeGraphAIAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str,
        secret: SecretValue,
        http_executor: JsonRequester,
    ) -> None:
        self.name = name
        self._api_url = configured_string(api_url, "api_url").rstrip("/")
        self._secret = secret
        self._http = http_executor

    @property
    def _headers(self) -> dict[str, str]:
        return {"SGAI-APIKEY": self._secret.reveal()}

    async def search(self, query: str) -> list[KeywordSearchHit]:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/api/search"),
            stage="search",
            response_diagnostics=_search_response_diagnostics,
            headers=self._headers,
            json_body={"query": query},
        )
        root = require_object(payload, self.name, "search", "response")
        if root.get("error") is not None or root.get("detail") is not None:
            raise failure(
                self.name,
                "search",
                "provider reported failure",
                diagnostics=best_effort_diagnostics(lambda: _search_error_fields(root)),
            )
        results = require_list(root.get("results"), self.name, "search", "results")
        counts = CandidateCounts(len(results))
        hits: list[KeywordSearchHit] = []
        for item in results:
            try:
                result = require_object(item, self.name, "search", "result")
                content = optional_string(
                    result.get("content"), self.name, "search", "result.content"
                )
                hits.append(
                    KeywordSearchHit(
                        url=non_empty_string(result.get("url"), self.name, "search", "result.url"),
                        title=optional_string(
                            result.get("title"), self.name, "search", "result.title"
                        ),
                        snippet="",
                        raw_content=content,
                        content=content,
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
            endpoint(self._api_url, "/api/scrape"),
            stage="fetch",
            headers=self._headers,
            json_body={"url": str(url), "formats": [{"type": "markdown"}]},
        )
        root = require_object(payload, self.name, "fetch", "response")
        if root.get("error") is not None or root.get("detail") is not None:
            raise failure(self.name, "fetch", "provider reported failure")
        results = require_object(root.get("results"), self.name, "fetch", "results")
        markdown = require_object(results.get("markdown"), self.name, "fetch", "results.markdown")
        data = require_list(markdown.get("data"), self.name, "fetch", "results.markdown.data")
        if not data:
            raise failure(self.name, "fetch", "page body is empty")
        text = non_empty_string(data[0], self.name, "fetch", "results.markdown.data[0]")
        return URLFetchCandidate(text, text)
