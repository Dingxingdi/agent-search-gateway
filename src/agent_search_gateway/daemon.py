"""Foreground Unix-socket daemon and graceful shutdown lifecycle."""

import asyncio
import logging
import os
import stat
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Protocol

from .config import load_toml, resolve_config
from .errors import ConfigFailure, ErrorCode, GatewayError
from .models import (
    ErrorResponse,
    KeywordSearchRequest,
    LLMSearchRequest,
    LLMSearchScope,
    PaperSearchRequest,
    Request,
    Response,
    ShutdownRequest,
    SuccessResponse,
    URLFetchRequest,
)
from .observability import DebugLoggingSession, elapsed_ms, log_event
from .paths import RuntimePaths
from .protocol import NDJSONDecoder, encode_response
from .providers.academic.defaults import (
    build_default_academic_registry,
    build_default_oa_resolver_registry,
)
from .providers.defaults import build_default_registry
from .request_ids import RequestIdFactory, RequestIdRegistry, bind_request_id, generate_request_id
from .runtime import Runtime
from .socket_probe import SocketState, probe_unix_socket

_SHUTDOWN_GRACE_SECONDS = 10.0
_SOCKET_PROBE_TIMEOUT_SECONDS = 2.0


class _SearchOrchestratorLike(Protocol):
    async def keyword_search(
        self, query: str, *, request_id: str, provider: str | None = None
    ) -> str: ...

    async def llm_search(
        self,
        prompt: str,
        *,
        request_id: str,
        scope: LLMSearchScope = "web",
        provider: str | None = None,
        model: str | None = None,
    ) -> str: ...


class _PaperSearchOrchestratorLike(Protocol):
    async def paper_search(
        self, query: str, *, request_id: str, provider: str | None = None
    ) -> str: ...


class _FetchOrchestratorLike(Protocol):
    async def url_fetch(self, url: str, focus: str | None = None) -> str: ...


class RuntimeLike(Protocol):
    @property
    def search_orchestrator(self) -> _SearchOrchestratorLike: ...

    @property
    def paper_search_orchestrator(self) -> _PaperSearchOrchestratorLike: ...

    @property
    def fetch_orchestrator(self) -> _FetchOrchestratorLike: ...

    async def aclose(self) -> None: ...


RuntimeFactory = Callable[[], RuntimeLike]
ShutdownWaiter = Callable[[tuple[asyncio.Task[object], ...], float], Awaitable[None]]
MonotonicClock = Callable[[], float]


async def _default_shutdown_waiter(
    tasks: tuple[asyncio.Task[object], ...],
    timeout: float,
) -> None:
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    if pending:
        raise TimeoutError


def _command_name(
    request: KeywordSearchRequest | PaperSearchRequest | LLMSearchRequest | URLFetchRequest,
) -> str:
    if isinstance(request, KeywordSearchRequest):
        return "keyword-search"
    if isinstance(request, PaperSearchRequest):
        return "paper-search"
    if isinstance(request, LLMSearchRequest):
        return "llm-search"
    return "url-fetch"


class ForegroundDaemon:
    def __init__(
        self,
        paths: RuntimePaths,
        *,
        runtime_factory: RuntimeFactory | None = None,
        environ: Mapping[str, str] | None = None,
        logger: logging.Logger | None = None,
        shutdown_waiter: ShutdownWaiter = _default_shutdown_waiter,
        request_id_factory: RequestIdFactory = generate_request_id,
        debug: bool = False,
        logging_session: DebugLoggingSession | None = None,
        monotonic: MonotonicClock = time.monotonic,
    ) -> None:
        self.paths = paths
        self.ready = asyncio.Event()
        self.stopped = asyncio.Event()
        self._logger = logger or logging.getLogger(__name__)
        self._environ = dict(os.environ if environ is None else environ)
        self._runtime_factory = runtime_factory
        self._shutdown_waiter = shutdown_waiter
        self._request_ids = RequestIdRegistry(paths.results_dir, factory=request_id_factory)
        self._debug = debug
        self._logging_session = logging_session
        self._monotonic = monotonic
        self._session_started = False
        self._session_stopped = False
        self._runtime: RuntimeLike | None = None
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._state_lock = asyncio.Lock()
        self._shutting_down = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._shutdown_response_ready = asyncio.Event()
        self._active_workflows: set[asyncio.Task[object]] = set()

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    async def start(self) -> None:
        self.paths.socket_file.parent.mkdir(parents=True, exist_ok=True)
        self.paths.results_dir.mkdir(parents=True, exist_ok=True)
        try:
            await self._prepare_socket_path()
            self._runtime = self._create_runtime()
            try:
                self._server = await asyncio.start_unix_server(
                    self._handle_connection,
                    path=self.paths.socket_file,
                )
            except OSError as exc:
                await self._close_runtime_after_startup_failure()
                raise ConfigFailure(
                    ErrorCode.CONFIG_ERROR,
                    f"Failed to bind daemon socket: {self.paths.socket_file}",
                ) from exc
            socket_stat = self.paths.socket_file.stat()
            self._socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
            if self._debug:
                log_event(
                    self._logger,
                    logging.INFO,
                    "session_started",
                    pid=os.getpid(),
                    debug=True,
                )
                self._session_started = True
            self.ready.set()
            await self.stopped.wait()
        except (asyncio.CancelledError, KeyboardInterrupt):
            await self.stop_for_test()
            raise

    async def _prepare_socket_path(self) -> None:
        path = self.paths.socket_file
        probe = await probe_unix_socket(
            path,
            timeout_seconds=_SOCKET_PROBE_TIMEOUT_SECONDS,
            connector=asyncio.open_unix_connection,
        )
        if probe.state is SocketState.MISSING:
            return
        if probe.state is SocketState.LIVE:
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR,
                f"Daemon is already running at: {path}",
            )
        if probe.state is SocketState.TIMEOUT:
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR,
                f"Daemon socket did not respond in time: {path}",
            )
        if probe.state is SocketState.NOT_SOCKET:
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR,
                f"Daemon socket path is not a Unix socket: {path}",
            )
        if probe.state is SocketState.OS_ERROR:
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR,
                f"Unable to inspect daemon socket: {path}",
            )

        identity = probe.identity
        if probe.state is not SocketState.REFUSED or identity is None:
            raise RuntimeError("unexpected daemon socket probe state")
        try:
            current = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(current.st_mode) or (current.st_dev, current.st_ino) != identity:
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR,
                f"Daemon socket changed during startup: {path}",
            )
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR,
                f"Failed to remove stale daemon socket: {path}",
            ) from exc

    def _create_runtime(self) -> RuntimeLike:
        try:
            if self._runtime_factory is not None:
                return self._runtime_factory()
            registry = build_default_registry()
            academic_registry = build_default_academic_registry()
            resolver_registry = build_default_oa_resolver_registry()
            data = load_toml(self.paths.config_file)
            config = resolve_config(
                data,
                registry,
                self._environ,
                academic_registry=academic_registry,
                oa_resolver_registry=resolver_registry,
            )
            if self._logging_session is not None:
                web_secrets = (
                    provider.secret
                    for provider in config.web.providers
                    if provider.secret is not None
                )
                llm_secrets = (provider.secret for provider in config.llm.providers)
                academic_secrets = (
                    secret
                    for provider in config.academic.providers
                    for secret in (provider.api_key, provider.contact_email)
                    if secret is not None
                )
                resolver_secrets = (
                    secret
                    for resolver in (config.oa_resolver,)
                    if resolver is not None
                    for secret in (resolver.api_key, resolver.contact_email)
                    if secret is not None
                )
                self._logging_session.add_secrets(
                    web_secrets,
                    llm_secrets,
                    academic_secrets,
                    resolver_secrets,
                )
            return Runtime.build(
                config,
                self.paths,
                registry=registry,
                academic_registry=academic_registry,
                oa_resolver_registry=resolver_registry,
            )
        except ConfigFailure:
            raise
        except Exception as exc:
            raise ConfigFailure(
                ErrorCode.CONFIG_ERROR,
                "Failed to initialize daemon runtime",
            ) from exc

    async def _close_runtime_after_startup_failure(self) -> None:
        runtime = self._runtime
        self._runtime = None
        if runtime is None:
            return
        try:
            await runtime.aclose()
        except Exception:
            self._logger.error("daemon runtime close failed during startup cleanup")

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        decoder = NDJSONDecoder()
        try:
            while data := await reader.read(4096):
                for decoded in decoder.feed(data):
                    if isinstance(decoded, ErrorResponse):
                        response: Response = decoded
                    else:
                        response = await self._dispatch(decoded)
                    writer.write(encode_response(response))
                    await writer.drain()
        except (ConnectionError, BrokenPipeError):
            return
        finally:
            writer.close()
            with suppress(ConnectionError, BrokenPipeError):
                await writer.wait_closed()

    async def _dispatch(self, request: Request) -> Response:
        if isinstance(request, ShutdownRequest):
            await self._begin_shutdown()
            await self._shutdown_response_ready.wait()
            return SuccessResponse("Daemon stopped.")
        if not isinstance(
            request,
            (KeywordSearchRequest, PaperSearchRequest, LLMSearchRequest, URLFetchRequest),
        ):
            return ErrorResponse(ErrorCode.BAD_REQUEST, "Unknown request type")

        is_search = isinstance(
            request,
            (KeywordSearchRequest, PaperSearchRequest, LLMSearchRequest),
        )
        with (
            self._request_ids.reserve(may_write_search_result=is_search) as request_id,
            bind_request_id(request_id),
        ):
            return await self._dispatch_business(request, request_id=request_id)

    async def _dispatch_business(
        self,
        request: KeywordSearchRequest | PaperSearchRequest | LLMSearchRequest | URLFetchRequest,
        *,
        request_id: str,
    ) -> Response:
        command = _command_name(request)
        started = self._monotonic()
        log_event(self._logger, logging.DEBUG, "workflow_started", command=command)
        current = asyncio.current_task()
        if current is None:
            return self._internal_workflow_failure(command, started, RuntimeError("missing task"))

        async with self._state_lock:
            if self._shutting_down:
                log_event(
                    self._logger,
                    logging.DEBUG,
                    "workflow_rejected",
                    command=command,
                    elapsed_ms=elapsed_ms(self._monotonic, started),
                    error_code=ErrorCode.DAEMON_SHUTTING_DOWN.value,
                )
                return ErrorResponse(
                    ErrorCode.DAEMON_SHUTTING_DOWN,
                    "Daemon is shutting down",
                )
            self._active_workflows.add(current)

        try:
            text = await self._invoke_workflow(request, request_id=request_id)
        except asyncio.CancelledError:
            log_event(
                self._logger,
                logging.DEBUG,
                "workflow_cancelled",
                command=command,
                elapsed_ms=elapsed_ms(self._monotonic, started),
            )
            raise
        except GatewayError as exc:
            log_event(
                self._logger,
                logging.DEBUG,
                "workflow_failed",
                command=command,
                elapsed_ms=elapsed_ms(self._monotonic, started),
                error_code=exc.code.value,
                error_type=type(exc).__name__,
            )
            return ErrorResponse(exc.code, exc.message)
        except Exception as exc:
            return self._internal_workflow_failure(command, started, exc)
        else:
            log_event(
                self._logger,
                logging.DEBUG,
                "workflow_completed",
                command=command,
                elapsed_ms=elapsed_ms(self._monotonic, started),
            )
            return SuccessResponse(text)
        finally:
            async with self._state_lock:
                self._active_workflows.discard(current)

    async def _invoke_workflow(
        self,
        request: KeywordSearchRequest | PaperSearchRequest | LLMSearchRequest | URLFetchRequest,
        *,
        request_id: str,
    ) -> str:
        runtime = self._require_runtime()
        if isinstance(request, KeywordSearchRequest):
            return await runtime.search_orchestrator.keyword_search(
                request.query,
                request_id=request_id,
                provider=request.provider,
            )
        if isinstance(request, PaperSearchRequest):
            return await runtime.paper_search_orchestrator.paper_search(
                request.query,
                request_id=request_id,
                provider=request.provider,
            )
        if isinstance(request, LLMSearchRequest):
            return await runtime.search_orchestrator.llm_search(
                request.prompt,
                request_id=request_id,
                scope=request.scope,
                provider=request.provider,
                model=request.model,
            )
        return await runtime.fetch_orchestrator.url_fetch(request.url, request.focus)

    def _internal_workflow_failure(
        self,
        command: str,
        started: float,
        exc: Exception,
    ) -> ErrorResponse:
        if self._debug:
            log_event(
                self._logger,
                logging.ERROR,
                "workflow_failed",
                command=command,
                elapsed_ms=elapsed_ms(self._monotonic, started),
                error_type=type(exc).__name__,
                exc_info=exc,
            )
        else:
            self._logger.error(
                "unexpected daemon workflow failure type=%s",
                type(exc).__name__,
            )
        return ErrorResponse(ErrorCode.PROTOCOL_ERROR, "Internal daemon error")

    def _require_runtime(self) -> RuntimeLike:
        if self._runtime is None:
            raise RuntimeError("daemon runtime is not initialized")
        return self._runtime

    async def _begin_shutdown(self) -> asyncio.Task[None]:
        async with self._state_lock:
            if self._shutdown_task is None:
                self._shutting_down = True
                self._shutdown_task = asyncio.create_task(self._shutdown_coordinator())
            return self._shutdown_task

    async def _shutdown_coordinator(self) -> None:
        server: asyncio.AbstractServer | None = None
        try:
            async with self._state_lock:
                active = tuple(self._active_workflows)
            try:
                await self._shutdown_waiter(active, _SHUTDOWN_GRACE_SECONDS)
            except TimeoutError:
                for task in active:
                    if not task.done():
                        task.cancel()
                if active:
                    await asyncio.gather(*active, return_exceptions=True)
            server = await self._cleanup_before_response()
            self._shutdown_response_ready.set()
            if server is not None:
                try:
                    await server.wait_closed()
                except Exception:
                    self._logger.error("daemon socket server close failed")
        finally:
            self._shutdown_response_ready.set()
            self.stopped.set()

    async def _cleanup_before_response(self) -> asyncio.AbstractServer | None:
        self._emit_session_stopped()
        runtime = self._runtime
        self._runtime = None
        if runtime is not None:
            try:
                await runtime.aclose()
            except Exception:
                self._logger.error("daemon runtime close failed")

        server = self._server
        self._server = None
        if server is not None:
            try:
                server.close()
            except Exception:
                self._logger.error("daemon socket server close failed")

        if self._owns_socket(self.paths.socket_file):
            try:
                self.paths.socket_file.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                self._logger.error("daemon socket unlink failed")
        return server

    def _emit_session_stopped(self) -> None:
        if not self._session_started or self._session_stopped:
            return
        self._session_stopped = True
        log_event(
            self._logger,
            logging.INFO,
            "session_stopped",
            pid=os.getpid(),
            debug=self._debug,
        )

    def _owns_socket(self, path: Path) -> bool:
        identity = self._socket_identity
        if identity is None:
            return False
        try:
            stat = path.stat()
        except FileNotFoundError:
            return False
        return (stat.st_dev, stat.st_ino) == identity

    async def stop_for_test(self) -> None:
        task = await self._begin_shutdown()
        await task
