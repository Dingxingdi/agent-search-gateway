"""Private stateless You.com MCP message construction and envelope decoding."""

import io
import json
from collections.abc import Mapping
from typing import Never

import httpx

from ... import __version__
from ...errors import ErrorCode, ProtocolFailure
from ..http import ProviderResponseFailure

_PROTOCOL_VERSION = "2026-07-28"
_FREE_ENDPOINT = "https://api.you.com/mcp/free"
_RPC_CODES = {
    -32700: "parse_error",
    -32600: "invalid_request",
    -32601: "method_not_found",
    -32602: "invalid_params",
    -32603: "internal_error",
    -32020: "header_mismatch",
    -32021: "missing_required_client_capability",
    -32022: "unsupported_protocol_version",
}


def tool_request(
    rpc_id: str, tool_name: str, arguments: Mapping[str, object]
) -> tuple[dict[str, str], dict[str, object]]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": _PROTOCOL_VERSION,
        "Mcp-Method": "tools/call",
        "Mcp-Name": tool_name,
    }
    body: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": dict(arguments),
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": _PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {
                    "name": "agent-search-gateway",
                    "version": __version__,
                },
            },
        },
    }
    return headers, body


def decode_tool_response(
    response: httpx.Response, rpc_id: str, provider: str, stage: str
) -> dict[str, object]:
    media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
    try:
        if media_type == "text/event-stream":
            envelope = _sse_response(response.content, provider, stage)
        elif media_type == "application/json":
            try:
                envelope = _load_json(response.content.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ProtocolFailure(
                    ErrorCode.PROTOCOL_ERROR,
                    f"{provider}/{stage}: invalid MCP JSON response",
                    diagnostics={"category": "decode", "semantic_result": "error"},
                ) from None
        else:
            raise _invalid_envelope(provider, stage)
        return _tool_payload(envelope, rpc_id, provider, stage, response.status_code)
    except (ValueError, RecursionError):
        raise _invalid_envelope(provider, stage) from None


def _reject_constant(_value: str) -> Never:
    raise ValueError("invalid JSON number")


def _load_json(data: str) -> object:
    return json.loads(data, parse_constant=_reject_constant)


def _tool_payload(
    envelope: object, rpc_id: str, provider: str, stage: str, status: int
) -> dict[str, object]:
    if (
        not isinstance(envelope, dict)
        or envelope.get("jsonrpc") != "2.0"
        or type(envelope.get("id")) is not type(rpc_id)
        or envelope.get("id") != rpc_id
        or "method" in envelope
        or ("result" in envelope) == ("error" in envelope)
    ):
        raise _invalid_envelope(provider, stage)
    if "error" in envelope:
        error = envelope["error"]
        if (
            not isinstance(error, dict)
            or type(error.get("code")) is not int
            or not isinstance(error.get("message"), str)
        ):
            raise _invalid_envelope(provider, stage)
        raise ProviderResponseFailure(
            provider,
            stage,
            status_code=status,
            retryable=False,
            safe_reason="MCP RPC error",
            diagnostics={"upstream_code": _RPC_CODES.get(error["code"], "unknown")},
        )
    result = envelope["result"]
    if not isinstance(result, dict) or result.get("resultType", "complete") != "complete":
        raise _invalid_envelope(provider, stage)
    is_error = result.get("isError", False)
    if type(is_error) is not bool:
        raise _invalid_envelope(provider, stage)
    if is_error:
        raise ProviderResponseFailure(
            provider,
            stage,
            status_code=status,
            retryable=False,
            safe_reason="MCP tool error",
            diagnostics={"upstream_code": "tool_error"},
        )
    content = result.get("content", [])
    if not isinstance(content, list) or any(
        not isinstance(block, dict)
        or block.get("type") != "text"
        or not isinstance(block.get("text"), str)
        for block in content
    ):
        raise _invalid_envelope(provider, stage)
    if "structuredContent" in result:
        payload = result["structuredContent"]
    else:
        if len(content) != 1:
            raise _invalid_envelope(provider, stage)
        payload = _load_json(content[0]["text"])
    if not isinstance(payload, dict):
        raise _invalid_envelope(provider, stage)
    return payload


def _sse_response(body: bytes, provider: str, stage: str) -> object:
    final = None
    data: list[str] = []
    # Universal newlines recognize CR, LF and CRLF without splitting Unicode text.
    for line in io.StringIO(body.decode("utf-8-sig"), newline=None):
        line = line.removesuffix("\n")
        if not line and data:
            message = _load_json("\n".join(data))
            data.clear()
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise _invalid_envelope(provider, stage)
            if "id" not in message:
                if (
                    final is not None
                    or not isinstance(message.get("method"), str)
                    or not isinstance(message.get("params", {}), dict)
                    or "result" in message
                    or "error" in message
                ):
                    raise _invalid_envelope(provider, stage)
            elif final is None:
                final = message
            elif json.dumps(final, sort_keys=True) != json.dumps(message, sort_keys=True):
                raise _invalid_envelope(provider, stage)
        field, _, value = line.partition(":")
        if field == "data":
            data.append(value.removeprefix(" "))
    if data or final is None:
        raise _invalid_envelope(provider, stage)
    return final


def _invalid_envelope(provider: str, stage: str) -> ProtocolFailure:
    return ProtocolFailure(
        ErrorCode.PROTOCOL_ERROR,
        f"{provider}/{stage}: invalid MCP response envelope",
        diagnostics={"category": "invalid_envelope", "semantic_result": "error"},
    )
