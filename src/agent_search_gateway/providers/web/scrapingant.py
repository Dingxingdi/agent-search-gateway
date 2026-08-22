"""ScrapingAnt v2 Markdown fetch adapter."""

from urllib.parse import urlencode

from ...observability import SecretValue
from ...providers.contracts import URLFetchCandidate
from ...url_normalization import NormalizedURL
from .common import (
    JsonRequester,
    configured_string,
    endpoint,
    failure,
    non_empty_string,
    require_object,
)


class ScrapingAntAdapter:
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

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        request_url = (
            f"{endpoint(self._api_url, '/v2/markdown')}?"
            f"{urlencode({'url': str(url), 'x-api-key': self._secret.reveal()})}"
        )
        payload = await self._http.request_json("GET", request_url, stage="fetch")
        root = require_object(payload, self.name, "fetch", "response")
        if root.get("error") is not None:
            raise failure(self.name, "fetch", "provider reported failure")
        markdown = non_empty_string(root.get("markdown"), self.name, "fetch", "markdown")
        return URLFetchCandidate(markdown, markdown)
