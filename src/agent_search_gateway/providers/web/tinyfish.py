"""TinyFish search and fetch adapter using separate public endpoints."""

import logging
from collections.abc import Mapping
from urllib.parse import urlencode

import httpx

from ...errors import ExecutionFailure
from ...observability import SecretValue, log_event
from ...providers.contracts import KeywordSearchHit, URLFetchCandidate
from ...url_normalization import NormalizedURL
from ..diagnostics import CandidateCounts, safe_diagnostic_fields
from .common import (
    JsonRequester,
    candidate_rejection_reason,
    failure,
    non_empty_string,
    normalized_match,
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


class TinyFishAdapter:
    def __init__(
        self,
        *,
        name: str,
        search_api_url: str,
        fetch_api_url: str,
        secret: SecretValue,
        http_executor: JsonRequester,
    ) -> None:
        self.name = name
        self._search_api_url = search_api_url.rstrip("/")
        self._fetch_api_url = fetch_api_url.rstrip("/")
        self._secret = secret
        self._http = http_executor

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._secret.reveal()}

    async def search(self, query: str) -> list[KeywordSearchHit]:
        request_url = f"{self._search_api_url}?{urlencode({'query': query})}"
        payload = await self._http.request_json(
            "GET",
            request_url,
            stage="search",
            response_diagnostics=_search_response_diagnostics,
            headers=self._headers,
        )
        root = require_object(payload, self.name, "search", "response")
        results = require_list(root.get("results"), self.name, "search", "results")
        counts = CandidateCounts(len(results))
        hits: list[KeywordSearchHit] = []
        for item in results:
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
                            result.get("snippet"), self.name, "search", "result.snippet"
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
            self._fetch_api_url,
            stage="fetch",
            response_diagnostics=lambda response: self._fetch_response_diagnostics(response, url),
            headers=self._headers,
            json_body={
                "urls": [str(url)],
                "format": "markdown",
                "links": False,
                "image_links": False,
            },
        )
        try:
            return self._parse_fetch(payload, url)
        except ExecutionFailure as exc:
            exc.diagnostics = safe_diagnostic_fields(
                {
                    "category": "invalid_envelope",
                    "semantic_result": "error",
                    **exc.diagnostics,
                }
            )
            raise

    def _fetch_response_diagnostics(
        self, response: httpx.Response, url: NormalizedURL
    ) -> Mapping[str, object]:
        try:
            payload = response.json()
        except ValueError:
            return {"category": "decode", "semantic_result": "unknown"}
        try:
            self._parse_fetch(payload, url)
        except ExecutionFailure as exc:
            return {"category": "invalid_envelope", "semantic_result": "error", **exc.diagnostics}
        return {"response_kind": "object", "semantic_result": "success"}

    def _parse_fetch(self, payload: object, url: NormalizedURL) -> URLFetchCandidate:
        root = require_object(payload, self.name, "fetch", "response")
        results = require_list(root.get("results"), self.name, "fetch", "results")
        for item in results:
            result = require_object(item, self.name, "fetch", "result")
            if normalized_match(result.get("url"), url, self.name, "fetch"):
                text = non_empty_string(result.get("text"), self.name, "fetch", "result.text")
                return URLFetchCandidate(raw_content=text, content=text)

        errors_value = root.get("errors", [])
        errors = require_list(errors_value, self.name, "fetch", "errors")
        for item in errors:
            error = require_object(item, self.name, "fetch", "error")
            if normalized_match(error.get("url"), url, self.name, "fetch"):
                raise failure(
                    self.name,
                    "fetch",
                    "provider reported per-URL failure",
                    diagnostics=safe_diagnostic_fields(
                        {
                            "category": "provider_response",
                            "semantic_result": "error",
                            **selected_error_fields(error),
                            **selected_error_fields(error.get("error")),
                        }
                    ),
                )
        raise failure(self.name, "fetch", "matching fetch result was not returned")
