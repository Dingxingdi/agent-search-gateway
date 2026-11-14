"""Bright Data SERP and Web Unlocker adapter."""

import logging
from collections.abc import Mapping
from urllib.parse import urlencode

import httpx

from ...errors import ExecutionFailure
from ...observability import SecretValue, log_event
from ...providers.contracts import KeywordSearchHit, URLFetchCandidate
from ...url_normalization import NormalizedURL
from ..diagnostics import CandidateCounts, best_effort_diagnostics, safe_diagnostic_fields
from ..http import ProviderResponseFailure, is_retryable_http_status
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

_RECOVERABLE_SEARCH_CODES = frozenset({"captcha", "timeout", "expect_element", "expect_body"})
_SAFE_REDIRECT_REASON = "redirect location was rejected"


def _bounded_header(
    headers: httpx.Headers, name: str, limit: int | None
) -> tuple[str | None, bool]:
    """Validate one scalar; classification limits never truncate diagnostic text."""
    values = headers.get_list(name)
    if not values:
        return None, False
    if len(values) != 1:
        return None, True
    value = values[0]
    if (
        not value
        or (limit is not None and len(value) > limit)
        or not value.isascii()
        or "," in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return None, True
    return value, False


def _search_upstream_status(headers: httpx.Headers) -> tuple[int | None, bool]:
    raw_status, invalid = _bounded_header(headers, "x-brd-status-code", 3)
    if raw_status is None:
        return None, invalid
    if len(raw_status) == 3 and raw_status.isdigit() and 100 <= int(raw_status) <= 599:
        return int(raw_status), False
    return None, True


def _search_response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    status, _ = _search_upstream_status(response.headers)
    return {
        **_search_body_diagnostics(response),
        **safe_diagnostic_fields({"upstream_status": status}),
    }


def _search_body_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    if not response.content:
        return {"category": "decode", "semantic_result": "unknown", "response_kind": "empty"}
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    if isinstance(payload, dict) and payload.get("error") is not None:
        return {
            "category": "provider_response",
            "semantic_result": "error",
            "response_kind": "object",
            **selected_error_fields(payload["error"]),
        }
    if isinstance(payload, dict) and isinstance(payload.get("organic"), list):
        return {"semantic_result": "success", "response_kind": "object"}
    return {"category": "invalid_envelope", "semantic_result": "unknown"}


class BrightDataAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str,
        search_zone: str,
        fetch_zone: str,
        secret: SecretValue,
        http_executor: HttpRequester,
    ) -> None:
        self.name = name
        self._api_url = configured_string(api_url, "api_url").rstrip("/")
        self._search_zone = configured_string(search_zone, "search_zone")
        self._fetch_zone = configured_string(fetch_zone, "fetch_zone")
        self._secret = secret
        self._http = http_executor

    async def search(self, query: str) -> list[KeywordSearchHit]:
        google_url = "https://www.google.com/search?" + urlencode({"q": query, "brd_json": "1"})
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/request"),
            stage="search",
            headers={"Authorization": f"Bearer {self._secret.reveal()}"},
            json_body={"zone": self._search_zone, "url": google_url, "format": "raw"},
            response_inspector=self._inspect_search_response,
            response_diagnostics=_search_response_diagnostics,
        )
        root = require_object(payload, self.name, "search", "response")
        if root.get("error") is not None:
            raise failure(
                self.name,
                "search",
                "provider reported failure",
                diagnostics=best_effort_diagnostics(lambda: selected_error_fields(root["error"])),
            )
        results = require_list(root.get("organic"), self.name, "search", "organic")
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
                            result.get("description"), self.name, "search", "result.description"
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

    def _inspect_search_response(self, response: httpx.Response) -> ProviderResponseFailure | None:
        code, invalid_code = _bounded_header(response.headers, "x-brd-error-code", 64)
        inner_status, invalid_status = _search_upstream_status(response.headers)
        explanations = response.headers.get_list("x-brd-error")
        if not (
            code is not None
            or invalid_code
            or invalid_status
            or any(explanations)
            or (inner_status is not None and not 200 <= inner_status < 300)
        ):
            return None

        if (
            code is None
            and not invalid_code
            and not invalid_status
            and not any(explanations)
            and inner_status is not None
            and is_retryable_http_status(inner_status)
            and is_retryable_http_status(response.status_code)
        ):
            return None

        safe_code = "missing"
        if invalid_code:
            safe_code = "invalid"
        elif code is not None:
            safe_code = code if code in _RECOVERABLE_SEARCH_CODES else "unknown"
        safe_reason = "provider_reported_failure"
        if (
            len(explanations) == 1
            and len(explanations[0]) <= 160
            and explanations[0] == _SAFE_REDIRECT_REASON
        ):
            safe_reason = _SAFE_REDIRECT_REASON
        explanation, _ = _bounded_header(response.headers, "x-brd-error", None)
        diagnostics = safe_diagnostic_fields(
            {
                "upstream_status": inner_status,
                "upstream_code": code,
                "upstream_message": explanation,
            }
        )
        diagnostics["brightdata_error_code"] = safe_code
        if inner_status is not None:
            diagnostics["brightdata_status"] = inner_status
        return ProviderResponseFailure(
            self.name,
            "search",
            status_code=response.status_code,
            retryable=(
                code in _RECOVERABLE_SEARCH_CODES
                and not invalid_code
                and not invalid_status
                and response.status_code not in {401, 403}
                and inner_status not in {401, 403}
            ),
            safe_reason=safe_reason,
            diagnostics=diagnostics,
        )

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        text = await self._http.request_text(
            "POST",
            endpoint(self._api_url, "/request"),
            stage="fetch",
            headers={"Authorization": f"Bearer {self._secret.reveal()}"},
            json_body={
                "zone": self._fetch_zone,
                "url": str(url),
                "format": "raw",
                "data_format": "markdown",
            },
        )
        if not text.strip():
            raise failure(
                self.name,
                "fetch",
                "page body is empty",
                diagnostics={"semantic_result": "empty", "response_kind": "text"},
            )
        return URLFetchCandidate(text, text)
