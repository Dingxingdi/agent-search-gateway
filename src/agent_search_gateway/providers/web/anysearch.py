"""AnySearch unified search adapter."""

import logging
from collections.abc import Mapping

import httpx

from ...observability import SecretValue
from ...providers.contracts import KeywordSearchHit
from ..diagnostics import best_effort_diagnostics
from .common import (
    JsonRequester,
    endpoint,
    failure,
    keyword_hits_from_results,
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
        return keyword_hits_from_results(results, self.name, logger=logging.getLogger(__name__))
