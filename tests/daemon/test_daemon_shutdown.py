import asyncio
from pathlib import Path

from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.errors import ErrorCode, ProtocolFailure
from agent_search_gateway.models import (
    ErrorResponse,
    KeywordSearchRequest,
    LLMSearchRequest,
    ShutdownRequest,
    SuccessResponse,
)
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.protocol import send_request


class _SlowSearch:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def keyword_search(
        self, query: str, *, request_id: str, provider: str | None = None
    ) -> str:
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return f"keyword:{query}"

    async def llm_search(
        self,
        prompt: str,
        *,
        request_id: str,
        scope: str = "web",
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        return f"llm:{prompt}"


class _Paper:
    async def paper_search(
        self, query: str, *, request_id: str, provider: str | None = None
    ) -> str:
        return f"paper:{query}"


class _Fetch:
    async def url_fetch(self, url: str, focus: str | None = None) -> str:
        return url


class _Runtime:
    def __init__(self) -> None:
        self.search_orchestrator = _SlowSearch()
        self.paper_search_orchestrator = _Paper()
        self.fetch_orchestrator = _Fetch()
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


async def _wait_until_shutting_down(daemon: ForegroundDaemon) -> None:
    while not daemon.shutting_down:
        await asyncio.sleep(0)


async def test_shutdown_rejects_new_work_waits_or_cancels_active_requests_and_cleans_up(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "g.toml",
        socket_file=tmp_path / "g.sock",
        results_dir=tmp_path / "g-results",
    )
    runtime = _Runtime()
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    daemon_task = asyncio.create_task(daemon.start())
    await daemon.ready.wait()

    slow = asyncio.create_task(send_request(paths.socket_file, KeywordSearchRequest("slow")))
    await runtime.search_orchestrator.entered.wait()
    shutdown_one = asyncio.create_task(send_request(paths.socket_file, ShutdownRequest()))
    await _wait_until_shutting_down(daemon)

    rejected = await send_request(paths.socket_file, LLMSearchRequest("new"))
    assert rejected == ErrorResponse(
        ErrorCode.DAEMON_SHUTTING_DOWN,
        "Daemon is shutting down",
    )
    shutdown_two = asyncio.create_task(send_request(paths.socket_file, ShutdownRequest()))
    runtime.search_orchestrator.release.set()

    assert await slow == SuccessResponse("keyword:slow")
    assert await shutdown_one == SuccessResponse("Daemon stopped.")
    assert await shutdown_two == SuccessResponse("Daemon stopped.")
    await daemon_task
    assert not runtime.search_orchestrator.cancelled.is_set()
    assert runtime.close_calls == 1
    assert not paths.socket_file.exists()


async def test_shutdown_timeout_cancels_active_requests_and_cleans_up(
    tmp_path: Path,
) -> None:
    timeout_paths = RuntimePaths(
        config_file=tmp_path / "t.toml",
        socket_file=tmp_path / "t.sock",
        results_dir=tmp_path / "t-results",
    )
    timeout_runtime = _Runtime()
    waiter_calls: list[float] = []

    async def timeout_waiter(
        tasks: tuple[asyncio.Task[object], ...],
        timeout: float,
    ) -> None:
        assert tasks
        waiter_calls.append(timeout)
        raise TimeoutError

    timeout_daemon = ForegroundDaemon(
        timeout_paths,
        runtime_factory=lambda: timeout_runtime,
        shutdown_waiter=timeout_waiter,
    )
    timeout_task = asyncio.create_task(timeout_daemon.start())
    await timeout_daemon.ready.wait()
    cancelled_client = asyncio.create_task(
        send_request(timeout_paths.socket_file, KeywordSearchRequest("slow"))
    )
    await timeout_runtime.search_orchestrator.entered.wait()
    timeout_shutdown = asyncio.create_task(
        send_request(timeout_paths.socket_file, ShutdownRequest())
    )

    assert await timeout_shutdown == SuccessResponse("Daemon stopped.")
    cancelled_result = await asyncio.gather(cancelled_client, return_exceptions=True)
    assert isinstance(cancelled_result[0], ProtocolFailure)
    await timeout_task
    assert waiter_calls == [10.0]
    assert timeout_runtime.search_orchestrator.cancelled.is_set()
    assert timeout_runtime.close_calls == 1
    assert not timeout_paths.socket_file.exists()
