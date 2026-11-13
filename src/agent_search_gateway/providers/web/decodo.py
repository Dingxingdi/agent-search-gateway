"""Decodo Google Search template and Markdown scrape adapter."""

import logging
from collections.abc import Mapping

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
    return _search_payload_diagnostics(payload)


def _search_payload_diagnostics(payload: object) -> dict[str, object]:
    fields: dict[str, object] = {"category": "invalid_envelope", "semantic_result": "unknown"}
    if not isinstance(payload, dict):
        return fields
    pages = payload.get("results")
    if not isinstance(pages, list) or not pages or not isinstance(pages[0], dict):
        return fields
    page = pages[0]
    fields["upstream_status"] = page.get("status_code")
    if page.get("status_code") != 200 and type(page.get("status_code")) in (str, int):
        fields.update(category="provider_response", semantic_result="error", response_kind="object")
    content = page.get("content")
    if not isinstance(content, dict):
        return fields
    errors = content.get("errors", [])
    if not isinstance(errors, list):
        return fields
    if errors:
        fields.update(category="provider_response", semantic_result="error", response_kind="object")
        # Select only the first error's known scalar fields; never expand the list.
        fields.update(selected_error_fields(errors[0]))
        return fields
    if page.get("status_code") != 200:
        return fields
    wrapper = content.get("results")
    if not isinstance(wrapper, dict):
        return fields
    parsed = wrapper.get("results")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("organic"), list):
        return fields
    fields.pop("category")
    fields.update(semantic_result="success", response_kind="object")
    return fields


class DecodoAdapter:
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

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Basic {self._secret.reveal()}"}

    async def search(self, query: str) -> list[KeywordSearchHit]:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/v2/scrape"),
            stage="search",
            response_diagnostics=_search_response_diagnostics,
            headers=self._headers,
            json_body={"target": "google_search", "query": query, "parse": True},
        )
        try:
            results = self._organic_results(payload)
        except ExecutionFailure as exc:
            exc.diagnostics = best_effort_diagnostics(lambda: _search_payload_diagnostics(payload))
            raise
        counts = CandidateCounts(len(results))
        hits: list[KeywordSearchHit] = []
        for item in results:
            try:
                result = require_object(item, self.name, "search", "result")
                hits.append(
                    KeywordSearchHit(
                        url=non_empty_string(result.get("url"), self.name, "search", "result.url"),
                        title=optional_string(
                            result.get("title"), self.name, "search", "result.title"
                        ),
                        snippet=optional_string(
                            result.get("desc"), self.name, "search", "result.desc"
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
        text = await self._http.request_text(
            "POST",
            endpoint(self._api_url, "/v2/scrape"),
            stage="fetch",
            headers=self._headers,
            json_body={"url": str(url), "markdown": True},
        )
        if not text.strip():
            raise failure(
                self.name,
                "fetch",
                "page body is empty",
                diagnostics={"semantic_result": "empty", "response_kind": "text"},
            )
        return URLFetchCandidate(text, text)

    def _organic_results(self, payload: object) -> list[object]:
        root = require_object(payload, self.name, "search", "response")
        pages = require_list(root.get("results"), self.name, "search", "results")
        if not pages:
            raise failure(self.name, "search", "results must contain a parsed page")
        page = require_object(pages[0], self.name, "search", "result page")
        if page.get("status_code") != 200:
            raise failure(self.name, "search", "provider reported failure")
        content = require_object(page.get("content"), self.name, "search", "content")
        errors = require_list(content.get("errors", []), self.name, "search", "content.errors")
        if errors:
            raise failure(self.name, "search", "provider reported failure")
        parsed_wrapper = require_object(
            content.get("results"), self.name, "search", "content.results"
        )
        parsed = require_object(
            parsed_wrapper.get("results"), self.name, "search", "content.results.results"
        )
        return require_list(parsed.get("organic"), self.name, "search", "organic")
