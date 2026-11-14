"""Brave web search adapter."""

import logging
from collections.abc import Mapping
from urllib.parse import urlencode

import httpx

from ...errors import ExecutionFailure
from ...observability import SecretValue, log_event
from ...providers.contracts import KeywordSearchHit
from ..diagnostics import CandidateCounts
from .common import (
    JsonRequester,
    candidate_rejection_reason,
    endpoint,
    non_empty_string,
    optional_string,
    require_list,
    require_object,
)


def _search_response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    if not isinstance(payload, dict):
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    web = payload.get("web")
    if web is None:
        return {"category": "unknown", "semantic_result": "unknown", "response_kind": "object"}
    if not isinstance(web, dict) or (
        web.get("results") is not None and not isinstance(web["results"], list)
    ):
        return {
            "category": "invalid_envelope",
            "semantic_result": "unknown",
            "response_kind": "object",
        }
    return {"semantic_result": "success", "response_kind": "object"}


class BraveAdapter:
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
        base = endpoint(self._api_url, "/res/v1/web/search")
        request_url = f"{base}?{urlencode({'q': query, 'count': 10})}"
        payload = await self._http.request_json(
            "GET",
            request_url,
            stage="search",
            response_diagnostics=_search_response_diagnostics,
            headers={"X-Subscription-Token": self._secret.reveal()},
        )
        root = require_object(payload, self.name, "search", "response")
        web_value = root.get("web")
        results: list[object] = []
        if web_value is not None:
            web = require_object(web_value, self.name, "search", "web")
            results_value = web.get("results")
            if results_value is not None:
                results = require_list(results_value, self.name, "search", "web.results")
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
                            result.get("description"),
                            self.name,
                            "search",
                            "result.description",
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
