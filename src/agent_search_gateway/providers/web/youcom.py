"""You.com web provider adapter."""

import logging
from collections.abc import Mapping

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
    normalized_match,
    optional_string,
    parse_fetch_with_diagnostics,
    require_list,
    require_object,
    require_string,
)

_KNOWN_ERROR_CODES = frozenset(
    {
        "unauthorized",
        "payment_required",
        "forbidden",
        "invalid_request",
        "spend_cap_exceeded",
    }
)


def _error_code(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        if error in _KNOWN_ERROR_CODES:
            return error
        code = payload.get("code")
        return code if isinstance(code, str) and code in _KNOWN_ERROR_CODES else "unknown"
    if isinstance(error, dict) and ("code" in error or "message" in error):
        code = error.get("code")
        return code if isinstance(code, str) and code in _KNOWN_ERROR_CODES else "unknown"
    results = payload.get("results")
    has_results = isinstance(results, dict) and isinstance(results.get("web"), list)
    detail = payload.get("detail")
    if not has_results and isinstance(detail, str) and detail.strip():
        return "unknown"
    return None


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
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, dict) and isinstance(results.get("web"), list):
            return {"semantic_result": "success", "response_kind": "object"}
    return {"category": "invalid_envelope", "semantic_result": "unknown"}


class YouComAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str = "https://ydc-index.io",
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
            json_body={"query": query, "count": 10},
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
        results = require_object(root.get("results"), self.name, "search", "results")
        web = require_list(results.get("web"), self.name, "search", "results.web")
        hits: list[KeywordSearchHit] = []
        counts = CandidateCounts(len(web))
        for item in web:
            try:
                record = require_object(item, self.name, "search", "result")
                url = non_empty_string(record.get("url"), self.name, "search", "result.url")
                normalize_url(url)
                snippets = require_list(
                    [] if record.get("snippets") is None else record["snippets"],
                    self.name,
                    "search",
                    "result.snippets",
                )
                hits.append(
                    KeywordSearchHit(
                        url=url,
                        title=optional_string(
                            record.get("title"), self.name, "search", "result.title"
                        ),
                        snippet="\n\n".join(
                            require_string(value, self.name, "search", "result.snippets[]")
                            for value in snippets
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

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/v1/contents"),
            stage="fetch",
            headers={"X-API-Key": self._secret.reveal()},
            json_body={"urls": [str(url)], "formats": ["markdown"]},
            response_diagnostics=lambda response: self._fetch_response_diagnostics(response, url),
            response_inspector=lambda response: self._inspect_response(response, "fetch"),
        )
        return parse_fetch_with_diagnostics(payload, lambda value: self._parse_fetch(value, url))

    def _parse_fetch(self, payload: object, target: NormalizedURL) -> URLFetchCandidate:
        code = _error_code(payload)
        if code is not None:
            raise failure(
                self.name,
                "fetch",
                "provider reported failure",
                diagnostics=_error_diagnostics(code),
            )
        results = require_list(payload, self.name, "fetch", "response")
        empty_match_seen = False
        for item in results:
            try:
                record = require_object(item, self.name, "fetch", "result")
                if not normalized_match(record.get("url"), target, self.name, "fetch"):
                    continue
            except ExecutionFailure:
                continue
            markdown = record.get("markdown")
            if isinstance(markdown, str) and markdown.strip():
                return URLFetchCandidate(markdown, markdown)
            empty_match_seen = True
        raise failure(
            self.name,
            "fetch",
            "matching markdown is empty" if empty_match_seen else "no matching result",
            diagnostics={
                "response_kind": "array",
                "semantic_result": "empty" if empty_match_seen else "no_match",
            },
        )

    def _fetch_response_diagnostics(
        self, response: httpx.Response, target: NormalizedURL
    ) -> Mapping[str, object]:
        fields = dict(
            fetch_response_diagnostics(response, lambda value: self._parse_fetch(value, target))
        )
        if fields.get("semantic_result") == "success":
            fields["response_kind"] = "array"
        return fields
