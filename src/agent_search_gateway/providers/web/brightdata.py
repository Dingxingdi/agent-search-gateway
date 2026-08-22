"""Bright Data SERP and Web Unlocker adapter."""

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


class BrightDataAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str,
        search_zone: str,
        fetch_zone: str,
        secret: SecretValue,
        http_executor: HttpRequester,
    ) -> None:
        self.name = name
        self._api_url = configured_string(api_url, "api_url").rstrip("/")
        self._search_zone = configured_string(search_zone, "search_zone")
        self._fetch_zone = configured_string(fetch_zone, "fetch_zone")
        self._secret = secret
        self._http = http_executor

    async def search(self, query: str) -> list[KeywordSearchHit]:
        google_url = "https://www.google.com/search?" + urlencode({"q": query, "brd_json": "1"})
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/request"),
            stage="search",
            headers={"Authorization": f"Bearer {self._secret.reveal()}"},
            json_body={"zone": self._search_zone, "url": google_url, "format": "raw"},
        )
        root = require_object(payload, self.name, "search", "response")
        if root.get("error") is not None:
            raise failure(self.name, "search", "provider reported failure")
        results = require_list(root.get("organic"), self.name, "search", "organic")
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
                            result.get("description"), self.name, "search", "result.description"
                        ),
                    )
                )
            except ExecutionFailure:
                continue
        return hits

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        text = await self._http.request_text(
            "POST",
            endpoint(self._api_url, "/request"),
            stage="fetch",
            headers={"Authorization": f"Bearer {self._secret.reveal()}"},
            json_body={
                "zone": self._fetch_zone,
                "url": str(url),
                "format": "raw",
                "data_format": "markdown",
            },
        )
        if not text.strip():
            raise failure(self.name, "fetch", "page body is empty")
        return URLFetchCandidate(text, text)
