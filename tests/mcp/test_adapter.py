import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import TypedDict, cast

import pytest

from agent_search_gateway.errors import (
    UNAVAILABLE_MESSAGE,
    DaemonUnavailable,
    ErrorCode,
    GatewayError,
)
from agent_search_gateway.mcp.adapter import AdapterFailure, GatewayAdapter
from agent_search_gateway.models import (
    AgentSearchRequest,
    ErrorResponse,
    KeywordSearchRequest,
    LLMSearchRequest,
    PaperSearchRequest,
    Request,
    Response,
    SearchScope,
    SuccessResponse,
    URLFetchRequest,
)
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.protocol import decode_request_frame, encode_response
from tests.mcp._support import RecordingClient, unix_peer


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths.from_home(tmp_path)


@pytest.fixture
def client() -> RecordingClient:
    return RecordingClient()


@pytest.fixture
def adapter(paths: RuntimePaths, client: RecordingClient) -> GatewayAdapter:
    return GatewayAdapter(paths, client)


async def assert_request(
    operation: Awaitable[str],
    paths: RuntimePaths,
    client: RecordingClient,
    expected: Request,
) -> None:
    with suppress(asyncio.CancelledError):
        await operation
    assert client.calls == [(paths.socket_file, expected)]


@pytest.mark.parametrize("request_type", [KeywordSearchRequest, PaperSearchRequest])
@pytest.mark.parametrize(
    ("selectors", "provider"),
    [({}, None), ({"provider": None}, None), ({"provider": "  AliasMixed  "}, "AliasMixed")],
)
async def test_query_request_mapping(
    adapter: GatewayAdapter,
    paths: RuntimePaths,
    client: RecordingClient,
    request_type: type[KeywordSearchRequest] | type[PaperSearchRequest],
    selectors: dict[str, str | None],
    provider: str | None,
) -> None:
    search = (
        adapter.keyword_search if request_type is KeywordSearchRequest else adapter.paper_search
    )
    await assert_request(
        search("  two words\t", **selectors), paths, client, request_type("two words", provider)
    )


class PromptOptions(TypedDict, total=False):
    scope: SearchScope
    provider: str | None
    model: str | None


@pytest.mark.parametrize("request_type", [LLMSearchRequest, AgentSearchRequest])
@pytest.mark.parametrize(
    "scope", [None, "web", "paper", "all"], ids=["default", "web", "paper", "all"]
)
@pytest.mark.parametrize(
    ("options", "provider", "model"),
    [
        ({}, None, None),
        ({"provider": None, "model": None}, None, None),
        ({"provider": "  AliasX "}, "AliasX", None),
        ({"model": " Model/Y "}, None, "Model/Y"),
        ({"provider": " AliasX ", "model": " Model/Y "}, "AliasX", "Model/Y"),
    ],
    ids=["omitted", "null", "provider-only", "model-only", "both"],
)
async def test_prompt_request_mapping(
    adapter: GatewayAdapter,
    paths: RuntimePaths,
    client: RecordingClient,
    request_type: type[LLMSearchRequest] | type[AgentSearchRequest],
    scope: SearchScope | None,
    options: PromptOptions,
    provider: str | None,
    model: str | None,
) -> None:
    search = adapter.llm_search if request_type is LLMSearchRequest else adapter.agent_search
    arguments = options.copy()
    if scope is not None:
        arguments["scope"] = scope
    await assert_request(
        search("  Keep  spacing\n", **arguments),
        paths,
        client,
        request_type("Keep  spacing", scope or "web", provider, model),
    )


@pytest.mark.parametrize(
    ("options", "focus"),
    [({}, None), ({"focus": None}, None), ({"focus": " \t"}, None), ({"focus": "  A B "}, "A B")],
    ids=["omitted", "null", "blank", "trimmed"],
)
async def test_url_fetch_request_mapping(
    adapter: GatewayAdapter,
    paths: RuntimePaths,
    client: RecordingClient,
    options: dict[str, str | None],
    focus: str | None,
) -> None:
    await assert_request(
        adapter.url_fetch("  HTTPS://EXAMPLE.Test/Path?A=B#C ", **options),
        paths,
        client,
        URLFetchRequest("https://example.test/Path?A=B#C", focus),
    )


@pytest.mark.parametrize(
    ("invoke", "code"),
    [
        pytest.param(lambda a: a.keyword_search(""), ErrorCode.EMPTY_QUERY, id="empty-query"),
        pytest.param(lambda a: a.paper_search(" \t"), ErrorCode.EMPTY_QUERY, id="blank-query"),
        pytest.param(lambda a: a.llm_search(" "), ErrorCode.EMPTY_QUERY, id="blank-prompt"),
        pytest.param(lambda a: a.agent_search(""), ErrorCode.EMPTY_QUERY, id="empty-prompt"),
        pytest.param(
            lambda a: a.keyword_search("q", ""),
            ErrorCode.BAD_REQUEST,
            id="keyword-alias",
        ),
        pytest.param(lambda a: a.paper_search("q", " \t"), ErrorCode.BAD_REQUEST, id="paper-alias"),
        pytest.param(
            lambda a: a.llm_search("p", provider=" "),
            ErrorCode.BAD_REQUEST,
            id="llm-alias",
        ),
        pytest.param(lambda a: a.llm_search("p", model=""), ErrorCode.BAD_REQUEST, id="llm-model"),
        pytest.param(
            lambda a: a.agent_search("p", provider=""),
            ErrorCode.BAD_REQUEST,
            id="agent-alias",
        ),
        pytest.param(
            lambda a: a.agent_search("p", model=" "),
            ErrorCode.BAD_REQUEST,
            id="agent-model",
        ),
        pytest.param(
            lambda a: a.llm_search("p", scope=cast(SearchScope, " web ")),
            ErrorCode.BAD_REQUEST,
            id="padded-scope",
        ),
        pytest.param(
            lambda a: a.agent_search("p", scope=cast(SearchScope, "ALL")),
            ErrorCode.BAD_REQUEST,
            id="uppercase-scope",
        ),
        pytest.param(
            lambda a: a.url_fetch("https://example.test:private-input/"),
            ErrorCode.INVALID_URL,
            id="invalid-url-port",
        ),
    ],
)
async def test_input_validation(
    adapter: GatewayAdapter,
    client: RecordingClient,
    invoke: Callable[[GatewayAdapter], Awaitable[str]],
    code: ErrorCode,
) -> None:
    try:
        with pytest.raises(AdapterFailure) as caught:
            await invoke(adapter)
    finally:
        assert client.calls == []
    assert caught.value.code is code
    assert caught.value.safe_message
    assert "private-input" not in caught.value.safe_message


@pytest.mark.parametrize(
    "body",
    ["  正文\r\nnext line\n", "", UNAVAILABLE_MESSAGE, "/reports/keyword-01234567.jsonl"],
    ids=["text", "empty", "unavailable-message", "absolute-path-text"],
)
async def test_fetch_text(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    client = RecordingClient(SuccessResponse(body))
    adapter = GatewayAdapter(paths, client)

    def fail_open(*args: object, **kwargs: object) -> None:
        pytest.fail("Fetch attempted to open a file")

    with monkeypatch.context() as guard:
        for boundary in ("builtins.open", "io.open", "os.open"):
            guard.setattr(boundary, fail_open)
        assert await adapter.url_fetch("https://example.test/") == body

    assert client.calls == [(paths.socket_file, URLFetchRequest("https://example.test/"))]


_PRIVATE_DETAIL = "/fixture/private token=fixture-secret"
_GUIDANCE = {
    ErrorCode.BAD_REQUEST: "request",
    ErrorCode.EMPTY_QUERY: "query",
    ErrorCode.INVALID_URL: "url",
    ErrorCode.URL_NOT_ADMITTED: "search",
    ErrorCode.NO_KEYWORD_SEARCH_PROVIDERS: "keyword",
    ErrorCode.NO_LLM_SEARCH_PROVIDERS: "llm",
    ErrorCode.NO_AGENT_SEARCH_PROVIDERS: "agent",
    ErrorCode.NO_URL_FETCH_PROVIDERS: "fetch",
    ErrorCode.NO_ACADEMIC_SEARCH_PROVIDERS: "academic",
    ErrorCode.ALL_PROVIDERS_FAILED: "provider",
    ErrorCode.LLM_STAGE_FAILED: "llm",
    ErrorCode.PROTOCOL_ERROR: "daemon",
    ErrorCode.CONFIG_ERROR: "config",
    ErrorCode.DAEMON_SHUTTING_DOWN: "shutting down",
}


@pytest.mark.parametrize("code", list(ErrorCode))
@pytest.mark.parametrize("local", [False, True], ids=["response", "typed-exception"])
async def test_gateway_error_preserves_code_and_guidance(
    paths: RuntimePaths, code: ErrorCode, local: bool, caplog: pytest.LogCaptureFixture
) -> None:
    outcome = GatewayError(code, _PRIVATE_DETAIL) if local else ErrorResponse(code, _PRIVATE_DETAIL)
    client = RecordingClient(outcome)
    with pytest.raises(AdapterFailure) as caught:
        await GatewayAdapter(paths, client).keyword_search("private-query")

    assert client.calls == [(paths.socket_file, KeywordSearchRequest("private-query"))]
    assert caught.value.code is code
    assert _GUIDANCE[code] in caught.value.safe_message.lower()
    for private in ("/fixture/private", "fixture-secret", "private-query"):
        assert private not in str(caught.value) + caplog.text


@pytest.mark.parametrize(
    ("outcome", "code", "guidance"),
    [
        (DaemonUnavailable(_PRIVATE_DETAIL), None, "agent-search-gateway start"),
        (PermissionError(_PRIVATE_DETAIL), None, "daemon"),
        (ConnectionResetError(_PRIVATE_DETAIL), None, "daemon"),
        (BrokenPipeError(_PRIVATE_DETAIL), None, "daemon"),
        (cast(Response, _PRIVATE_DETAIL), ErrorCode.PROTOCOL_ERROR, "daemon"),
        (RuntimeError(_PRIVATE_DETAIL), None, "gateway"),
    ],
    ids=["unavailable", "permission", "reset", "broken-pipe", "unexpected-response", "unexpected"],
)
async def test_client_error_safe_failure(
    paths: RuntimePaths,
    outcome: Exception | Response,
    code: ErrorCode | None,
    guidance: str,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = RecordingClient(outcome)
    with pytest.raises(AdapterFailure) as caught:
        await GatewayAdapter(paths, client).llm_search("private-prompt")

    assert client.calls == [(paths.socket_file, LLMSearchRequest("private-prompt"))]
    assert caught.value.code is code
    assert guidance in caught.value.safe_message.lower()
    out, err = capsys.readouterr()
    assert not out
    if isinstance(outcome, RuntimeError):
        assert caplog.text or err
    for private in ("/fixture/private", "fixture-secret", "private-prompt", "Traceback"):
        assert private not in str(caught.value) + caplog.text + err


@pytest.mark.parametrize(
    "case", ["malformed", "unknown-code", "oversized", "missing-newline", "eof"]
)
async def test_protocol_error_from_unix_peer(paths: RuntimePaths, case: str) -> None:
    expected = URLFetchRequest("https://example.test/")
    async with asyncio.timeout(10), unix_peer(paths) as (isolated, connections):

        async def reply() -> None:
            reader, writer = await connections.get()
            assert decode_request_frame(await reader.readline()) == expected
            if case == "oversized":
                # Valid JSON whose response exceeds the existing 8 MiB socket frame limit.
                frame = encode_response(SuccessResponse("x" * (8 << 20)))
            else:
                frame = {
                    "malformed": b"fixture-secret\n",
                    "unknown-code": b'{"ok":false,"error":"unknown","message":"fixture-secret"}\n',
                    "missing-newline": b'{"ok":true,"text":"fixture-secret"}',
                    "eof": b"",
                }[case]
            writer.write(frame)
            await writer.drain()
            writer.close()

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(reply())
            with pytest.raises(AdapterFailure) as caught:
                await GatewayAdapter(isolated).url_fetch(expected.url)
        assert caught.value.code is ErrorCode.PROTOCOL_ERROR
        assert "fixture-secret" not in caught.value.safe_message
        assert connections.empty()


async def test_concurrent_calls_enter_without_serializing(paths: RuntimePaths) -> None:
    # Both requests must reach the peer before it replies to either.
    async with (
        asyncio.timeout(5),
        unix_peer(paths) as (isolated, connections),
        asyncio.TaskGroup() as tasks,
    ):
        adapter = GatewayAdapter(isolated)
        first = tasks.create_task(adapter.url_fetch("https://example.test/one"))
        second = tasks.create_task(adapter.url_fetch("https://example.test/two"))
        requests = []
        for _ in range(2):
            reader, writer = await connections.get()
            request = decode_request_frame(await reader.readline())
            assert isinstance(request, URLFetchRequest)
            requests.append((request.url, writer))
        for url, writer in requests:
            writer.write(encode_response(SuccessResponse(url)))
            await writer.drain()
        assert await first == "https://example.test/one"
        assert await second == "https://example.test/two"
        assert connections.empty()


async def test_cancel_waiting_closes_socket_and_next_call_succeeds(paths: RuntimePaths) -> None:
    async with (
        asyncio.timeout(5),
        unix_peer(paths) as (isolated, connections),
        asyncio.TaskGroup() as tasks,
    ):
        adapter = GatewayAdapter(isolated)
        pending = tasks.create_task(adapter.llm_search("first"))
        reader, _ = await connections.get()
        assert decode_request_frame(await reader.readline()) == LLMSearchRequest("first")
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert await reader.read() == b""

        subsequent = tasks.create_task(adapter.url_fetch("https://example.test/next"))
        reader, writer = await connections.get()
        expected = URLFetchRequest("https://example.test/next")
        assert decode_request_frame(await reader.readline()) == expected
        writer.write(encode_response(SuccessResponse("second")))
        await writer.drain()
        assert await subsequent == "second"
        assert await reader.read() == b""
        assert connections.empty()


async def test_unexpected_error_reading_search_result(
    paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    paths.results_dir.mkdir(parents=True)
    result = paths.results_dir / "keyword-01234567.jsonl"
    result.write_text("row", encoding="utf-8")
    client = RecordingClient(SuccessResponse(str(result)))

    def fail_read(fd: int, size: int) -> bytes:
        raise ArithmeticError(_PRIVATE_DETAIL)

    with monkeypatch.context() as guard:
        guard.setattr("os.read", fail_read)
        with pytest.raises(AdapterFailure) as caught:
            await GatewayAdapter(paths, client).keyword_search("private-query")

    assert client.calls == [(paths.socket_file, KeywordSearchRequest("private-query"))]
    assert caught.value.code is None
    assert caplog.text
    private_details = ("/fixture/private", "fixture-secret", "private-query", str(result))
    assert "Traceback" not in caplog.text
    for private in private_details:
        assert private not in str(caught.value) + caplog.text
