import asyncio
from pathlib import Path

import pytest

from agent_search_gateway.errors import DaemonUnavailable, ErrorCode, ProtocolFailure
from agent_search_gateway.models import KeywordSearchRequest, SuccessResponse
from agent_search_gateway.protocol import _MAX_RESPONSE_FRAME_BYTES, encode_response, send_request


async def _run_server(path: Path, response: bytes, received: list[bytes]) -> asyncio.AbstractServer:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.readline())
        writer.write(response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    return await asyncio.start_unix_server(handler, path=path)


async def test_socket_client_sends_one_line_and_accepts_first_complete_response(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "daemon.sock"
    received: list[bytes] = []
    server = await _run_server(
        socket_path,
        b'{"ok":true,"text":"done"}\n{"ok":true,"text":"ignored"}\n',
        received,
    )
    try:
        response = await send_request(socket_path, KeywordSearchRequest("hello"))
    finally:
        server.close()
        await server.wait_closed()

    assert response == SuccessResponse("done")
    assert received == [b'{"type":"keyword_search","query":"hello"}\n']


async def test_socket_client_accepts_response_larger_than_default_asyncio_limit(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "large.sock"
    received: list[bytes] = []
    text = "x" * (128 * 1024)
    server = await _run_server(socket_path, encode_response(SuccessResponse(text)), received)
    try:
        response = await send_request(socket_path, KeywordSearchRequest("hello"))
    finally:
        server.close()
        await server.wait_closed()

    assert response == SuccessResponse(text)


async def test_socket_client_rejects_oversized_and_stalled_responses(tmp_path: Path) -> None:
    oversized_path = tmp_path / "oversized.sock"
    received: list[bytes] = []
    oversized_response = b'{"ok":true,"text":"' + b"x" * (_MAX_RESPONSE_FRAME_BYTES + 1) + b'"}\n'
    server = await _run_server(oversized_path, oversized_response, received)
    try:
        with pytest.raises(ProtocolFailure, match="too large"):
            await send_request(oversized_path, KeywordSearchRequest("hello"))
    finally:
        server.close()
        await server.wait_closed()

    stalled_path = tmp_path / "stalled.sock"
    release = asyncio.Event()
    done = asyncio.Event()

    async def stalled_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
            await release.wait()
        finally:
            writer.close()
            await writer.wait_closed()
            done.set()

    stalled_server = await asyncio.start_unix_server(stalled_handler, path=stalled_path)
    try:
        with pytest.raises(ProtocolFailure, match="Timed out"):
            await send_request(
                stalled_path,
                KeywordSearchRequest("hello"),
                response_timeout_seconds=0.01,
            )
    finally:
        release.set()
        await asyncio.wait_for(done.wait(), timeout=1.0)
        stalled_server.close()
        await stalled_server.wait_closed()


@pytest.mark.parametrize(
    "response",
    [
        b"not-json\n",
        b'{"ok":true,"text":"missing-newline"}',
        b'{"ok":true}\n',
        b'{"ok":"true","text":"wrong"}\n',
        b'{"ok":false,"error":"bad_request"}\n',
    ],
)
async def test_socket_client_rejects_malformed_response(tmp_path: Path, response: bytes) -> None:
    socket_path = tmp_path / "daemon.sock"
    received: list[bytes] = []
    server = await _run_server(socket_path, response, received)
    try:
        with pytest.raises(ProtocolFailure) as caught:
            await send_request(socket_path, KeywordSearchRequest("hello"))
    finally:
        server.close()
        await server.wait_closed()

    assert caught.value.code is ErrorCode.PROTOCOL_ERROR


async def test_socket_client_distinguishes_missing_daemon(tmp_path: Path) -> None:
    with pytest.raises(DaemonUnavailable):
        await send_request(tmp_path / "missing.sock", KeywordSearchRequest("hello"))


@pytest.mark.parametrize("kind", ["agent", "llm", "keyword", "paper", "fetch", "shutdown"])
@pytest.mark.parametrize("override", [None, 0.01, 42.0])
async def test_socket_timeout_depends_on_request_type_and_honors_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    override: float | None,
) -> None:
    from collections.abc import Awaitable

    from agent_search_gateway.models import (
        AgentSearchRequest,
        LLMSearchRequest,
        PaperSearchRequest,
        Request,
        ShutdownRequest,
        URLFetchRequest,
    )

    requests: dict[str, Request] = {
        "agent": AgentSearchRequest("q"),
        "llm": LLMSearchRequest("q"),
        "keyword": KeywordSearchRequest("q"),
        "paper": PaperSearchRequest("q"),
        "fetch": URLFetchRequest("https://example.test"),
        "shutdown": ShutdownRequest(),
    }
    seen: list[float | None] = []

    async def recording_wait_for(awaitable: Awaitable[bytes], timeout: float | None) -> bytes:
        seen.append(timeout)
        return await awaitable

    monkeypatch.setattr(asyncio, "wait_for", recording_wait_for)
    path = tmp_path / "timeout.sock"
    server = await _run_server(path, encode_response(SuccessResponse("ok")), [])
    try:
        assert await send_request(
            path, requests[kind], response_timeout_seconds=override
        ) == SuccessResponse("ok")
    finally:
        server.close()
        await server.wait_closed()
    assert seen == [override if override is not None else (1200.0 if kind == "agent" else 300.0)]
