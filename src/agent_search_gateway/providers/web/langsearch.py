"""LangSearch REST web search adapter."""

import logging
from collections.abc import Mapping

import httpx

from ...errors import ExecutionFailure
from ...observability import SecretValue, log_event
from ..contracts import KeywordSearchHit
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


def _is_success_code(code: object) -> bool:
    return type(code) is int and code == 200


def _error_fields(root: Mapping[str, object]) -> dict[str, object]:
    fields = {"code": root.get("code")}
    for key in ("message", "msg"):
        message = root.get(key)
        if isinstance(message, str):
            fields["message"] = message
            break
    return selected_error_fields(fields)


def _search_response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    try:
        root = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    if isinstance(root, dict):
        code = root.get("code")
        if _is_success_code(code):
            data = root.get("data")
            pages = data.get("webPages") if isinstance(data, dict) else None
            if isinstance(pages, dict) and isinstance(pages.get("value"), list):
                return {"semantic_result": "success", "response_kind": "object"}
        elif type(code) is int or (isinstance(code, str) and code and code != "200"):
            return {
                "category": "provider_response",
                "semantic_result": "error",
                "response_kind": "object",
                **_error_fields(root),
            }
    return {"category": "invalid_envelope", "semantic_result": "unknown"}


class LangSearchAdapter:
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

    async def search(self, query: str) -> list[KeywordSearchHit]:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/v1/web-search"),
            stage="search",
            response_diagnostics=_search_response_diagnostics,
            headers={"Authorization": f"Bearer {self._secret.reveal()}"},
            json_body={"query": query, "count": 10},
        )
        root = require_object(payload, self.name, "search", "response")
        code = root.get("code")
        if not _is_success_code(code):
            raise failure(
                self.name,
                "search",
                "code must be the integer 200",
                diagnostics=best_effort_diagnostics(lambda: _error_fields(root)),
            )
        data = require_object(root.get("data"), self.name, "search", "data")
        pages = require_object(data.get("webPages"), self.name, "search", "data.webPages")
        results = require_list(pages.get("value"), self.name, "search", "data.webPages.value")
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
                            result.get("name"), self.name, "search", "result.name"
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
