"""Jina Reader fetch adapter."""

from ...providers.contracts import URLFetchCandidate
from ...url_normalization import NormalizedURL
from .common import TextRequester, configured_string, failure


class JinaReaderAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str,
        http_executor: TextRequester,
    ) -> None:
        self.name = name
        self._api_url = configured_string(api_url, "api_url").rstrip("/")
        self._http = http_executor

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        text = await self._http.request_text(
            "POST",
            self._api_url,
            stage="fetch",
            headers={"X-No-Cache": "true"},
            json_body={"url": str(url)},
        )
        if not text.strip():
            raise failure(
                self.name,
                "fetch",
                "page body is empty",
                diagnostics={"semantic_result": "empty", "response_kind": "text"},
            )
        return URLFetchCandidate(text, text)
