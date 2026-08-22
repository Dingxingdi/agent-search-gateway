"""ZenRows current Fetch API adapter."""

from urllib.parse import urlencode

from ...observability import SecretValue
from ...providers.contracts import URLFetchCandidate
from ...url_normalization import NormalizedURL
from .common import TextRequester, configured_string, endpoint, failure


class ZenRowsAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str,
        secret: SecretValue,
        http_executor: TextRequester,
    ) -> None:
        self.name = name
        self._api_url = configured_string(api_url, "api_url").rstrip("/")
        self._secret = secret
        self._http = http_executor

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        params = urlencode(
            {
                "apikey": self._secret.reveal(),
                "url": str(url),
                "response_type": "markdown",
            }
        )
        request_url = f"{endpoint(self._api_url, '/v1')}?{params}"
        text = await self._http.request_text("GET", request_url, stage="fetch")
        if not text.strip():
            raise failure(self.name, "fetch", "page body is empty")
        return URLFetchCandidate(text, text)
