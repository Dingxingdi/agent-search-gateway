"""LangSearch REST web search adapter."""

import logging

from ...errors import ExecutionFailure
from ...observability import SecretValue, log_event
from ..contracts import KeywordSearchHit
from ..diagnostics import CandidateCounts
from .common import (
    JsonRequester,
    candidate_rejection_reason,
    endpoint,
    failure,
    non_empty_string,
    optional_string,
    require_list,
    require_object,
)


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
            headers={"Authorization": f"Bearer {self._secret.reveal()}"},
            json_body={"query": query, "count": 10},
        )
        root = require_object(payload, self.name, "search", "response")
        code = root.get("code")
        if not isinstance(code, int) or isinstance(code, bool) or code != 200:
            raise failure(self.name, "search", "code must be the integer 200")
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
