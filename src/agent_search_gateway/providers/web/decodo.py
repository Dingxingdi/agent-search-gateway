"""Decodo Google Search template and Markdown scrape adapter."""

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


class DecodoAdapter:
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

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Basic {self._secret.reveal()}"}

    async def search(self, query: str) -> list[KeywordSearchHit]:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/v2/scrape"),
            stage="search",
            headers=self._headers,
            json_body={"target": "google_search", "query": query, "parse": True},
        )
        results = self._organic_results(payload)
        hits: list[KeywordSearchHit] = []
        for item in results:
            try:
                result = require_object(item, self.name, "search", "result")
                hits.append(
                    KeywordSearchHit(
                        url=non_empty_string(
                            result.get("url"), self.name, "search", "result.url"
                        ),
                        title=optional_string(
                            result.get("title"), self.name, "search", "result.title"
                        ),
                        snippet=optional_string(
                            result.get("desc"), self.name, "search", "result.desc"
                        ),
                    )
                )
            except ExecutionFailure:
                continue
        return hits

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        text = await self._http.request_text(
            "POST",
            endpoint(self._api_url, "/v2/scrape"),
            stage="fetch",
            headers=self._headers,
            json_body={"url": str(url), "markdown": True},
        )
        if not text.strip():
            raise failure(self.name, "fetch", "page body is empty")
        return URLFetchCandidate(text, text)

    def _organic_results(self, payload: object) -> list[object]:
        root = require_object(payload, self.name, "search", "response")
        pages = require_list(root.get("results"), self.name, "search", "results")
        if not pages:
            raise failure(self.name, "search", "results must contain a parsed page")
        page = require_object(pages[0], self.name, "search", "result page")
        if page.get("status_code") != 200:
            raise failure(self.name, "search", "provider reported failure")
        content = require_object(page.get("content"), self.name, "search", "content")
        errors = require_list(content.get("errors", []), self.name, "search", "content.errors")
        if errors:
            raise failure(self.name, "search", "provider reported failure")
        parsed_wrapper = require_object(
            content.get("results"), self.name, "search", "content.results"
        )
        parsed = require_object(
            parsed_wrapper.get("results"), self.name, "search", "content.results.results"
        )
        return require_list(parsed.get("organic"), self.name, "search", "organic")
