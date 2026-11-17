"""Doubao web provider adapter."""

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
    endpoint,
    failure,
    fetch_response_diagnostics,
    non_empty_string,
    optional_string,
    parse_fetch_with_diagnostics,
    require_list,
    require_object,
)

_KNOWN_ERROR_CODES = frozenset(
    {
        "10400",
        "10401",
        "10402",
        "10403",
        "10406",
        "10407",
        "10408",
        "10409",
        "10410",
        "10412",
        "10413",
        "10500",
        "10502",
        "700429",
    }
)
_SEARCH_RETRYABLE_CODES = frozenset({"700429", "10500"})
_FETCH_RETRYABLE_CODES = frozenset({"700429", "10500", "10502"})


def _metadata_error(payload: object) -> object:
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("ResponseMetadata")
    return metadata.get("Error") if isinstance(metadata, dict) else None


def _error_diagnostics(error: object) -> dict[str, object]:
    code = _error_code(error)
    return safe_diagnostic_fields(
        {
            "category": "provider_response",
            "semantic_result": "error",
            "response_kind": "object",
            "upstream_code": code if code in _KNOWN_ERROR_CODES else "unknown",
        }
    )


def _search_response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    error = _metadata_error(payload)
    if error:
        return _error_diagnostics(error)
    if isinstance(payload, dict):
        metadata_valid = "ResponseMetadata" not in payload or isinstance(
            payload["ResponseMetadata"], dict
        )
        result = payload.get("Result")
        if (
            metadata_valid
            and isinstance(result, dict)
            and isinstance(result.get("WebResults"), list)
        ):
            return {"semantic_result": "success", "response_kind": "object"}
    return {"category": "invalid_envelope", "semantic_result": "unknown", "response_kind": "object"}


def _rejection_reason(item: object) -> str:
    if isinstance(item, dict):
        url = item.get("Url")
        if url is None or (isinstance(url, str) and not url.strip()):
            return "missing_url"
    return "invalid_record"


def _error_code(error: object) -> str:
    if not isinstance(error, dict):
        return "unknown"
    code = error.get("Code")
    number = error.get("CodeN")
    if "Code" in error and (not isinstance(code, str) or not code.isascii() or not code.isdigit()):
        return "unknown"
    if "CodeN" in error and type(number) is not int:
        return "unknown"
    if "Code" in error and "CodeN" in error and code != str(number):
        return "unknown"
    if isinstance(code, str):
        return code
    return str(number) if type(number) is int else "unknown"


class DoubaoAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str = "https://open.feedcoopapi.com/search_api",
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
            endpoint(self._api_url, "/web_search"),
            stage="search",
            headers={"Authorization": f"Bearer {self._secret.reveal()}"},
            json_body={"Query": query, "SearchType": "web"},
            response_inspector=self._inspect_search_response,
            response_diagnostics=_search_response_diagnostics,
        )
        root = require_object(payload, self.name, "search", "response")
        if "ResponseMetadata" in root:
            require_object(root["ResponseMetadata"], self.name, "search", "ResponseMetadata")
        error = _metadata_error(root)
        if error:
            raise failure(
                self.name,
                "search",
                "provider reported failure",
                diagnostics=_error_diagnostics(error),
            )
        result = require_object(root.get("Result"), self.name, "search", "Result")
        results = require_list(result.get("WebResults"), self.name, "search", "WebResults")
        hits: list[KeywordSearchHit] = []
        counts = CandidateCounts(len(results))
        for item in results:
            try:
                record = require_object(item, self.name, "search", "result")
                url = non_empty_string(record.get("Url"), self.name, "search", "Url")
                normalize_url(url)
                summary = optional_string(record.get("Summary"), self.name, "search", "Summary")
                snippet = (
                    summary
                    if summary.strip()
                    else optional_string(record.get("Snippet"), self.name, "search", "Snippet")
                )
                hits.append(
                    KeywordSearchHit(
                        url,
                        optional_string(record.get("Title"), self.name, "search", "Title"),
                        snippet,
                    )
                )
            except (ExecutionFailure, InputFailure):
                counts.reject(_rejection_reason(item))
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

    def _inspect_search_response(self, response: httpx.Response) -> ProviderResponseFailure | None:
        return self._inspect_response(response, "search", _SEARCH_RETRYABLE_CODES)

    async def fetch(self, target: NormalizedURL) -> URLFetchCandidate:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/fetch"),
            stage="fetch",
            headers={"Authorization": f"Bearer {self._secret.reveal()}"},
            json_body={"Url": str(target)},
            response_inspector=self._inspect_fetch_response,
            response_diagnostics=self._fetch_response_diagnostics,
        )
        return parse_fetch_with_diagnostics(payload, self._parse_fetch)

    def _parse_fetch(self, payload: object) -> URLFetchCandidate:
        root = require_object(payload, self.name, "fetch", "response")
        if "ResponseMetadata" in root:
            require_object(root["ResponseMetadata"], self.name, "fetch", "ResponseMetadata")
        error = _metadata_error(root)
        if error:
            raise failure(
                self.name,
                "fetch",
                "provider reported failure",
                diagnostics=_error_diagnostics(error),
            )
        result = require_object(root.get("Result"), self.name, "fetch", "Result")
        data = require_object(result.get("Data"), self.name, "fetch", "Data")
        status_code = data.get("StatusCode")
        if type(status_code) is not int or not 200 <= status_code < 300:
            raise failure(self.name, "fetch", "origin status must be an integer 2xx")
        content = data.get("ContentText")
        if content is None or (isinstance(content, str) and not content.strip()):
            raise failure(
                self.name,
                "fetch",
                "ContentText must be non-empty",
                diagnostics={"semantic_result": "empty"},
            )
        content = non_empty_string(content, self.name, "fetch", "ContentText")
        return URLFetchCandidate(raw_content=content, content=content)

    def _fetch_response_diagnostics(self, response: httpx.Response) -> Mapping[str, object]:
        return {
            "response_kind": "object",
            **fetch_response_diagnostics(response, self._parse_fetch),
        }

    def _inspect_fetch_response(self, response: httpx.Response) -> ProviderResponseFailure | None:
        return self._inspect_response(response, "fetch", _FETCH_RETRYABLE_CODES)

    def _inspect_response(
        self, response: httpx.Response, stage: str, retryable_codes: frozenset[str]
    ) -> ProviderResponseFailure | None:
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        error = _metadata_error(payload)
        if not error:
            return None
        return ProviderResponseFailure(
            self.name,
            stage,
            status_code=200,
            retryable=_error_code(error) in retryable_codes,
            safe_reason="provider_reported_failure",
            diagnostics=_error_diagnostics(error),
        )
