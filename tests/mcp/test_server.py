import asyncio
from pathlib import Path

import pytest
from mcp import types
from mcp.shared.exceptions import MCPError

from agent_search_gateway.errors import DaemonUnavailable, ErrorCode
from agent_search_gateway.mcp.adapter import GatewayAdapter
from agent_search_gateway.models import (
    AgentSearchRequest,
    ErrorResponse,
    KeywordSearchRequest,
    LLMSearchRequest,
    PaperSearchRequest,
    Request,
    SuccessResponse,
    URLFetchRequest,
)
from agent_search_gateway.paths import RuntimePaths
from tests.mcp._support import RecordingClient, gateway_client, successful_response


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths.from_home(tmp_path)


async def test_discovery_tool_contract(paths: RuntimePaths) -> None:
    rpc = RecordingClient()
    async with gateway_client(paths, rpc) as client:
        tools = (await client.list_tools()).tools

    assert sorted(tool.name for tool in tools) == [
        "agent_search",
        "keyword_search",
        "llm_search",
        "paper_search",
        "url_fetch",
    ]
    assert rpc.calls == []
    for tool in tools:
        assert "outputSchema" not in tool.model_dump(by_alias=True, exclude_none=True)
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        description = (tool.description or "").lower()
        properties = schema["properties"]
        if tool.name == "url_fetch":
            required, nullable = "url", {"focus"}
            assert "admit" in description and "text" in description
        else:
            assert "jsonl" in description and "alias" in description
            if tool.name in {"keyword_search", "paper_search"}:
                required, nullable = "query", {"provider"}
            else:
                required, nullable = "prompt", {"provider", "model"}
                assert set(properties["scope"]["enum"]) == {"web", "paper", "all"}
                assert properties["scope"]["default"] == "web"
        assert schema["required"] == [required]
        assert properties[required]["type"] == "string"
        expected = {required} | nullable
        if required == "prompt":
            expected.add("scope")
        assert set(properties) == expected
        for key in nullable:
            assert properties[key]["default"] is None
            assert {choice["type"] for choice in properties[key]["anyOf"]} == {"string", "null"}


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("private-marker-tool", {}),
        ("keyword_search", {"query": "private-marker", "extra": True}),
        ("paper_search", {"query": 12}),
        ("keyword_search", {"query": True}),
        ("paper_search", {"query": None}),
        ("keyword_search", {}),
        ("llm_search", {"prompt": ["private-marker"]}),
        ("agent_search", {"prompt": {"private-marker": 1}}),
        ("llm_search", {"prompt": None}),
        ("agent_search", {}),
        ("llm_search", {"prompt": "ok", "scope": "WEB"}),
        ("agent_search", {"prompt": "ok", "scope": " web "}),
        ("agent_search", {"prompt": "ok", "scope": None}),
        ("llm_search", {"prompt": "ok", "model": []}),
        ("keyword_search", {"query": "ok", "provider": 12}),
        ("url_fetch", {"url": "https://example.org/", "focus": False}),
        ("url_fetch", {"url": "https://example.org/", "provider": None}),
        ("url_fetch", {"url": "https://example.org/", "model": "private-marker"}),
        ("url_fetch", {"url": None}),
        ("url_fetch", {}),
    ],
)
async def test_schema_rejects_invalid_calls_before_rpc(
    paths: RuntimePaths, name: str, arguments: dict[str, object]
) -> None:
    rpc = RecordingClient()
    async with gateway_client(paths, rpc) as client:
        with pytest.raises(MCPError) as rejected:
            await client.call_tool(name, arguments)
        assert rpc.calls == []
        rpc.response = SuccessResponse("recovered")
        recovered = await client.call_tool("url_fetch", {"url": "https://example.org/"})

    assert not recovered.is_error
    assert recovered.content == [types.TextContent(type="text", text="recovered")]
    assert rejected.value.code == types.INVALID_PARAMS
    assert "private-marker" not in rejected.value.message
    assert "Traceback" not in rejected.value.message
    assert rejected.value.data is None


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        (
            "keyword_search",
            {"query": " q ", "provider": " null "},
            KeywordSearchRequest("q", "null"),
        ),
        ("paper_search", {"query": " q ", "provider": " [] "}, PaperSearchRequest("q", "[]")),
        (
            "llm_search",
            {"prompt": " p ", "provider": " [] ", "model": " null "},
            LLMSearchRequest("p", "web", "[]", "null"),
        ),
        (
            "agent_search",
            {"prompt": " p ", "scope": "all", "provider": " Alpha ", "model": " [] "},
            AgentSearchRequest("p", "all", "Alpha", "[]"),
        ),
        (
            "url_fetch",
            {"url": "https://example.org/", "focus": " null "},
            URLFetchRequest("https://example.org/", "null"),
        ),
        (
            "url_fetch",
            {"url": "https://example.org/", "focus": " [] "},
            URLFetchRequest("https://example.org/", "[]"),
        ),
    ],
)
@pytest.mark.parametrize("text", [' {"title":"雪"}\r\n{"url":"https://example.org/"}\n', ""])
async def test_literal_strings_and_exact_result_for_each_tool(
    paths: RuntimePaths, name: str, arguments: dict[str, object], expected: Request, text: str
) -> None:
    rpc = RecordingClient(successful_response(paths, name, text))
    async with gateway_client(paths, rpc) as client:
        result = await client.call_tool(name, arguments)

    assert rpc.calls == [(paths.socket_file, expected)]
    assert result.model_dump(
        include={"content", "is_error", "structured_content"}, by_alias=True, exclude_none=True
    ) == {
        "content": [{"type": "text", "text": text}],
        "isError": False,
    }


_PRIVATE = "synthetic-secret /synthetic/private/path"


async def assert_failure_then_recovery(
    paths: RuntimePaths, rpc: RecordingClient, expected_message: str
) -> None:
    async with gateway_client(paths, rpc) as client:
        result = await client.call_tool("keyword_search", {"query": _PRIVATE})
        rpc.response = SuccessResponse("recovered")
        recovered = await client.call_tool("url_fetch", {"url": "https://example.org/"})

    assert result.is_error
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    assert expected_message in block.text.lower()
    assert result.model_dump(
        include={"content", "is_error", "structured_content"}, by_alias=True, exclude_none=True
    ) == {
        "content": [{"type": "text", "text": block.text}],
        "isError": True,
    }
    for private in (
        "synthetic-secret",
        "/synthetic/private/path",
        str(paths.results_dir),
        "Traceback",
    ):
        assert private not in block.text
    assert not recovered.is_error
    assert recovered.content == [types.TextContent(type="text", text="recovered")]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        *((ErrorResponse(code, _PRIVATE), code.value) for code in ErrorCode),
        (DaemonUnavailable(_PRIVATE), "agent-search-gateway start"),
        (RuntimeError(_PRIVATE), "unexpected"),
    ],
)
async def test_business_error_and_subsequent_call(
    paths: RuntimePaths,
    response: ErrorResponse | Exception,
    message: str,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rpc = RecordingClient(response)
    await assert_failure_then_recovery(paths, rpc, message)
    assert len(rpc.calls) == 2
    diagnostics = [
        r.getMessage() for r in caplog.records if r.name.startswith("agent_search_gateway")
    ]
    diagnostics.extend(capsys.readouterr().err.splitlines())
    assert len(diagnostics) == int(isinstance(response, RuntimeError))
    for line in diagnostics:
        assert "synthetic-secret" not in line
        assert "/synthetic/private/path" not in line
        assert "Traceback" not in line


async def test_missing_result_and_subsequent_call(paths: RuntimePaths) -> None:
    paths.results_dir.mkdir(parents=True)
    rpc = RecordingClient(SuccessResponse(str(paths.results_dir / "keyword-1234abcd.jsonl")))
    await assert_failure_then_recovery(paths, rpc, "result unavailable")
    assert len(rpc.calls) == 2


async def test_unexpected_server_error_and_subsequent_call(
    paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fail(self: GatewayAdapter, query: str, provider: str | None = None) -> str:
        raise RuntimeError(_PRIVATE)

    monkeypatch.setattr(GatewayAdapter, "keyword_search", fail)
    rpc = RecordingClient()
    await assert_failure_then_recovery(paths, rpc, "unexpected")
    assert len(rpc.calls) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert len(captured.err.splitlines()) == 1
    assert "synthetic-secret" not in captured.err
    assert "/synthetic/private/path" not in captured.err
    assert "Traceback" not in captured.err


async def test_cancelled_call_propagates_without_tool_result(paths: RuntimePaths) -> None:
    rpc = RecordingClient()
    async with gateway_client(paths, rpc) as client:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(client.call_tool("keyword_search", {"query": "cancel"}), 5)
    assert len(rpc.calls) == 1
