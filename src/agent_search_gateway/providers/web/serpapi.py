"""SerpApi Google Search adapter."""

import logging
from functools import partial
from urllib.parse import urlencode

from ...observability import SecretValue
from ...providers.contracts import KeywordSearchHit
from .common import (
    JsonRequester,
    configured_string,
    endpoint,
    json_search_response_diagnostics,
    organic_search_hits,
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
        payload = await self._http.request_json(
            "GET",
            request_url,
            stage="search",
            response_diagnostics=partial(
                json_search_response_diagnostics, results_key="organic_results"
            ),
        )
        return organic_search_hits(payload, self.name, logger=logging.getLogger(__name__))
