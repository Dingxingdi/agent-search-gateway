"""open-webSearch daemon HTTP adapter."""

import logging
from collections.abc import Mapping

import httpx

from ...errors import ExecutionFailure, InputFailure
from ...observability import log_event
from ...url_normalization import NormalizedURL, normalize_url
from ..contracts import KeywordSearchHit, URLFetchCandidate
from ..diagnostics import CandidateCounts, safe_diagnostic_fields
from ..http import ProviderResponseFailure
from .common import (
    JsonRequester,
    candidate_rejection_reason,
    configured_string,
    endpoint,
    failure,
    fetch_response_diagnostics,
    non_empty_string,
    optional_string,
    parse_fetch_with_diagnostics,
    require_object,
    require_string,
)

_KNOWN_ERROR_CODES = frozenset(
    {"invalid_request", "engine_error", "browser_unavailable", "validation_failed", "fetch_failed"}
)


def _error_diagnostics(payload: dict[str, object]) -> dict[str, object]:
    error = payload.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return safe_diagnostic_fields(
        {
            "category": "provider_response",
            "semantic_result": "error",
            "response_kind": "object",
            "upstream_code": code
            if isinstance(code, str) and code in _KNOWN_ERROR_CODES
            else "unknown",
        }
    )


def _search_response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    if isinstance(payload, dict) and payload.get("status") == "error":
        return _error_diagnostics(payload)
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    fields: dict[str, object] = {
        "category": "provider_response",
        "response_kind": "object",
        "semantic_result": "success",
    }
    failures = data.get("partialFailures")
    if isinstance(failures, list) and failures:
        fields["upstream_code"] = "partial_engine_failures"
        fields["upstream_message"] = f"partial_engine_failures count={len(failures)}"
    return safe_diagnostic_fields(fields)


class OpenWebSearchAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str = "http://127.0.0.1:3210",
        http_executor: JsonRequester,
        engines: list[str] | None = None,
    ) -> None:
        self.name = name
        self._api_url = configured_string(api_url, "api_url")
        self._http = http_executor
        if engines is not None and (
            not isinstance(engines, list)
            or not engines
            or any(not isinstance(item, str) or not item.strip() for item in engines)
        ):
            raise TypeError("engines must be a non-empty list of non-blank strings or None")
        self._engines = tuple(engines) if engines is not None else None

    async def search(self, query: str) -> list[KeywordSearchHit]:
        body: dict[str, object] = {"query": query}
        if self._engines is not None:
            body["engines"] = list(self._engines)
            body["limit"] = 50
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/search"),
            stage="search",
            json_body=body,
            response_inspector=lambda response: self._inspect_response(response, "search"),
            response_diagnostics=_search_response_diagnostics,
        )
        data = self._response_data(payload, "search")
        results = data.get("results")
        if not isinstance(results, list):
            raise failure(
                self.name,
                "search",
                "data.results must be an array",
                diagnostics={"category": "invalid_envelope", "semantic_result": "unknown"},
            )
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
                            record.get("description"), self.name, "search", "result.description"
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

    async def fetch(self, target: NormalizedURL) -> URLFetchCandidate:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/fetch-web"),
            stage="fetch",
            json_body={"url": str(target)},
            response_inspector=lambda response: self._inspect_response(response, "fetch"),
            response_diagnostics=lambda response: fetch_response_diagnostics(
                response, self._parse_fetch
            ),
        )
        return parse_fetch_with_diagnostics(payload, self._parse_fetch)

    def _parse_fetch(self, payload: object) -> URLFetchCandidate:
        data = self._response_data(payload, "fetch")
        content = data.get("content")
        if content is None or (isinstance(content, str) and not content.strip()):
            raise failure(
                self.name,
                "fetch",
                "data.content must be non-empty",
                diagnostics={"semantic_result": "empty"},
            )
        content = non_empty_string(content, self.name, "fetch", "data.content")
        return URLFetchCandidate(content, content)

    def _response_data(self, payload: object, stage: str) -> dict[str, object]:
        invalid = {"category": "invalid_envelope", "semantic_result": "unknown"}
        if not isinstance(payload, dict):
            raise failure(self.name, stage, "response must be an object", diagnostics=invalid)
        if payload.get("status") == "error":
            raise failure(
                self.name,
                stage,
                "provider reported failure",
                diagnostics=_error_diagnostics(payload),
            )
        if payload.get("status") != "ok":
            raise failure(self.name, stage, "response status must be ok", diagnostics=invalid)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise failure(self.name, stage, "data must be an object", diagnostics=invalid)
        return data

    def _inspect_response(
        self, response: httpx.Response, stage: str
    ) -> ProviderResponseFailure | None:
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict) or payload.get("status") != "error":
            return None
        return ProviderResponseFailure(
            self.name,
            stage,
            status_code=response.status_code,
            retryable=False,
            safe_reason="provider reported failure",
            diagnostics=_error_diagnostics(payload),
        )
