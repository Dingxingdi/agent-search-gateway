"""You.com web provider adapter."""

import logging
import posixpath
from collections.abc import Callable, Mapping
from typing import Literal, cast
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import httpx

from ...errors import ConfigFailure, ErrorCode, ExecutionFailure, InputFailure
from ...observability import SecretValue, log_event
from ...url_normalization import NormalizedURL, normalize_url
from ..contracts import KeywordSearchHit, URLFetchCandidate
from ..diagnostics import CandidateCounts, safe_diagnostic_fields
from ..http import ProviderResponseFailure
from ._youcom_mcp import _FREE_ENDPOINT, decode_tool_response, tool_request
from .common import (
    JsonRequester,
    candidate_rejection_reason,
    endpoint,
    failure,
    fetch_response_diagnostics,
    non_empty_string,
    normalized_match,
    optional_string,
    parse_fetch_with_diagnostics,
    require_list,
    require_object,
    require_string,
)

_DEFAULT_API_URL = "https://ydc-index.io"
_MCP_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

_KNOWN_ERROR_CODES = frozenset(
    {
        "unauthorized",
        "payment_required",
        "forbidden",
        "invalid_request",
        "spend_cap_exceeded",
    }
)


def _is_free_mcp_endpoint(api_url: object) -> bool:
    if not isinstance(api_url, str):
        raise ConfigFailure(ErrorCode.CONFIG_ERROR, "Invalid You.com API URL")
    try:
        url = urlsplit(api_url)
    except ValueError:
        raise ConfigFailure(ErrorCode.CONFIG_ERROR, "Invalid You.com API URL") from None
    # IDNA treats these three characters as DNS label separators, as does HTTPX.
    host = unquote(url.hostname or "").lower().translate(str.maketrans("\u3002\uff0e\uff61", "..."))
    if host.rstrip(".") != "api.you.com":
        return False
    # Reject aliases of official MCP paths before they can be treated as REST roots.
    decoded_path = "/" + unquote(url.path).lstrip("/")
    if not (url.path.startswith("/mcp") or posixpath.normpath(decoded_path).startswith("/mcp")):
        return False
    scheme, _, remainder = api_url.partition("://")
    if (
        scheme.lower() == "https"
        and url.netloc.lower() in {"api.you.com", "api.you.com:443"}
        and url.path in {"/mcp/free", "/mcp/free/"}
        # Reconstruct the suffix to reject even empty query/fragment delimiters.
        and remainder == url.netloc + url.path
    ):
        return True
    raise ConfigFailure(ErrorCode.CONFIG_ERROR, "Unsupported You.com MCP endpoint")


def _use_free_mcp(api_url: object, has_api_key: bool) -> bool:
    use_mcp = _is_free_mcp_endpoint(api_url)
    if use_mcp and has_api_key:
        raise ConfigFailure(ErrorCode.CONFIG_ERROR, "You.com free MCP does not accept an API key")
    if not use_mcp and not has_api_key:
        raise ConfigFailure(ErrorCode.CONFIG_ERROR, "You.com REST requires an API key")
    return use_mcp


def _error_code(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        if error in _KNOWN_ERROR_CODES:
            return error
        code = payload.get("code")
        return code if isinstance(code, str) and code in _KNOWN_ERROR_CODES else "unknown"
    if isinstance(error, dict) and ("code" in error or "message" in error):
        code = error.get("code")
        return code if isinstance(code, str) and code in _KNOWN_ERROR_CODES else "unknown"
    results = payload.get("results")
    has_results = isinstance(results, dict) and isinstance(results.get("web"), list)
    detail = payload.get("detail")
    if not has_results and isinstance(detail, str) and detail.strip():
        return "unknown"
    return None


def _error_diagnostics(code: str) -> dict[str, object]:
    return safe_diagnostic_fields(
        {
            "category": "provider_response",
            "semantic_result": "error",
            "response_kind": "object",
            "upstream_code": code,
        }
    )


def _search_response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    code = _error_code(payload)
    if code is not None:
        return _error_diagnostics(code)
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, dict) and isinstance(results.get("web"), list):
            return {"semantic_result": "success", "response_kind": "object"}
    return {"category": "invalid_envelope", "semantic_result": "unknown"}


class YouComAdapter:
    @staticmethod
    def validate_config(options: Mapping[str, object], has_api_key: bool) -> None:
        _use_free_mcp(options.get("api_url", _DEFAULT_API_URL), has_api_key)

    def __init__(
        self,
        *,
        name: str,
        api_url: str = _DEFAULT_API_URL,
        secret: SecretValue | None = None,
        http_executor: JsonRequester,
    ) -> None:
        self._use_mcp = _use_free_mcp(api_url, secret is not None)
        self.name = name
        self._api_url = _FREE_ENDPOINT if self._use_mcp else api_url
        self._secret = secret
        self._http = http_executor

    def _rest_headers(self) -> dict[str, str]:
        if self._secret is None:
            raise ConfigFailure(ErrorCode.CONFIG_ERROR, "You.com REST requires an API key")
        return {"X-API-Key": self._secret.reveal()}

    async def search(self, query: str) -> list[KeywordSearchHit]:
        if self._use_mcp:
            return await self._search_mcp(query)
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/v1/search"),
            stage="search",
            headers=self._rest_headers(),
            json_body={"query": query, "count": 10},
            response_inspector=lambda response: self._inspect_response(response, "search"),
            response_diagnostics=_search_response_diagnostics,
        )
        root = require_object(payload, self.name, "search", "response")
        code = _error_code(root)
        if code is not None:
            raise failure(
                self.name,
                "search",
                "provider reported failure",
                diagnostics=_error_diagnostics(code),
            )
        results = require_object(root.get("results"), self.name, "search", "results")
        web = require_list(results.get("web"), self.name, "search", "results.web")
        hits: list[KeywordSearchHit] = []
        counts = CandidateCounts(len(web))
        for item in web:
            try:
                record = require_object(item, self.name, "search", "result")
                url = non_empty_string(record.get("url"), self.name, "search", "result.url")
                normalize_url(url)
                snippets = require_list(
                    [] if record.get("snippets") is None else record["snippets"],
                    self.name,
                    "search",
                    "result.snippets",
                )
                hits.append(
                    KeywordSearchHit(
                        url=url,
                        title=optional_string(
                            record.get("title"), self.name, "search", "result.title"
                        ),
                        snippet="\n\n".join(
                            require_string(value, self.name, "search", "result.snippets[]")
                            for value in snippets
                        ),
                    )
                )
            except (ExecutionFailure, InputFailure):
                counts.reject(candidate_rejection_reason(item, "url"))
                continue
            counts.accept()
        log_event(
            logging.getLogger(__name__),
            logging.DEBUG,
            "provider_candidates_summary",
            provider=self.name,
            stage="search",
            phase="adapter",
            exc_info=False,
            **counts.as_fields(),
        )
        return hits

    async def _search_mcp(self, query: str) -> list[KeywordSearchHit]:
        decoded = await self._call_mcp(
            "you-search", {"query": query, "count": 10}, "search", self._parse_mcp_search
        )
        if not isinstance(decoded, list) or not all(
            isinstance(hit, KeywordSearchHit) for hit in decoded
        ):
            raise failure(self.name, "search", "invalid decoded search results")
        return cast(list[KeywordSearchHit], decoded)

    async def _call_mcp(
        self,
        tool_name: Literal["you-search", "you-extract"],
        arguments: Mapping[str, object],
        stage: str,
        parse: Callable[[object], object],
    ) -> object:
        rpc_id = uuid4().hex
        headers, body = tool_request(rpc_id, tool_name, arguments)
        diagnostics: dict[str, object] = {}

        def decode(response: httpx.Response) -> object:
            payload = decode_tool_response(response, rpc_id, self.name, stage)
            parsed = parse(payload)
            diagnostics.update(semantic_result="success", response_kind="object")
            return parsed

        return await self._http.request_json(
            "POST",
            self._api_url,
            stage=stage,
            headers=headers,
            json_body=body,
            response_decoder=decode,
            response_diagnostics=lambda response: diagnostics,
            max_response_bytes=_MCP_MAX_RESPONSE_BYTES,
            preview_bodies=False,
            inherit_client_defaults=False,
        )

    def _parse_mcp_search(self, payload: object) -> list[KeywordSearchHit]:
        try:
            root = require_object(payload, self.name, "search", "response")
            results = require_object(root.get("results"), self.name, "search", "results")
            web = require_list(results.get("web"), self.name, "search", "results.web")
        except ExecutionFailure as exc:
            exc.diagnostics = safe_diagnostic_fields(
                {"category": "invalid_envelope", "semantic_result": "error"}
            )
            raise
        hits: list[KeywordSearchHit] = []
        counts = CandidateCounts(len(web))
        for item in web:
            try:
                record = require_object(item, self.name, "search", "result")
                url = non_empty_string(record.get("url"), self.name, "search", "result.url")
                normalize_url(url)
                contents_value = record.get("contents")
                contents = require_object(
                    {} if contents_value is None else contents_value,
                    self.name,
                    "search",
                    "result.contents",
                )
                # Prefer observed highlights; the output schema also permits top-level snippets.
                selected = contents.get("highlights")
                if selected is None:
                    selected = record.get("snippets")
                snippets = require_list(
                    [] if selected is None else selected,
                    self.name,
                    "search",
                    "result.snippets",
                )
                hits.append(
                    KeywordSearchHit(
                        url=url,
                        title=optional_string(
                            record.get("title"), self.name, "search", "result.title"
                        ),
                        snippet="\n\n".join(
                            require_string(value, self.name, "search", "result.snippets[]")
                            for value in snippets
                        ),
                    )
                )
            except (ExecutionFailure, InputFailure):
                counts.reject(candidate_rejection_reason(item, "url"))
                continue
            counts.accept()
        log_event(
            logging.getLogger(__name__),
            logging.DEBUG,
            "provider_candidates_summary",
            provider=self.name,
            stage="search",
            phase="adapter",
            exc_info=False,
            **counts.as_fields(),
        )
        return hits

    def _inspect_response(
        self, response: httpx.Response, stage: str
    ) -> ProviderResponseFailure | None:
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        code = _error_code(payload)
        if code is None:
            return None
        return ProviderResponseFailure(
            self.name,
            stage,
            status_code=response.status_code,
            retryable=False,
            safe_reason="provider reported failure",
            diagnostics=_error_diagnostics(code),
        )

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        if self._use_mcp:
            decoded = await self._call_mcp(
                "you-extract",
                {"url": str(url)},
                "fetch",
                lambda payload: parse_fetch_with_diagnostics(
                    payload, lambda value: self._parse_mcp_fetch(value, url)
                ),
            )
            if not isinstance(decoded, URLFetchCandidate):
                raise failure(self.name, "fetch", "invalid decoded fetch result")
            return decoded
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/v1/contents"),
            stage="fetch",
            headers=self._rest_headers(),
            json_body={"urls": [str(url)], "formats": ["markdown"]},
            response_diagnostics=lambda response: self._fetch_response_diagnostics(response, url),
            response_inspector=lambda response: self._inspect_response(response, "fetch"),
        )
        return parse_fetch_with_diagnostics(payload, lambda value: self._parse_fetch(value, url))

    def _parse_mcp_fetch(self, payload: object, target: NormalizedURL) -> URLFetchCandidate:
        root = require_object(payload, self.name, "fetch", "response")
        output = require_object(root.get("output"), self.name, "fetch", "output")
        if "url" in output and not normalized_match(output["url"], target, self.name, "fetch"):
            raise failure(
                self.name,
                "fetch",
                "no matching result",
                diagnostics={"response_kind": "object", "semantic_result": "no_match"},
            )
        contents = require_object(output.get("contents"), self.name, "fetch", "output.contents")
        full_page = require_object(
            contents.get("full_page"), self.name, "fetch", "output.contents.full_page"
        )
        markdown = full_page.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise failure(
                self.name,
                "fetch",
                "matching markdown is empty",
                diagnostics={"response_kind": "object", "semantic_result": "empty"},
            )
        return URLFetchCandidate(raw_content=markdown, content=markdown)

    def _parse_fetch(self, payload: object, target: NormalizedURL) -> URLFetchCandidate:
        code = _error_code(payload)
        if code is not None:
            raise failure(
                self.name,
                "fetch",
                "provider reported failure",
                diagnostics=_error_diagnostics(code),
            )
        results = require_list(payload, self.name, "fetch", "response")
        empty_match_seen = False
        for item in results:
            try:
                record = require_object(item, self.name, "fetch", "result")
                if not normalized_match(record.get("url"), target, self.name, "fetch"):
                    continue
            except ExecutionFailure:
                continue
            markdown = record.get("markdown")
            if isinstance(markdown, str) and markdown.strip():
                return URLFetchCandidate(markdown, markdown)
            empty_match_seen = True
        raise failure(
            self.name,
            "fetch",
            "matching markdown is empty" if empty_match_seen else "no matching result",
            diagnostics={
                "response_kind": "array",
                "semantic_result": "empty" if empty_match_seen else "no_match",
            },
        )

    def _fetch_response_diagnostics(
        self, response: httpx.Response, target: NormalizedURL
    ) -> Mapping[str, object]:
        fields = dict(
            fetch_response_diagnostics(response, lambda value: self._parse_fetch(value, target))
        )
        if fields.get("semantic_result") == "success":
            fields["response_kind"] = "array"
        return fields
