"""LangSearch REST web search adapter."""

from ...observability import SecretValue
from ..contracts import KeywordSearchHit
from .common import (
    JsonRequester,
    endpoint,
    non_empty_string,
    require_list,
    require_object,
    require_string,
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
        data = require_object(root.get("data"), self.name, "search", "data")
        pages = require_object(data.get("webPages"), self.name, "search", "data.webPages")
        results = require_list(pages.get("value"), self.name, "search", "data.webPages.value")
        hits = []
        for item in results:
            result = require_object(item, self.name, "search", "result")
            hits.append(
                KeywordSearchHit(
                    url=non_empty_string(result.get("url"), self.name, "search", "result.url"),
                    title=require_string(result.get("name"), self.name, "search", "result.name"),
                    snippet=require_string(
                        result.get("snippet"), self.name, "search", "result.snippet"
                    ),
                )
            )
        return hits
