"""Typed adapter between MCP tools and the gateway socket client."""

import asyncio
import logging
import os
import stat
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from pathlib import Path
from typing import TypeAlias

from ..errors import DaemonUnavailable, ErrorCode, GatewayError, InputFailure
from ..models import (
    AgentSearchRequest,
    ErrorResponse,
    KeywordSearchRequest,
    LLMSearchRequest,
    PaperSearchRequest,
    Request,
    Response,
    SearchScope,
    SuccessResponse,
    URLFetchRequest,
)
from ..paths import RuntimePaths
from ..protocol import send_request
from ..request_ids import ResultKind, result_filename
from ..url_normalization import normalize_url

_LOGGER = logging.getLogger(__name__)

BusinessRequest: TypeAlias = (
    KeywordSearchRequest
    | PaperSearchRequest
    | LLMSearchRequest
    | AgentSearchRequest
    | URLFetchRequest
)
SocketClient: TypeAlias = Callable[[Path, Request], Awaitable[Response]]


class AdapterFailure(Exception):
    def __init__(self, code: ErrorCode | None, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


_SAFE_MESSAGES = {
    ErrorCode.BAD_REQUEST: "Invalid request. Check the supplied selectors and scope.",
    ErrorCode.EMPTY_QUERY: "Query or prompt must not be empty",
    ErrorCode.INVALID_URL: "URL must be a valid HTTP or HTTPS URL",
    ErrorCode.URL_NOT_ADMITTED: "Search must admit the URL before fetching it.",
    ErrorCode.NO_KEYWORD_SEARCH_PROVIDERS: "No keyword search providers for this request.",
    ErrorCode.NO_LLM_SEARCH_PROVIDERS: "No LLM search providers for this request.",
    ErrorCode.NO_AGENT_SEARCH_PROVIDERS: "No agent search providers for this request.",
    ErrorCode.NO_URL_FETCH_PROVIDERS: "No Fetch providers for this request.",
    ErrorCode.NO_ACADEMIC_SEARCH_PROVIDERS: "No academic search providers for this request.",
    ErrorCode.ALL_PROVIDERS_FAILED: "All relevant providers failed. Consult the daemon logs.",
    ErrorCode.LLM_STAGE_FAILED: "The LLM stage failed. Consult the daemon logs.",
    ErrorCode.PROTOCOL_ERROR: "Invalid daemon response. Consult the daemon logs.",
    ErrorCode.CONFIG_ERROR: "Gateway configuration error. Check the daemon configuration.",
    ErrorCode.DAEMON_SHUTTING_DOWN: "The daemon is shutting down.",
}


def _gateway_failure(code: ErrorCode) -> AdapterFailure:
    return AdapterFailure(code, _SAFE_MESSAGES[code])


def _required_text(value: str) -> str:
    text = value.strip()
    if not text:
        raise _gateway_failure(ErrorCode.EMPTY_QUERY)
    return text


def _selector(value: str | None) -> str | None:
    if value is None:
        return None
    selector = value.strip()
    if not selector:
        raise AdapterFailure(ErrorCode.BAD_REQUEST, "Provider or model must not be empty")
    return selector


def _scope(value: SearchScope) -> SearchScope:
    if value not in ("web", "paper", "all"):
        raise AdapterFailure(ErrorCode.BAD_REQUEST, "Scope must be web, paper or all")
    return value


def _focus(value: str | None) -> str | None:
    return (value or "").strip() or None


_RESULT_KINDS: dict[type[BusinessRequest], ResultKind] = {
    KeywordSearchRequest: "keyword",
    PaperSearchRequest: "paper",
    LLMSearchRequest: "llm",
    AgentSearchRequest: "agent",
}


def _read_result(results_dir: Path, path_text: str, kind: ResultKind) -> str:
    try:
        candidate = Path(path_text)
        if not candidate.is_absolute() or ".." in candidate.parts or "\x00" in path_text:
            raise ValueError
        root = results_dir.resolve(strict=True)
        request_id = candidate.stem.partition("-")[2]
        # pathlib normalizes a trailing slash or "/."; the wire name must identify a file.
        name = os.path.basename(path_text)
        if name != result_filename(kind, request_id):
            raise ValueError
        if candidate.parent.resolve(strict=True) != root:
            raise ValueError
        with ExitStack() as descriptors:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
            descriptors.callback(os.close, root_fd)
            before = os.lstat(name, dir_fd=root_fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            result_fd = os.open(name, flags, dir_fd=root_fd)
            descriptors.callback(os.close, result_fd)
            opened = os.fstat(result_fd)
            if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
                raise ValueError
            data = bytearray()
            while chunk := os.read(result_fd, 64 * 1024):
                data.extend(chunk)
        return data.decode("utf-8")
    except (OSError, ValueError, RuntimeError):
        raise AdapterFailure(None, "Search result unavailable or invalid") from None


class GatewayAdapter:
    def __init__(self, paths: RuntimePaths, client: SocketClient = send_request) -> None:
        self._paths = paths
        self._client = client

    async def _execute(self, request: BusinessRequest) -> str:
        try:
            response = await self._client(self._paths.socket_file, request)
            if isinstance(response, ErrorResponse):
                raise _gateway_failure(response.error)
            if not isinstance(response, SuccessResponse):
                raise _gateway_failure(ErrorCode.PROTOCOL_ERROR)
            if isinstance(request, URLFetchRequest):
                return response.text
            return await asyncio.to_thread(
                _read_result, self._paths.results_dir, response.text, _RESULT_KINDS[type(request)]
            )
        except AdapterFailure:
            raise
        except GatewayError as exc:
            raise _gateway_failure(exc.code) from None
        except DaemonUnavailable:
            raise AdapterFailure(
                None, "Daemon unavailable. Run agent-search-gateway start."
            ) from None
        except OSError:
            raise AdapterFailure(None, "Communication with the daemon failed.") from None
        except Exception:
            _LOGGER.error("Unexpected gateway adapter failure")
            raise AdapterFailure(
                None, "Unexpected gateway failure. Check the MCP server diagnostics."
            ) from None

    async def keyword_search(self, query: str, provider: str | None = None) -> str:
        return await self._execute(
            KeywordSearchRequest(_required_text(query), provider=_selector(provider))
        )

    async def paper_search(self, query: str, provider: str | None = None) -> str:
        return await self._execute(
            PaperSearchRequest(_required_text(query), provider=_selector(provider))
        )

    async def llm_search(
        self,
        prompt: str,
        scope: SearchScope = "web",
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        return await self._execute(
            LLMSearchRequest(
                _required_text(prompt),
                _scope(scope),
                provider=_selector(provider),
                model=_selector(model),
            )
        )

    async def agent_search(
        self,
        prompt: str,
        scope: SearchScope = "web",
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        return await self._execute(
            AgentSearchRequest(
                _required_text(prompt),
                _scope(scope),
                provider=_selector(provider),
                model=_selector(model),
            )
        )

    async def url_fetch(self, url: str, focus: str | None = None) -> str:
        try:
            normalized = normalize_url(url)
        except InputFailure as exc:
            raise _gateway_failure(exc.code) from None
        return await self._execute(URLFetchRequest(normalized, _focus(focus)))
