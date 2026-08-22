"""ScrapingDog Google Search and Web Scraping adapter."""

from urllib.parse import urlencode

from ...errors import ExecutionFailure
from ...observability import SecretValue
from ...providers.contracts import KeywordSearchHit, URLFetchCandidate
from ...url_normalization import NormalizedURL
from .common import (
    HttpRequester,
    configured_string,
    endpoint,
    failure,
    non_empty_string,
    optional_string,
    require_list,
    require_object,
)


class ScrapingDogAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str,
        secret: SecretValue,
        http_executor: HttpRequester,
    ) -> None:
        self.name = name
        self._api_url = configured_string(api_url, "api_url").rstrip("/")
        self._secret = secret
        self._http = http_executor

    async def search(self, query: str) -> list[KeywordSearchHit]:
        params = urlencode({"api_key": self._secret.reveal(), "query": query})
        request_url = f"{endpoint(self._api_url, '/google')}?{params}"
        payload = await self._http.request_json("GET", request_url, stage="search")
        root = require_object(payload, self.name, "search", "response")
        if root.get("error") is not None:
            raise failure(self.name, "search", "provider reported failure")
        results = require_list(root.get("organic_results"), self.name, "search", "organic_results")
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
                            result.get("snippet"), self.name, "search", "result.snippet"
                        ),
                    )
                )
            except ExecutionFailure:
                continue
        return hits

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        params = urlencode({"api_key": self._secret.reveal(), "url": str(url)})
        request_url = f"{endpoint(self._api_url, '/scrape')}?{params}"
        text = await self._http.request_text("GET", request_url, stage="fetch")
        if not text.strip():
            raise failure(self.name, "fetch", "page body is empty")
        return URLFetchCandidate(text, text)
