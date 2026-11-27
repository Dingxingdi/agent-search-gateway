"""Streamable HTTP behavior over a test-owned loopback listener."""

import json
from pathlib import Path
from unittest.mock import patch

import httpx2
import pytest
import uvicorn
from mcp import types
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from agent_search_gateway.mcp import server as gateway_server
from agent_search_gateway.mcp.adapter import GatewayAdapter
from agent_search_gateway.models import SuccessResponse
from agent_search_gateway.paths import RuntimePaths
from tests.mcp._support import RecordingClient, running_http


@pytest.mark.parametrize("backend_host", [None, "127.0.0.1", "localhost", "[::1]"])
async def test_http_endpoint_discovers_tools_and_calls_without_daemon_probe(
    mcp_home: Path, backend_host: str | None
) -> None:
    rpc = RecordingClient(SuccessResponse("hello\n"))
    server = gateway_server.create_server(GatewayAdapter(RuntimePaths.from_home(mcp_home), rpc))
    headers = (
        {} if backend_host is None else {"Host": backend_host, "Origin": f"http://{backend_host}"}
    )
    async with (
        running_http(gateway_server.create_http_app(server, "127.0.0.1")) as url,
        httpx2.AsyncClient(trust_env=False, headers=headers) as http,
    ):
        transport = streamable_http_client(f"{url}/mcp", http_client=http)
        async with Client(transport, cache=None) as client:
            assert {tool.name for tool in (await client.list_tools()).tools} == {
                "keyword_search",
                "paper_search",
                "llm_search",
                "agent_search",
                "url_fetch",
            }
            assert not rpc.calls and not list(mcp_home.iterdir())
            result = await client.call_tool("url_fetch", {"url": "https://example.test/item"})
            assert not result.is_error
            assert result.content == [types.TextContent(type="text", text="hello\n")]
            assert len(rpc.calls) == 1
        for path in ("/", "/sse", "/search", "/health"):
            assert (await http.get(url + path)).status_code == 404


@pytest.mark.parametrize("bind_host", ["127.0.0.1", "0.0.0.0"])
async def test_host_origin_checks_precede_adapter_for_forwarded_calls(
    mcp_home: Path, bind_host: str
) -> None:
    rpc = RecordingClient(SuccessResponse("ok"))
    adapter = GatewayAdapter(RuntimePaths.from_home(mcp_home), rpc)
    app = gateway_server.create_http_app(gateway_server.create_server(adapter), bind_host)
    captured: list[tuple[dict[str, str], bytes]] = []

    async def capture(request: httpx2.Request) -> None:
        if request.method == "POST" and json.loads(request.content).get("method") == "tools/call":
            captured.append((dict(request.headers), request.content))

    # These are the backend headers sent by a proxy after it validates its frontend request.
    backend = {"Host": "localhost:8765", "Origin": "http://localhost:8765"}
    async with (
        running_http(app) as url,
        httpx2.AsyncClient(
            trust_env=False, headers=backend, event_hooks={"request": [capture]}
        ) as http,
    ):
        endpoint = f"{url}/mcp"
        async with Client(streamable_http_client(endpoint, http_client=http), cache=None) as client:
            result = await client.call_tool("url_fetch", {"url": "https://example.test/item"})
            assert not result.is_error and len(rpc.calls) == 1
            ((headers, body),) = captured
            payload = json.loads(body)
            payload["id"] = "replay-request"
            headers.pop("content-length", None)
            with patch.object(GatewayAdapter, "url_fetch", wraps=adapter.url_fetch) as fetch:
                for changed, status in (
                    ({"host": "external.test:8765"}, 421),
                    ({"origin": "null"}, 403),
                    ({"origin": "https://external.test"}, 403),
                ):
                    response = await http.post(endpoint, headers=headers | changed, json=payload)
                    assert response.status_code == status
                fetch.assert_not_called()
                assert len(rpc.calls) == 1
                assert (await http.post(endpoint, headers=headers, json=payload)).status_code == 200
                ipv6 = headers | {"host": "[::1]:8765", "origin": "http://[::1]:8765"}
                payload["id"] = "ipv6-request"
                assert (await http.post(endpoint, headers=ipv6, json=payload)).status_code == 200
                assert fetch.call_count == 2 and len(rpc.calls) == 3


def test_nonloopback_bind_warns_without_disabling_backend_protection(
    mcp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch.object(uvicorn, "run") as listen:
        gateway_server.run_server(
            paths=RuntimePaths.from_home(mcp_home),
            transport="streamable-http",
            host="0.0.0.0",
            port=8765,
        )
    listen.assert_called_once()
    assert isinstance(listen.call_args.args[0], Starlette)
    assert listen.call_args.kwargs["host"] == "0.0.0.0"
    assert listen.call_args.kwargs["port"] == 8765
    assert listen.call_args.kwargs["access_log"] is False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "non-loopback" in captured.err.lower() and "proxy" in captured.err.lower()
    assert list(mcp_home.iterdir()) == []
