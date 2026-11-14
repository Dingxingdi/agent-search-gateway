"""Linkup search and fetch adapter."""

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


def _search_response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    return {"semantic_result": "success", "response_kind": "object"}


class LinkupAdapter:
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
            endpoint(self._api_url, "/v1/search"),
            stage="search",
            response_diagnostics=_search_response_diagnostics,
            headers=self._headers,
            json_body={"q": query, "depth": "standard", "outputType": "searchResults"},
        )
        root = require_object(payload, self.name, "search", "response")
        results = require_list(root.get("results"), self.name, "search", "results")
        counts = CandidateCounts(len(results))
        hits: list[KeywordSearchHit] = []
        for item in results:
            try:
                result = require_object(item, self.name, "search", "result")
                hits.append(
                    KeywordSearchHit(
                        url=non_empty_string(result.get("url"), self.name, "search", "result.url"),
                        title=non_empty_string(
                            result.get("name"), self.name, "search", "result.name"
                        ),
                        snippet=non_empty_string(
                            result.get("content"), self.name, "search", "result.content"
                        ),
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
            endpoint(self._api_url, "/v1/fetch"),
            stage="fetch",
            response_diagnostics=self._fetch_response_diagnostics,
            headers=self._headers,
            json_body={
                "url": str(url),
                "includeRawContent": True,
                "extractImages": False,
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
        root = require_object(payload, self.name, "fetch", "response")
        markdown = optional_string(root.get("markdown"), self.name, "fetch", "markdown")
        raw_content = optional_string(root.get("rawContent"), self.name, "fetch", "rawContent")
        raw = raw_content or markdown
        if not raw.strip():
            raise failure(
                self.name,
                "fetch",
                "page body is empty",
                diagnostics=best_effort_diagnostics(
                    lambda: {
                        "category": "provider_response"
                        if root.get("error") is not None
                        else "empty_output",
                        "semantic_result": "error" if root.get("error") is not None else "empty",
                        **selected_error_fields(root.get("error")),
                    }
                ),
            )
        return URLFetchCandidate(raw_content=raw, content=markdown)
