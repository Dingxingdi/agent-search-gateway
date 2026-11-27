"""Fixtures for processes that discover the gateway through their own HOME."""

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from mcp.client import Client

from agent_search_gateway.paths import RuntimePaths
from tests.mcp._support import ClientMode, Frontend, captured_stdio, http_frontend


@pytest.fixture
def mcp_home() -> Iterator[Path]:
    with TemporaryDirectory(dir="/tmp", prefix="mcp-") as directory:
        home = Path(directory)
        # macOS sockaddr_un.sun_path holds 104 bytes including the final NUL.
        assert len(os.fsencode(RuntimePaths.from_home(home).socket_file)) < 104
        yield home


@pytest.fixture(params=["stdio", "streamable-http"])
def frontend(request: pytest.FixtureRequest, mcp_home: Path) -> Frontend:
    @asynccontextmanager
    async def connect(mode: ClientMode) -> AsyncIterator[Client]:
        if request.param == "stdio":
            with captured_stdio(mcp_home, mode=mode) as capture:
                async with capture.client as client:
                    yield client
            return

        async with http_frontend(mcp_home, mode) as client:
            yield client

    return connect
