"""Helpers for isolated adapter and MCP protocol tests."""

import asyncio
import errno
import fcntl
import os
import socket
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from typing import Literal, TextIO, TypeAlias
from unittest.mock import patch

import httpx2
import pytest
import uvicorn
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from agent_search_gateway.mcp.adapter import GatewayAdapter
from agent_search_gateway.mcp.server import create_http_app, create_server
from agent_search_gateway.models import Request, Response, SuccessResponse
from agent_search_gateway.paths import RuntimePaths


class RecordingClient:
    """Record each RPC, then deliver its configured outcome."""

    def __init__(self, response: Response | Exception | None = None) -> None:
        self.calls: list[tuple[Path, Request]] = []
        self.response = response

    async def __call__(self, socket_path: Path, request: Request, /) -> Response:
        self.calls.append((socket_path, request))
        if self.response is None:
            raise asyncio.CancelledError
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def successful_response(paths: RuntimePaths, tool_name: str, text: str) -> SuccessResponse:
    """Prepare the typed daemon success for Fetch or a real temporary search file."""
    if tool_name == "url_fetch":
        return SuccessResponse(text)
    paths.results_dir.mkdir(parents=True, exist_ok=True)
    result = paths.results_dir / f"{tool_name.removesuffix('_search')}-1234abcd.jsonl"
    result.write_bytes(text.encode("utf-8"))
    return SuccessResponse(str(result))


def gateway_client(paths: RuntimePaths, rpc: RecordingClient) -> Client:
    """Connect an SDK client to the production server with an isolated socket boundary."""
    return Client(create_server(GatewayAdapter(paths, rpc)), cache=None)


@contextmanager
def guard_result_opens(root: Path, name: str) -> Iterator[list[int]]:
    """Record actual descriptors and fail before any unexpected or blocking open."""
    real_open = os.open
    opened: list[int] = []

    def checked_open(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if dir_fd is None:
            assert Path(path) == root and not opened
            required = os.O_DIRECTORY | os.O_CLOEXEC
        else:
            assert path == name and opened == [dir_fd]
            required = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        assert flags & required == required
        assert flags & os.O_ACCMODE == os.O_RDONLY
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(fd)
        return fd

    with (
        patch("os.open", side_effect=checked_open),
        patch.object(Path, "open", side_effect=AssertionError("unexpected path-based open")),
        patch("os.scandir", side_effect=AssertionError("unexpected directory scan")),
        patch("os.listdir", side_effect=AssertionError("unexpected directory listing")),
    ):
        yield opened

    for fd in opened:
        with pytest.raises(OSError) as closed:
            fcntl.fcntl(fd, fcntl.F_GETFD)
        assert closed.value.errno == errno.EBADF


PeerConnections: TypeAlias = asyncio.Queue[tuple[asyncio.StreamReader, asyncio.StreamWriter]]


@asynccontextmanager
async def socket_peer(socket_file: Path) -> AsyncIterator[PeerConnections]:
    """Expose each accepted connection to the test; own every server-side writer."""
    connections: PeerConnections = asyncio.Queue()
    writers: list[asyncio.StreamWriter] = []

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.append(writer)
        connections.put_nowait((reader, writer))

    async with await asyncio.start_unix_server(accept, socket_file) as server:
        try:
            yield connections
        finally:
            server.close()
            for writer in writers:
                writer.close()
            for writer in writers:
                with suppress(ConnectionError):
                    await writer.wait_closed()


@asynccontextmanager
async def unix_peer(
    paths: RuntimePaths,
) -> AsyncIterator[tuple[RuntimePaths, PeerConnections]]:
    # Unix socket path limits can be shorter than pytest's temporary directory names.
    with TemporaryDirectory(dir="/tmp") as directory:
        isolated = replace(paths, socket_file=Path(directory) / "peer.sock")
        async with socket_peer(isolated.socket_file) as connections:
            yield isolated, connections


ClientMode: TypeAlias = Literal["auto", "legacy"]
Frontend: TypeAlias = Callable[[ClientMode], AbstractAsyncContextManager[Client]]


@dataclass
class CapturedStdio:
    client: Client
    stderr: TextIO
    stdout: bytearray = field(default_factory=bytearray)
    process: asyncio.SubprocessTransport | None = None


@contextmanager
def captured_stdio(home: Path, *args: str, mode: ClientMode = "auto") -> Iterator[CapturedStdio]:
    """Observe the SDK-owned child at the OS pipe, including bytes during teardown."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-I", "-B", "-m", "agent_search_gateway.mcp", *args],
        cwd=home,
        env={"HOME": str(home)},
    )
    with TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        capture = CapturedStdio(
            Client(stdio_client(params, errlog=stderr), cache=None, mode=mode), stderr
        )

        # Observe bytes before the SDK can decode or discard them on shutdown.
        protocol = asyncio.subprocess.SubprocessStreamProtocol
        connection_made = protocol.connection_made
        pipe_data_received = protocol.pipe_data_received

        def observe_process(
            handler: asyncio.subprocess.SubprocessStreamProtocol, transport: asyncio.BaseTransport
        ) -> None:
            assert isinstance(transport, asyncio.SubprocessTransport)
            assert capture.process is None
            capture.process = transport
            connection_made(handler, transport)

        def observe_bytes(
            handler: asyncio.subprocess.SubprocessStreamProtocol, fd: int, data: bytes | str
        ) -> None:
            if fd == 1:
                assert isinstance(data, bytes)
                capture.stdout.extend(data)
            pipe_data_received(handler, fd, data)

        with (
            patch.object(protocol, "connection_made", observe_process),
            patch.object(protocol, "pipe_data_received", observe_bytes),
        ):
            yield capture


@asynccontextmanager
async def running_http(app: Starlette) -> AsyncIterator[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_config=None, ws="none"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        pytest.fail("HTTP server exited before startup")
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            async with asyncio.timeout(5):
                await task


@asynccontextmanager
async def http_session(url: str, mode: ClientMode = "auto") -> AsyncIterator[Client]:
    async with httpx2.AsyncClient(trust_env=False) as http:
        transport = streamable_http_client(f"{url}/mcp", http_client=http, max_sse_event_size=None)
        async with Client(transport, cache=None, mode=mode) as client:
            yield client


@asynccontextmanager
async def http_frontend(home: Path, mode: ClientMode = "auto") -> AsyncIterator[Client]:
    server = create_server(GatewayAdapter(RuntimePaths.from_home(home)))
    async with (
        running_http(create_http_app(server, "127.0.0.1")) as url,
        http_session(url, mode) as client,
    ):
        yield client
