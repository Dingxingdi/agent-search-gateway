"""Shared real-transport contracts and STDIO-specific process checks."""

import asyncio
import json
from pathlib import Path

import pytest
from mcp import types
from mcp.shared.exceptions import MCPError

from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.errors import ErrorCode
from agent_search_gateway.models import (
    AgentSearchRequest,
    KeywordSearchRequest,
    LLMSearchRequest,
    PaperSearchRequest,
    Request,
    URLFetchRequest,
)
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.protocol import decode_request_frame, encode_response
from tests.acceptance.test_gateway_workflows import _filtering_cli, _running_daemon
from tests.mcp._support import (
    ClientMode,
    Frontend,
    captured_stdio,
    socket_peer,
    successful_response,
)
from tests.support.acceptance import build_acceptance_runtime


@pytest.mark.parametrize("args", [(), ("--transport", "stdio")], ids=["default", "explicit"])
async def test_stdio_lifecycle(mcp_home: Path, args: tuple[str, ...]) -> None:
    paths = RuntimePaths.from_home(mcp_home)
    runtime = build_acceptance_runtime(paths)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with asyncio.timeout(20), _running_daemon(daemon) as running:
        with captured_stdio(mcp_home, *args) as capture:
            async with capture.client as client:
                result = await client.call_tool("keyword_search", {"query": "find article"})
                assert "Keyword abstract" in _text(result)

            assert capture.process is not None and capture.process.get_returncode() == 0
            assert capture.stdout.endswith(b"\n")
            for line in bytes(capture.stdout).split(b"\n")[:-1]:
                types.jsonrpc_message_adapter.validate_json(line, by_name=False)
            assert str(mcp_home).encode() not in capture.stdout
            capture.stderr.seek(0)
            assert "Traceback" not in capture.stderr.read()

        assert not running.done()
        code, output, errors = await _filtering_cli(paths, ["keyword-search", "still running"])
        assert code == 0 and errors == ""
        assert "Keyword abstract" in Path(output.strip()).read_text(encoding="utf-8")

    assert not paths.socket_file.exists()


def _text(result: types.CallToolResult, *, error: bool = False) -> str:
    assert result.is_error is error
    assert result.structured_content is None
    assert len(result.content) == 1
    item = result.content[0]
    assert isinstance(item, types.TextContent)
    return item.text


def _assert_schemas(tools: list[types.Tool]) -> None:
    required = {
        "keyword_search": "query",
        "paper_search": "query",
        "llm_search": "prompt",
        "agent_search": "prompt",
        "url_fetch": "url",
    }
    assert sorted(tool.name for tool in tools) == sorted(required)
    for tool in tools:
        schema = tool.input_schema
        field = required[tool.name]
        nullable = {"focus"} if field == "url" else {"provider"}
        if field == "prompt":
            nullable.add("model")
        properties = schema["properties"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert schema["required"] == [field] and properties[field]["type"] == "string"
        assert set(properties) == {field} | nullable | ({"scope"} if field == "prompt" else set())
        for name in nullable:
            assert properties[name]["default"] is None
            assert {option["type"] for option in properties[name]["anyOf"]} == {"string", "null"}
        if field == "prompt":
            assert properties["scope"]["default"] == "web"
            assert set(properties["scope"]["enum"]) == {"web", "paper", "all"}
        assert tool.output_schema is None


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_transport_recovery_and_handshake(
    frontend: Frontend, mcp_home: Path, mode: ClientMode
) -> None:
    paths = RuntimePaths.from_home(mcp_home)
    arguments = {"query": "find article"}
    async with asyncio.timeout(25), frontend(mode) as client:
        if mode == "legacy":
            assert client.protocol_version == "2025-11-25"
        _assert_schemas((await client.list_tools()).tools)
        missing = await client.call_tool("keyword_search", arguments)
        assert "agent-search-gateway start" in _text(missing, error=True)
        assert list(mcp_home.iterdir()) == []

        runtime = build_acceptance_runtime(paths)
        daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
        async with _running_daemon(daemon):
            fetch_args = {"url": "https://example.com/article"}
            denied = await client.call_tool("url_fetch", fetch_args)
            assert ErrorCode.URL_NOT_ADMITTED.value in _text(denied, error=True)
            assert not runtime.fetch_provider.calls
            search = await client.call_tool("keyword_search", arguments)
            (written,) = paths.results_dir.glob("keyword-*.jsonl")
            original = written.read_bytes().decode("utf-8")
            assert json.loads(original) == {
                "url": fetch_args["url"],
                "abstract": "Keyword abstract",
            }
            assert _text(search) == original
            assert _text(await client.call_tool("url_fetch", fetch_args)) == "Full article content"
            assert len(runtime.fetch_provider.calls) == 1


async def test_transport_tool_call_contract(frontend: Frontend, mcp_home: Path) -> None:
    paths = RuntimePaths.from_home(mcp_home)
    paths.socket_file.parent.mkdir(parents=True, exist_ok=True)
    large = '{"abstract":"雪' + "x" * (1024 * 1024) + '"}\r\n'
    assert len(large.encode("utf-8")) > 1024 * 1024
    cases: list[tuple[str, dict[str, object], Request, str]] = [
        (
            "keyword_search",
            {"query": " q ", "provider": " alpha "},
            KeywordSearchRequest("q", "alpha"),
            large,
        ),
        (
            "paper_search",
            {"query": " papers ", "provider": " core "},
            PaperSearchRequest("papers", "core"),
            "",
        ),
        (
            "llm_search",
            {"prompt": " p ", "provider": " llm ", "model": " m "},
            LLMSearchRequest("p", "web", "llm", "m"),
            ' {"abstract":"雪"}\r\n',
        ),
        (
            "agent_search",
            {"prompt": " a ", "scope": "all", "provider": " agent ", "model": " m2 "},
            AgentSearchRequest("a", "all", "agent", "m2"),
            '{"type":"paper"}\n{"type":"web"}\n',
        ),
        (
            "url_fetch",
            {"url": "https://example.org/", "focus": " focus "},
            URLFetchRequest("https://example.org/", "focus"),
            " full text\r\nsecond line\n",
        ),
    ]
    async with (
        asyncio.timeout(25),
        socket_peer(paths.socket_file) as connections,
        frontend("auto") as client,
    ):
        with pytest.raises(MCPError) as rejected:
            await client.call_tool("keyword_search", {"query": 1})
        assert rejected.value.code == types.INVALID_PARAMS
        assert connections.empty()
        for name, arguments, expected, text in cases:
            async with asyncio.TaskGroup() as tasks:
                call = tasks.create_task(client.call_tool(name, arguments))
                reader, writer = await connections.get()
                assert decode_request_frame(await reader.readline()) == expected
                writer.write(encode_response(successful_response(paths, name, text)))
                await writer.drain()
                writer.close()
            assert _text(call.result()) == text
            assert connections.empty()
