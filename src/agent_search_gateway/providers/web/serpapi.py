"""SerpApi Google Search adapter."""

from urllib.parse import urlencode

from ...errors import ExecutionFailure
from ...observability import SecretValue
from ...providers.contracts import KeywordSearchHit
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


class SerpApiAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str,
        secret: SecretValue,
        http_executor: JsonRequester,
    ) -> None:
        self.name = name
        self._api_url = configured_string(api_url, "api_url").rstrip("?")
        self._secret = secret
        self._http = http_executor

    async def search(self, query: str) -> list[KeywordSearchHit]:
        request_url = (
            f"{endpoint(self._api_url, '/search')}?"
            f"{urlencode({'engine': 'google', 'q': query, 'api_key': self._secret.reveal()})}"
        )
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
