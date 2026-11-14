"""ScrapingDog Google Search and Web Scraping adapter."""

import logging
from functools import partial
from urllib.parse import urlencode

from ...observability import SecretValue
from ...providers.contracts import KeywordSearchHit, URLFetchCandidate
from ...url_normalization import NormalizedURL
from .common import (
    HttpRequester,
    configured_string,
    endpoint,
    failure,
    json_search_response_diagnostics,
    organic_search_hits,
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
        payload = await self._http.request_json(
            "GET",
            request_url,
            stage="search",
            response_diagnostics=partial(
                json_search_response_diagnostics, results_key="organic_results"
            ),
        )
        return organic_search_hits(payload, self.name, logger=logging.getLogger(__name__))

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        params = urlencode({"api_key": self._secret.reveal(), "url": str(url)})
        request_url = f"{endpoint(self._api_url, '/scrape')}?{params}"
        text = await self._http.request_text("GET", request_url, stage="fetch")
        if not text.strip():
            raise failure(
                self.name,
                "fetch",
                "page body is empty",
                diagnostics={"semantic_result": "empty", "response_kind": "text"},
            )
        return URLFetchCandidate(text, text)
