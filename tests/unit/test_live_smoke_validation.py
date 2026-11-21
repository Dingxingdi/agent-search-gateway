"""Offline coverage for live probe response validation and LLM configuration."""

import json
from collections.abc import AsyncIterator, Mapping

import httpx
import pytest

from agent_search_gateway.providers.web import youcom
from agent_search_gateway.providers.web._youcom_mcp import tool_request
from tests.integration import test_live_new_web_providers as web_smoke
from tests.integration.test_live_rate_limits_and_inputs import (
    _agent_reply_error,
    _llm_probe_settings,
)


def test_llm_smoke_prefers_configured_openrouter_free_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)

    key, model, base = _llm_probe_settings()

    assert key.reveal() == "test-openrouter-key"
    assert model == "openrouter/free"
    assert base == "https://openrouter.ai/api"


def test_llm_smoke_respects_openrouter_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "example/model:free")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://example.test/api/v1")

    _, model, base = _llm_probe_settings()

    assert model == "example/model:free"
    assert base == "https://example.test/api/v1"


def test_llm_smoke_uses_openai_without_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com")

    key, model, base = _llm_probe_settings()

    assert key.reveal() == "test-openai-key"
    assert model == "gpt-4o-mini"
    assert base == "https://api.openai.com"


@pytest.mark.parametrize(
    "reply",
    [
        '{"ok": false}',
        '```json\n{"ok": true}\n```',
        '  ```JSON\r\n{"ok": false, "reason": "short"}\r\n```\n',
        '```\n{"ok": true}\n```',
    ],
)
def test_agent_smoke_accepts_one_json_decision(reply: str) -> None:
    assert _agent_reply_error(reply) is None


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        ('Explanation\n```json\n{"ok": true}\n```', "invalid_json"),
        ('```json\n{"ok": true}\n```\nExtra', "invalid_json"),
        ('```json\n{"ok": true}\n```\n```json\n{"ok": false}\n```', "invalid_json"),
        ('```text\n{"ok": true}\n```', "invalid_json"),
        ('```json\n{"ok": true}', "invalid_json"),
        ('```json\n{"ok": true,}\n```', "invalid_json"),
        ('{"ok": true, "value": NaN}', "invalid_json"),
        ('[{"ok": true}]', "response_not_object"),
        ("```json\n{}\n```", "invalid_ok_field"),
        ('{"ok": 1}', "invalid_ok_field"),
    ],
)
def test_agent_smoke_classifies_invalid_decisions(reply: str, error: str) -> None:
    assert _agent_reply_error(reply) == error


@pytest.mark.parametrize("search_sets_cookie", [False, True], ids=["stateless", "search-cookie"])
async def test_youcom_mcp_smoke_uses_search_url_for_extract(
    search_sets_cookie: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("YDC_API_KEY", "PRIVATE_AUTH")
    target = "https://example.test/PRIVATE_TARGET"
    calls: list[tuple[str, dict[str, object]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        tool = message["params"]["name"]
        calls.append((tool, message["params"]["arguments"]))
        assert str(request.url) == "https://api.you.com/mcp/free"
        assert "PRIVATE_AUTH" not in request.headers.values()
        assert "cookie" not in request.headers
        payload: object
        if tool == "you-search":
            rows = [
                {"url": target, "title": "PRIVATE_TITLE", "contents": {"highlights": ["PRIVATE"]}},
                {"url": "https://example.test/other"},
            ]
            payload = {"results": {"web": rows}}
        else:
            payload = {
                "output": {"url": target, "contents": {"full_page": {"markdown": "PRIVATE_BODY"}}}
            }
        headers = (
            {"Set-Cookie": "edge=PRIVATE_COOKIE; Path=/; Secure"}
            if search_sets_cookie and tool == "you-search"
            else {}
        )
        return httpx.Response(
            200,
            headers=headers,
            json={"jsonrpc": "2.0", "id": message["id"], "result": {"structuredContent": payload}},
        )

    assert await web_smoke._run_youcom_mcp("PRIVATE_QUERY", httpx.MockTransport(respond))
    assert calls == [
        ("you-search", {"query": "PRIVATE_QUERY", "count": 10}),
        ("you-extract", {"url": target}),
    ]
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err
    search, extract = map(json.loads, captured.out.splitlines())
    for stage, row in (("search", search), ("extract", extract)):
        assert row == {
            "provider": "youcom",
            "protocol": "mcp",
            "protocol_version": "2026-07-28",
            "stage": stage,
            "status": "PASS",
            "category": "success",
            "request_count": 1,
            "elapsed_ms": row["elapsed_ms"],
            **({"hits": 2} if stage == "search" else {"content_length": len("PRIVATE_BODY")}),
        }
        assert isinstance(row["elapsed_ms"], int) and row["elapsed_ms"] >= 0


@pytest.mark.parametrize(
    ("destination", "headers"),
    [
        ("https://other.test/mcp/free", {}),
        (str(httpx.URL(scheme="https", host="api.you.com", path="/mcp")), {}),
        ("https://api.you.com/mcp/free?", {}),
        ("https://api.you.com/mcp/free/", {}),
        ("https://api.you.com/mcp/free#PRIVATE", {}),
        ("https://api.you.com/mcp/free?query=PRIVATE", {}),
        (str(httpx.URL(web_smoke._MCP_ENDPOINT).copy_with(username="PRIVATE")), {}),
        ("https://api.you.com:8443/mcp/free", {}),
        (None, {"Authorization": "Payment PRIVATE"}),
        (None, {"X-API-Key": "PRIVATE"}),
        (None, {"Payment-Signature": "PRIVATE"}),
        (None, {"X-Payment": "PRIVATE"}),
        (None, {"Cookie": "PRIVATE"}),
        (None, {"Mcp-Session-Id": "PRIVATE"}),
        (None, {"Mcp-Method": "tools/list"}),
        (None, {"Proxy-Authorization": "PRIVATE"}),
        (None, {"Mcp-Name": "you-extract"}),
        (None, {"MCP-Protocol-Version": "PRIVATE"}),
        (None, {"Host": "other.test"}),
        (None, {"Content-Type": "text/plain"}),
        (None, {"Accept": "text/html"}),
    ],
)
async def test_youcom_mcp_smoke_blocks_unexpected_destinations(
    destination: str | None,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dispatched = 0

    def altered(
        rpc_id: str, name: str, args: Mapping[str, object]
    ) -> tuple[dict[str, str], dict[str, object]]:
        expected, body = tool_request(rpc_id, name, args)
        return {**expected, **headers}, body

    if destination is not None:
        monkeypatch.setattr(youcom, "_FREE_ENDPOINT", destination)
    monkeypatch.setattr(youcom, "tool_request", altered)

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal dispatched
        dispatched += 1
        return httpx.Response(500)

    assert not await web_smoke._run_youcom_mcp("PRIVATE_QUERY", httpx.MockTransport(respond))
    assert dispatched == 0
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err
    search, extract = map(json.loads, output.out.splitlines())
    assert (search["status"], search["category"], search["request_count"]) == (
        "FAIL",
        "request_guard",
        0,
    )
    assert (extract["status"], extract["request_count"]) == ("SKIP", 0)


@pytest.mark.parametrize(
    ("case", "failed_stage", "category"),
    [
        ("empty", "search", "empty"),
        ("invalid_url", "search", "empty"),
        ("malformed_search", "search", "invalid_envelope"),
        ("quota", "search", "rate_limited"),
        ("redirect", "search", "http_status"),
        ("wrong_id", "search", "invalid_envelope"),
        ("rpc", "search", "rpc_error"),
        ("tool", "extract", "tool_error"),
        ("page_empty", "extract", "empty"),
        ("page_mismatch", "extract", "no_match"),
        ("transport", "extract", "transport"),
    ],
)
async def test_youcom_mcp_smoke_reports_contract_failure_safely(
    case: str, failed_stage: str, category: str, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    target = "https://example.test/PRIVATE_TARGET"

    def respond(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        stage = message["params"]["name"].removeprefix("you-")
        calls.append(stage)
        headers = {"Mcp-Session-Id": "PRIVATE_SESSION", "MCP-Protocol-Version": "PRIVATE_VERSION"}
        rpc_id = message["id"]
        if stage == failed_stage:
            if case in {"quota", "redirect"}:
                return httpx.Response(
                    429 if case == "quota" else 307,
                    headers={**headers, "Location": "https://example.test/PRIVATE_LOCATION"},
                    text="PRIVATE_ERROR",
                )
            if case == "rpc":
                return httpx.Response(
                    200,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "error": {"code": -32602, "message": "PRIVATE_ERROR"},
                    },
                )
            if case == "tool":
                return httpx.Response(
                    200,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": "PRIVATE_ERROR"}],
                        },
                    },
                )
            if case == "transport":
                raise httpx.ConnectError("PRIVATE_EXCEPTION")
            if case == "wrong_id":
                rpc_id = "PRIVATE_ID"
        payload: object
        if stage == "search":
            rows = [{"url": target, "title": "PRIVATE_TITLE"}]
            if case == "empty":
                rows = []
            elif case == "invalid_url":
                rows = [{"url": "PRIVATE_INVALID"}]
            payload = {} if case == "malformed_search" else {"results": {"web": rows}}
        else:
            payload = {
                "output": {
                    "url": target if case == "page_empty" else "https://example.test/PRIVATE_WRONG",
                    "contents": {
                        "full_page": {"markdown": " " if case == "page_empty" else "PRIVATE_BODY"}
                    },
                }
            }
        return httpx.Response(
            200,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {"structuredContent": payload},
            },
        )

    assert not await web_smoke._run_youcom_mcp("PRIVATE_QUERY", httpx.MockTransport(respond))
    assert calls == (["search"] if failed_stage == "search" else ["search", "extract"])
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err
    search, extract = map(json.loads, captured.out.splitlines())
    failed = search if failed_stage == "search" else extract
    assert (failed["stage"], failed["status"], failed["category"], failed["request_count"]) == (
        failed_stage,
        "FAIL",
        category,
        1,
    )
    if failed_stage == "search":
        assert (extract["status"], extract["category"], extract["request_count"]) == (
            "SKIP",
            "search_failed",
            0,
        )
    else:
        assert (search["status"], search["request_count"]) == ("PASS", 1)


async def test_youcom_mcp_smoke_keeps_error_body_unread(capsys: pytest.CaptureFixture[str]) -> None:
    class ErrorStream(httpx.AsyncByteStream):
        read = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            self.read = True
            yield b"PRIVATE_RESPONSE"

    stream = ErrorStream()
    transport = httpx.MockTransport(lambda _: httpx.Response(429, stream=stream))
    assert not await web_smoke._run_youcom_mcp("PRIVATE_QUERY", transport)
    assert not stream.read
    assert "PRIVATE" not in capsys.readouterr().out
