"""AnySearch unified search adapter."""

import logging
from collections.abc import Mapping

import httpx

from ...errors import ExecutionFailure
from ...observability import SecretValue, log_event
from ...providers.contracts import KeywordSearchHit
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


def _search_response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    if not isinstance(payload, dict):
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    code = payload.get("code")
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        return {
            "category": "provider_response",
            "semantic_result": "error",
            "response_kind": "object",
            **selected_error_fields(payload),
        }
    data = payload.get("data")
    if (
        isinstance(code, int)
        and not isinstance(code, bool)
        and code == 0
        and isinstance(data, dict)
        and isinstance(data.get("results"), list)
    ):
        return {"semantic_result": "success", "response_kind": "object"}
    return {"category": "invalid_envelope", "semantic_result": "unknown"}


class AnySearchAdapter:
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
            endpoint(self._api_url, "/v1/search"),
            stage="search",
            response_diagnostics=_search_response_diagnostics,
            headers={"Authorization": f"Bearer {self._secret.reveal()}"},
            json_body={"query": query, "format": "json", "max_results": 10},
        )
        root = require_object(payload, self.name, "search", "response")
        code = root.get("code")
        if not isinstance(code, int) or isinstance(code, bool) or code != 0:
            raise failure(
                self.name,
                "search",
                "provider reported failure",
                diagnostics=best_effort_diagnostics(
                    lambda: (
                        selected_error_fields(root)
                        if isinstance(code, int) and not isinstance(code, bool)
                        else {}
                    )
                ),
            )
        data = require_object(root.get("data"), self.name, "search", "data")
        results = require_list(data.get("results"), self.name, "search", "data.results")
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
