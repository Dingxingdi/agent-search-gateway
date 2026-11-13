"""Scrape.do Google Search plugin and Markdown scrape adapter."""

import logging
from collections.abc import Mapping
from urllib.parse import urlencode

import httpx

from ...errors import ExecutionFailure
from ...observability import SecretValue, log_event
from ...providers.contracts import KeywordSearchHit, URLFetchCandidate
from ...url_normalization import NormalizedURL
from ..diagnostics import CandidateCounts, best_effort_diagnostics
from .common import (
    HttpRequester,
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


def _search_response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    if not isinstance(payload, dict):
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    if payload.get("error") is not None:
        return {
            "category": "provider_response",
            "semantic_result": "error",
            "response_kind": "object",
            **selected_error_fields(payload["error"]),
        }
    if not isinstance(payload.get("organic_results"), list):
        return {
            "category": "invalid_envelope",
            "semantic_result": "unknown",
            "response_kind": "object",
        }
    return {"semantic_result": "success", "response_kind": "object"}


class ScrapeDoAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str,
        secret: SecretValue,
        http_executor: HttpRequester,
    ) -> None:
        self.name = name
        self._api_url = configured_string(api_url, "api_url").rstrip("/")
        self._secret = secret
        self._http = http_executor

    async def search(self, query: str) -> list[KeywordSearchHit]:
        params = urlencode({"token": self._secret.reveal(), "q": query})
        request_url = f"{endpoint(self._api_url, '/plugin/google/search')}?{params}"
        payload = await self._http.request_json(
            "GET", request_url, stage="search", response_diagnostics=_search_response_diagnostics
        )
        root = require_object(payload, self.name, "search", "response")
        if root.get("error") is not None:
            raise failure(
                self.name,
                "search",
                "provider reported failure",
                diagnostics=best_effort_diagnostics(lambda: selected_error_fields(root["error"])),
            )
        results = require_list(root.get("organic_results"), self.name, "search", "organic_results")
        counts = CandidateCounts(len(results))
        hits: list[KeywordSearchHit] = []
        for item in results:
            try:
                result = require_object(item, self.name, "search", "result")
                hits.append(
                    KeywordSearchHit(
                        url=non_empty_string(
                            result.get("link"), self.name, "search", "result.link"
                        ),
                        title=optional_string(
                            result.get("title"), self.name, "search", "result.title"
                        ),
                        snippet=optional_string(
                            result.get("snippet"), self.name, "search", "result.snippet"
                        ),
                    )
                )
            except ExecutionFailure:
                counts.reject(candidate_rejection_reason(item, "link"))
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
        params = urlencode({"token": self._secret.reveal(), "url": str(url), "output": "markdown"})
        request_url = f"{self._api_url}?{params}"
        text = await self._http.request_text("GET", request_url, stage="fetch")
        if not text.strip():
            raise failure(
                self.name,
                "fetch",
                "page body is empty",
                diagnostics={"semantic_result": "empty", "response_kind": "text"},
            )
        return URLFetchCandidate(text, text)
