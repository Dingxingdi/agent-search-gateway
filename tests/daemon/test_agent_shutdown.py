import asyncio
from pathlib import Path

import pytest

from agent_search_gateway.config import resolve_config
from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.errors import ErrorCode, ProtocolFailure
from agent_search_gateway.models import (
    AgentSearchRequest,
    ErrorResponse,
    ShutdownRequest,
    SuccessResponse,
)
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.agent import antigravity_cli as adapter
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.runtime import Runtime
from tests.acceptance.test_gateway_workflows import _running_daemon
from tests.providers.agent.test_antigravity_lifecycle import assert_clean, hanging_cli
from tests.runtime.test_agent_runtime_assembly import agent_config
from tests.runtime.test_runtime_assembly import _CountingAsyncClient, _environment
from tests.support.agent_cli import force_cleanup, wait_for_json


async def test_agent_shutdown_awaits_process_group_cleanup_before_stopping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_search_gateway.protocol import send_request

    monkeypatch.setattr(adapter, "_TERMINATE_GRACE_SECONDS", 0.05)
    command, ready = hanging_cli(tmp_path, ignore_term=True)
    paths = RuntimePaths(tmp_path / "config.toml", tmp_path / "daemon.sock", tmp_path / "results")
    runtime = Runtime.build(
        resolve_config(agent_config(str(command)), build_default_registry(), _environment()),
        paths,
        http_client_factory=_CountingAsyncClient,
    )
    close_calls = 0
    original_close = runtime.aclose

    async def counted_close() -> None:
        nonlocal close_calls
        close_calls += 1
        await original_close()

    monkeypatch.setattr(runtime, "aclose", counted_close)

    waiting, expire = asyncio.Event(), asyncio.Event()

    async def waiter(tasks: tuple[asyncio.Task[object], ...], timeout: float) -> None:
        assert timeout == 10.0 and len(tasks) == 1
        waiting.set()
        await expire.wait()
        raise TimeoutError

    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime, shutdown_waiter=waiter)
    state: dict[str, object] = {}
    async with _running_daemon(daemon) as daemon_task:
        active = asyncio.create_task(
            send_request(paths.socket_file, AgentSearchRequest("q", "all"))
        )
        try:
            async with asyncio.timeout(2):
                state = await wait_for_json(ready)
                stop = asyncio.create_task(send_request(paths.socket_file, ShutdownRequest()))
                await waiting.wait()
                assert await send_request(
                    paths.socket_file, AgentSearchRequest("new")
                ) == ErrorResponse(
                    ErrorCode.DAEMON_SHUTTING_DOWN,
                    "Daemon is shutting down",
                )
                expire.set()
                assert await stop == SuccessResponse("Daemon stopped.")
                with pytest.raises(ProtocolFailure, match="ended before newline"):
                    await active
                await daemon_task
            assert_clean(state, runtime.quotas.get_agent("primary"))
            assert close_calls == 1
            assert not paths.socket_file.exists()
            assert not list(paths.results_dir.glob("*.jsonl"))
        finally:
            expire.set()
            if state:
                force_cleanup(state)
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
