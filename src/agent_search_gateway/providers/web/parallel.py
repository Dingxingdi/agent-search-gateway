"""Parallel Search and Extract adapter."""

import logging
from collections.abc import Mapping

import httpx

from ...errors import ExecutionFailure
from ...observability import SecretValue, log_event
from ...providers.contracts import KeywordSearchHit, URLFetchCandidate
from ...url_normalization import NormalizedURL
from ..diagnostics import CandidateCounts, safe_diagnostic_fields
from .common import (
    JsonRequester,
    candidate_rejection_reason,
    endpoint,
    failure,
    non_empty_string,
    normalized_match,
    optional_string,
    require_list,
    require_object,
    require_string,
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


_VALID_MODES = frozenset({"turbo", "fast", "basic", "advanced"})
_FETCH_POLICY_KEYS = frozenset({"max_age_seconds", "timeout_seconds", "disable_cache_fallback"})
_MIN_MAX_AGE_SECONDS = 600


def _validate_mode(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or value not in _VALID_MODES):
        raise TypeError("mode must be turbo, fast, basic, advanced, or None")
    return value


def _validate_fetch_policy(
    value: Mapping[str, object] | None,
    *,
    label: str,
) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    if set(value) - _FETCH_POLICY_KEYS:
        raise TypeError(f"{label} contains unsupported fields")

    if "max_age_seconds" in value:
        max_age_seconds = value["max_age_seconds"]
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, int)
            or max_age_seconds < _MIN_MAX_AGE_SECONDS
        ):
            raise TypeError(f"{label}.max_age_seconds must be an integer >= 600")

    if "timeout_seconds" in value:
        timeout_seconds = value["timeout_seconds"]
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise TypeError(f"{label}.timeout_seconds must be numeric")

    if "disable_cache_fallback" in value:
        disable_cache_fallback = value["disable_cache_fallback"]
        if not isinstance(disable_cache_fallback, bool):
            raise TypeError(f"{label}.disable_cache_fallback must be boolean")

    return dict(value)


class ParallelAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str,
        secret: SecretValue,
        http_executor: JsonRequester,
        mode: str | None = None,
        search_fetch_policy: Mapping[str, object] | None = None,
        extract_fetch_policy: Mapping[str, object] | None = None,
    ) -> None:
        self.name = name
        self._api_url = api_url
        self._secret = secret
        self._http = http_executor
        self._mode = _validate_mode(mode)
        self._search_fetch_policy = _validate_fetch_policy(
            search_fetch_policy,
            label="search_fetch_policy",
        )
        self._extract_fetch_policy = _validate_fetch_policy(
            extract_fetch_policy,
            label="extract_fetch_policy",
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._secret.reveal()}

    async def search(self, query: str) -> list[KeywordSearchHit]:
        request_body: dict[str, object] = {"search_queries": [query]}
        if self._mode is not None:
            request_body["mode"] = self._mode
        if self._search_fetch_policy is not None:
            request_body["advanced_settings"] = {"fetch_policy": dict(self._search_fetch_policy)}

        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/v1/search"),
            stage="search",
            response_diagnostics=_search_response_diagnostics,
            headers=self._headers,
            json_body=request_body,
        )
        root = require_object(payload, self.name, "search", "response")
        results = require_list(root.get("results"), self.name, "search", "results")
        counts = CandidateCounts(len(results))
        hits: list[KeywordSearchHit] = []
        for item in results:
            try:
                result = require_object(item, self.name, "search", "result")
                url = non_empty_string(result.get("url"), self.name, "search", "result.url")
                title = optional_string(result.get("title"), self.name, "search", "result.title")
                excerpts = require_list(
                    result.get("excerpts"), self.name, "search", "result.excerpts"
                )
                snippet = "\n\n".join(
                    require_string(excerpt, self.name, "search", "result.excerpts[]")
                    for excerpt in excerpts
                )
                hits.append(KeywordSearchHit(url=url, title=title, snippet=snippet))
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
        advanced_settings: dict[str, object] = {"full_content": True}
        if self._extract_fetch_policy is not None:
            advanced_settings["fetch_policy"] = dict(self._extract_fetch_policy)

        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/v1/extract"),
            stage="fetch",
            response_diagnostics=lambda response: self._fetch_response_diagnostics(response, url),
            headers=self._headers,
            json_body={
                "urls": [str(url)],
                "advanced_settings": advanced_settings,
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
        errors = require_list(root.get("errors"), self.name, "fetch", "errors")
        for item in results:
            result = require_object(item, self.name, "fetch", "result")
            if normalized_match(result.get("url"), url, self.name, "fetch"):
                full_content = non_empty_string(
                    result.get("full_content"), self.name, "fetch", "result.full_content"
                )
                return URLFetchCandidate(
                    raw_content=full_content,
                    content=full_content,
                )
        for item in errors:
            provider_error = require_object(item, self.name, "fetch", "error")
            if normalized_match(provider_error.get("url"), url, self.name, "fetch"):
                raise failure(
                    self.name,
                    "fetch",
                    "provider reported extraction failure",
                    diagnostics=safe_diagnostic_fields(
                        {
                            "category": "provider_response",
                            "semantic_result": "error",
                            **selected_error_fields(provider_error),
                            **selected_error_fields(provider_error.get("error")),
                        }
                    ),
                )
        raise failure(self.name, "fetch", "matching extraction result was not returned")
