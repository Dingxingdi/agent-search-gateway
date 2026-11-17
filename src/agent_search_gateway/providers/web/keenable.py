"""Keenable keyed web search and page fetch adapter."""

import logging
from collections.abc import Mapping
from functools import partial
from urllib.parse import urlencode

import httpx

from ...errors import ExecutionFailure, InputFailure
from ...observability import SecretValue, log_event
from ...url_normalization import NormalizedURL, normalize_url
from ..contracts import KeywordSearchHit, URLFetchCandidate
from ..diagnostics import CandidateCounts, safe_diagnostic_fields
from ..http import ProviderResponseFailure
from .common import (
    JsonRequester,
    candidate_rejection_reason,
    endpoint,
    failure,
    fetch_response_diagnostics,
    non_empty_string,
    optional_string,
    parse_fetch_with_diagnostics,
    require_list,
    require_object,
    require_string,
)

_KNOWN_ERROR_CODES = frozenset(
    {
        "invalid_json",
        "invalid_parameter",
        "invalid_request",
        "missing_parameter",
        "unknown_parameter",
        "query_rejected",
        "search_rejected_upstream",
        "missing_api_key",
        "invalid_api_key",
        "invalid_authorization_header",
        "api_key_required",
        "insufficient_credits",
        "credentials_forbidden",
        "forbidden",
        "search_failed_upstream",
        "search_rate_limited",
        "rate_limited",
        "internal_error",
        "search_upstream_error",
        "search_overloaded",
        "content_not_extracted",
        "target_timeout",
    }
)


def _error_code(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if error is None or error is False or error == "":
        return None
    code = payload.get("code")
    return code if isinstance(code, str) and code in _KNOWN_ERROR_CODES else "unknown"


def _error_diagnostics(code: str) -> dict[str, object]:
    return safe_diagnostic_fields(
        {
            "category": "provider_response",
            "semantic_result": "error",
            "response_kind": "object",
            "upstream_code": code,
        }
    )


def _search_response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    code = _error_code(payload)
    if code is not None:
        return _error_diagnostics(code)
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return {"semantic_result": "success", "response_kind": "object"}
    return {"category": "invalid_envelope", "semantic_result": "unknown"}


class KeenableAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str = "https://api.keenable.ai",
        secret: SecretValue,
        http_executor: JsonRequester,
    ) -> None:
        self.name = name
        self._api_url = api_url
        self._secret = secret
        self._http = http_executor

    async def search(self, query: str) -> list[KeywordSearchHit]:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/v1/search"),
            stage="search",
            headers={"X-API-Key": self._secret.reveal()},
            json_body={"query": query, "max_results": 10},
            response_inspector=lambda response: self._inspect_response(response, "search"),
            response_diagnostics=_search_response_diagnostics,
        )
        root = require_object(payload, self.name, "search", "response")
        code = _error_code(root)
        if code is not None:
            raise failure(
                self.name,
                "search",
                "provider reported failure",
                diagnostics=_error_diagnostics(code),
            )
        results = require_list(root.get("results"), self.name, "search", "results")
        hits: list[KeywordSearchHit] = []
        counts = CandidateCounts(len(results))
        for item in results:
            try:
                record = require_object(item, self.name, "search", "result")
                url = non_empty_string(record.get("url"), self.name, "search", "result.url")
                normalize_url(url)
                hits.append(
                    KeywordSearchHit(
                        url,
                        require_string(record.get("title"), self.name, "search", "result.title"),
                        optional_string(
                            record.get("snippet"), self.name, "search", "result.snippet"
                        ),
                    )
                )
            except (ExecutionFailure, InputFailure):
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
        request_url = f"{endpoint(self._api_url, '/v1/fetch')}?{urlencode({'url': str(url)})}"
        payload = await self._http.request_json(
            "GET",
            request_url,
            stage="fetch",
            headers={"X-API-Key": self._secret.reveal()},
            response_inspector=lambda response: self._inspect_response(response, "fetch"),
            response_diagnostics=partial(fetch_response_diagnostics, parse=self._parse_fetch),
        )
        return parse_fetch_with_diagnostics(payload, self._parse_fetch)

    def _parse_fetch(self, payload: object) -> URLFetchCandidate:
        root = require_object(payload, self.name, "fetch", "response")
        code = _error_code(root)
        if code is not None:
            raise failure(
                self.name,
                "fetch",
                "provider reported failure",
                diagnostics=_error_diagnostics(code),
            )
        returned_url = non_empty_string(root.get("url"), self.name, "fetch", "url")
        try:
            normalize_url(returned_url)
        except InputFailure:
            raise failure(self.name, "fetch", "response URL is invalid") from None
        value = root.get("content")
        if value is None or (isinstance(value, str) and not value.strip()):
            raise failure(
                self.name,
                "fetch",
                "page body is empty",
                diagnostics={"semantic_result": "empty", "response_kind": "object"},
            )
        content = require_string(value, self.name, "fetch", "content")
        return URLFetchCandidate(content, content)

    def _inspect_response(
        self, response: httpx.Response, stage: str
    ) -> ProviderResponseFailure | None:
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        code = _error_code(payload)
        if code is None:
            return None
        return ProviderResponseFailure(
            self.name,
            stage,
            status_code=response.status_code,
            retryable=False,
            safe_reason="provider reported failure",
            diagnostics=_error_diagnostics(code),
        )
