"""Yuanbao service-key web search adapter."""

import json
import logging

import httpx

from ...errors import ExecutionFailure
from ...observability import SecretValue, log_event
from ..contracts import KeywordSearchHit
from ..diagnostics import CandidateCounts, best_effort_diagnostics, safe_diagnostic_fields
from .common import (
    JsonRequester,
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

_LOGGER = logging.getLogger(__name__)


def _error_diagnostics(error: object) -> dict[str, object]:
    return best_effort_diagnostics(
        lambda: selected_error_fields(
            {"code": error.get("Code"), "message": error.get("Message")}
            if isinstance(error, dict)
            else {}
        )
    )


def _response_diagnostics(response: httpx.Response) -> dict[str, object]:
    """Classify known envelope fields without changing business or retry handling."""
    try:
        payload = response.json()
    except ValueError:
        return safe_diagnostic_fields({"category": "decode", "semantic_result": "unknown"})
    invalid_envelope = {"category": "invalid_envelope", "semantic_result": "unknown"}
    if not isinstance(payload, dict):
        return safe_diagnostic_fields(invalid_envelope)
    envelope = payload.get("Response")
    if not isinstance(envelope, dict):
        return safe_diagnostic_fields(invalid_envelope)
    if "Error" in envelope:
        return safe_diagnostic_fields(
            {
                "category": "provider_response",
                "semantic_result": "error",
                "response_kind": "object",
                **_error_diagnostics(envelope["Error"]),
            }
        )
    if not isinstance(envelope.get("Pages"), list):
        return safe_diagnostic_fields(invalid_envelope)
    return safe_diagnostic_fields({"semantic_result": "success", "response_kind": "object"})


class YuanbaoAdapter:
    def __init__(
        self,
        *,
        name: str,
        secret: SecretValue,
        http_executor: JsonRequester,
        api_url: str = "https://api.wsa.cloud.tencent.com",
    ) -> None:
        self.name = name
        self._api_url = configured_string(api_url, "api_url")
        self._secret = secret
        self._http = http_executor

    async def search(self, query: str) -> list[KeywordSearchHit]:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/SearchPro"),
            stage="search",
            headers={"Authorization": f"Bearer {self._secret.reveal()}"},
            json_body={"Query": query},
            response_diagnostics=_response_diagnostics,
        )
        root = require_object(payload, self.name, "search", "response")
        response = require_object(root.get("Response"), self.name, "search", "Response")
        if "Error" in response:
            error = response["Error"]
            raise failure(
                self.name,
                "search",
                "provider reported failure",
                diagnostics=_error_diagnostics(error),
            )
        pages = require_list(response.get("Pages"), self.name, "search", "Response.Pages")
        counts = CandidateCounts(len(pages))
        hits: list[KeywordSearchHit] = []
        for item in pages:
            if not isinstance(item, str):
                counts.reject("invalid_record_shape")
                continue
            try:
                page = json.loads(item)
            except ValueError:
                counts.reject("decode_error")
                continue
            if not isinstance(page, dict):
                counts.reject("invalid_record_shape")
                continue
            try:
                content = optional_string(page.get("content"), self.name, "search", "page.content")
                snippet = (
                    content
                    if content.strip()
                    else optional_string(page.get("passage"), self.name, "search", "page.passage")
                )
                hit = KeywordSearchHit(
                    url=non_empty_string(page.get("url"), self.name, "search", "page.url"),
                    title=optional_string(page.get("title"), self.name, "search", "page.title"),
                    snippet=snippet,
                )
            except ExecutionFailure:
                counts.reject(candidate_rejection_reason(page, "url"))
                continue
            hits.append(hit)
            counts.accept()
        log_event(
            _LOGGER,
            logging.DEBUG,
            "provider_candidates_summary",
            provider=self.name,
            stage="search",
            phase="adapter",
            exc_info=False,
            **counts.as_fields(),
        )
        return hits
