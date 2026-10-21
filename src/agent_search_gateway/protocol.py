"""Migration-stable newline-delimited JSON socket protocol."""

import asyncio
import json
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import TypeAlias

from .errors import DaemonUnavailable, ErrorCode, ProtocolFailure
from .models import (
    ErrorResponse,
    KeywordSearchRequest,
    LLMSearchRequest,
    PaperSearchRequest,
    Request,
    Response,
    ShutdownRequest,
    SuccessResponse,
    URLFetchRequest,
)

DecodedRequest: TypeAlias = Request | ErrorResponse
RequestParser = Callable[[Mapping[str, object]], Request]

_MAX_REQUEST_FRAME_BYTES = 1 << 20
_MAX_RESPONSE_FRAME_BYTES = 8 << 20
_RESPONSE_TIMEOUT_SECONDS = 300.0


def _bad_request(message: str) -> ErrorResponse:
    return ErrorResponse(ErrorCode.BAD_REQUEST, message)


def _require_exact_keys(payload: Mapping[str, object], expected: set[str]) -> None:
    if set(payload) != expected:
        raise ValueError("request fields do not match schema")


def _optional_string(payload: Mapping[str, object], key: str) -> str | None:
    if key not in payload:
        return None
    value = payload[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _parse_keyword(payload: Mapping[str, object]) -> Request:
    if not {"type", "query"} <= set(payload) <= {"type", "query", "provider"}:
        raise ValueError("request fields do not match schema")
    query = payload["query"]
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    return KeywordSearchRequest(query, provider=_optional_string(payload, "provider"))


def _parse_paper(payload: Mapping[str, object]) -> Request:
    if not {"type", "query"} <= set(payload) <= {"type", "query", "provider"}:
        raise ValueError("request fields do not match schema")
    query = payload["query"]
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    return PaperSearchRequest(query, provider=_optional_string(payload, "provider"))


def _parse_llm(payload: Mapping[str, object]) -> Request:
    if not {"type", "prompt"} <= set(payload) <= {"type", "prompt", "scope", "provider", "model"}:
        raise ValueError("request fields do not match schema")
    prompt = payload["prompt"]
    if not isinstance(prompt, str):
        raise ValueError("prompt must be a string")
    provider = _optional_string(payload, "provider")
    model = _optional_string(payload, "model")
    scope = payload.get("scope", "web")
    if scope == "web":
        return LLMSearchRequest(prompt, "web", provider=provider, model=model)
    if scope == "paper":
        return LLMSearchRequest(prompt, "paper", provider=provider, model=model)
    if scope == "all":
        return LLMSearchRequest(prompt, "all", provider=provider, model=model)
    raise ValueError("scope must be web, paper, or all")


def _parse_fetch(payload: Mapping[str, object]) -> Request:
    _require_exact_keys(payload, {"type", "url", "focus"})
    url = payload["url"]
    focus = payload["focus"]
    if not isinstance(url, str):
        raise ValueError("url must be a string")
    if focus is not None and not isinstance(focus, str):
        raise ValueError("focus must be a string or null")
    return URLFetchRequest(url, focus)


def _parse_shutdown(payload: Mapping[str, object]) -> Request:
    _require_exact_keys(payload, {"type"})
    return ShutdownRequest()


_REQUEST_PARSERS: dict[str, RequestParser] = {
    "keyword_search": _parse_keyword,
    "paper_search": _parse_paper,
    "llm_search": _parse_llm,
    "url_fetch": _parse_fetch,
    "shutdown": _parse_shutdown,
}


def decode_request_frame(frame: bytes) -> DecodedRequest:
    try:
        text = frame.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _bad_request("Request must be valid UTF-8 JSON")
    if not isinstance(payload, dict):
        return _bad_request("Request must be a JSON object")
    request_type = payload.get("type")
    if not isinstance(request_type, str):
        return _bad_request("Request type must be a string")
    parser = _REQUEST_PARSERS.get(request_type)
    if parser is None:
        return _bad_request("Unknown request type")
    try:
        return parser(payload)
    except (KeyError, ValueError):
        return _bad_request("Request fields do not match schema")


class NDJSONDecoder:
    def __init__(self) -> None:
        self._buffer = bytearray()
        self._discarding_oversized_frame = False

    def feed(self, data: bytes) -> list[DecodedRequest]:
        decoded: list[DecodedRequest] = []
        remaining = data
        while remaining:
            if self._discarding_oversized_frame:
                boundary = remaining.find(b"\n")
                if boundary < 0:
                    return decoded
                self._discarding_oversized_frame = False
                remaining = remaining[boundary + 1 :]
                continue

            boundary = remaining.find(b"\n")
            if boundary < 0:
                capacity = _MAX_REQUEST_FRAME_BYTES - len(self._buffer)
                if len(remaining) > capacity:
                    self._buffer.clear()
                    self._discarding_oversized_frame = True
                    decoded.append(_bad_request("Request frame is too large"))
                    return decoded
                self._buffer.extend(remaining)
                return decoded

            if len(self._buffer) + boundary > _MAX_REQUEST_FRAME_BYTES:
                self._buffer.clear()
                decoded.append(_bad_request("Request frame is too large"))
                remaining = remaining[boundary + 1 :]
                continue

            self._buffer.extend(remaining[:boundary])
            frame = bytes(self._buffer)
            self._buffer.clear()
            decoded.append(decode_request_frame(frame))
            remaining = remaining[boundary + 1 :]
        return decoded


def _request_payload(request: Request) -> dict[str, object]:
    if isinstance(request, KeywordSearchRequest):
        payload: dict[str, object] = {"type": "keyword_search", "query": request.query}
    elif isinstance(request, PaperSearchRequest):
        payload = {"type": "paper_search", "query": request.query}
    elif isinstance(request, LLMSearchRequest):
        payload = {"type": "llm_search", "prompt": request.prompt}
        if request.scope != "web":
            payload["scope"] = request.scope
    elif isinstance(request, URLFetchRequest):
        return {"type": "url_fetch", "url": request.url, "focus": request.focus}
    else:
        return {"type": "shutdown"}
    if request.provider is not None:
        payload["provider"] = request.provider
    if isinstance(request, LLMSearchRequest) and request.model is not None:
        payload["model"] = request.model
    return payload


def _response_payload(response: Response) -> dict[str, object]:
    if isinstance(response, SuccessResponse):
        return {"ok": True, "text": response.text}
    return {"ok": False, "error": response.error.value, "message": response.message}


def _encode(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def encode_request(request: Request) -> bytes:
    return _encode(_request_payload(request))


def encode_response(response: Response) -> bytes:
    return _encode(_response_payload(response))


def parse_response_frame(frame: bytes) -> Response:
    try:
        text = frame.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _protocol_error("Response must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        raise _protocol_error("Response must contain boolean ok")
    if payload["ok"] is True:
        if set(payload) != {"ok", "text"} or not isinstance(payload.get("text"), str):
            raise _protocol_error("Success response fields do not match schema")
        return SuccessResponse(payload["text"])
    if set(payload) != {"ok", "error", "message"}:
        raise _protocol_error("Error response fields do not match schema")
    error = payload.get("error")
    message = payload.get("message")
    if not isinstance(error, str) or not isinstance(message, str):
        raise _protocol_error("Error response fields have invalid types")
    try:
        code = ErrorCode(error)
    except ValueError as exc:
        raise _protocol_error("Error response contains unknown error code") from exc
    return ErrorResponse(code, message)


def _protocol_error(message: str) -> ProtocolFailure:
    return ProtocolFailure(ErrorCode.PROTOCOL_ERROR, message)


async def send_request(
    socket_path: Path,
    request: Request,
    *,
    response_timeout_seconds: float = _RESPONSE_TIMEOUT_SECONDS,
) -> Response:
    try:
        reader, writer = await asyncio.open_unix_connection(
            path=socket_path,
            limit=_MAX_RESPONSE_FRAME_BYTES,
        )
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise DaemonUnavailable(str(socket_path)) from exc

    try:
        writer.write(encode_request(request))
        await writer.drain()
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=response_timeout_seconds)
        except TimeoutError as exc:
            raise _protocol_error("Timed out waiting for daemon response") from exc
        except ValueError as exc:
            raise _protocol_error("Daemon response frame is too large") from exc
        if not line or not line.endswith(b"\n"):
            raise _protocol_error("Daemon response ended before newline")
        return parse_response_frame(line[:-1])
    finally:
        writer.close()
        with suppress(ConnectionError, BrokenPipeError):
            await writer.wait_closed()
