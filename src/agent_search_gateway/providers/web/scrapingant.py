"""ScrapingAnt v2 Markdown fetch adapter."""

from collections.abc import Mapping
from urllib.parse import urlencode

import httpx

from ...errors import ExecutionFailure
from ...observability import SecretValue
from ...providers.contracts import URLFetchCandidate
from ...url_normalization import NormalizedURL
from ..diagnostics import best_effort_diagnostics
from .common import (
    JsonRequester,
    configured_string,
    endpoint,
    failure,
    non_empty_string,
    require_object,
    selected_error_fields,
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
        payload = await self._http.request_json(
            "GET",
            request_url,
            stage="fetch",
            response_diagnostics=self._fetch_response_diagnostics,
        )
        return self._parse_fetch(payload)

    def _fetch_response_diagnostics(self, response: httpx.Response) -> Mapping[str, object]:
        try:
            payload = response.json()
        except ValueError:
            return {"category": "decode", "semantic_result": "unknown"}
        try:
            self._parse_fetch(payload)
        except ExecutionFailure as exc:
            return {"category": "invalid_envelope", "semantic_result": "error", **exc.diagnostics}
        return {"response_kind": "object", "semantic_result": "success"}

    def _parse_fetch(self, payload: object) -> URLFetchCandidate:
        root = require_object(payload, self.name, "fetch", "response")
        if root.get("error") is not None:
            raise failure(
                self.name,
                "fetch",
                "provider reported failure",
                diagnostics=best_effort_diagnostics(
                    lambda: {
                        "category": "provider_response",
                        "semantic_result": "error",
                        **selected_error_fields(root.get("error")),
                    }
                ),
            )
        try:
            markdown = non_empty_string(root.get("markdown"), self.name, "fetch", "markdown")
        except ExecutionFailure as exc:
            value = root.get("markdown")
            if isinstance(value, str) and not value.strip():
                exc.diagnostics = {
                    **exc.diagnostics,
                    "semantic_result": "empty",
                    "response_kind": "object",
                }
            raise
        return URLFetchCandidate(markdown, markdown)
