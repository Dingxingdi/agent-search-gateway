"""ScrapeGraphAI v2 Search and Scrape adapter."""

from ...errors import ExecutionFailure
from ...observability import SecretValue
from ...providers.contracts import KeywordSearchHit, URLFetchCandidate
from ...url_normalization import NormalizedURL
from .common import (
    JsonRequester,
    configured_string,
    endpoint,
    failure,
    non_empty_string,
    optional_string,
    require_list,
    require_object,
)


class ScrapeGraphAIAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str,
        secret: SecretValue,
        http_executor: JsonRequester,
    ) -> None:
        self.name = name
        self._api_url = configured_string(api_url, "api_url").rstrip("/")
        self._secret = secret
        self._http = http_executor

    @property
    def _headers(self) -> dict[str, str]:
        return {"SGAI-APIKEY": self._secret.reveal()}

    async def search(self, query: str) -> list[KeywordSearchHit]:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/api/search"),
            stage="search",
            headers=self._headers,
            json_body={"query": query},
        )
        root = require_object(payload, self.name, "search", "response")
        if root.get("error") is not None or root.get("detail") is not None:
            raise failure(self.name, "search", "provider reported failure")
        results = require_list(root.get("results"), self.name, "search", "results")
        hits: list[KeywordSearchHit] = []
        for item in results:
            try:
                result = require_object(item, self.name, "search", "result")
                content = optional_string(
                    result.get("content"), self.name, "search", "result.content"
                )
                hits.append(
                    KeywordSearchHit(
                        url=non_empty_string(result.get("url"), self.name, "search", "result.url"),
                        title=optional_string(
                            result.get("title"), self.name, "search", "result.title"
                        ),
                        snippet="",
                        raw_content=content,
                        content=content,
                    )
                )
            except ExecutionFailure:
                continue
        return hits

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/api/scrape"),
            stage="fetch",
            headers=self._headers,
            json_body={"url": str(url), "formats": [{"type": "markdown"}]},
        )
        root = require_object(payload, self.name, "fetch", "response")
        if root.get("error") is not None or root.get("detail") is not None:
            raise failure(self.name, "fetch", "provider reported failure")
        results = require_object(root.get("results"), self.name, "fetch", "results")
        markdown = require_object(results.get("markdown"), self.name, "fetch", "results.markdown")
        data = require_list(markdown.get("data"), self.name, "fetch", "results.markdown.data")
        if not data:
            raise failure(self.name, "fetch", "page body is empty")
        text = non_empty_string(data[0], self.name, "fetch", "results.markdown.data[0]")
        return URLFetchCandidate(text, text)
