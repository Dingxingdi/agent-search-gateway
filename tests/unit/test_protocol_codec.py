import json

import pytest

from agent_search_gateway.errors import ErrorCode
from agent_search_gateway.models import (
    ErrorResponse,
    KeywordSearchRequest,
    LLMSearchRequest,
    PaperSearchRequest,
    ShutdownRequest,
    SuccessResponse,
    URLFetchRequest,
)
from agent_search_gateway.protocol import (
    _MAX_REQUEST_FRAME_BYTES,
    NDJSONDecoder,
    decode_request_frame,
    encode_request,
    encode_response,
)


def test_ndjson_codec_buffers_partial_bytes_and_splits_multiple_requests() -> None:
    decoder = NDJSONDecoder()
    assert decoder.feed(b'{"type":"keyword_search","query":"hel') == []
    decoded = decoder.feed(
        b'lo"}\n{"type":"llm_search","prompt":"find"}\n'
        b'{"type":"paper_search","query":"papers"}\n'
        b'{"type":"url_fetch","url":"https://example.com","focus":null}\n'
        b'{"type":"shutdown"}\n'
    )
    assert decoded == [
        KeywordSearchRequest("hello"),
        LLMSearchRequest("find"),
        PaperSearchRequest("papers"),
        URLFetchRequest("https://example.com", None),
        ShutdownRequest(),
    ]

    errors = decoder.feed(
        b"not-json\n"
        b'{"type":"unknown"}\n'
        b'{"type":"keyword_search"}\n'
        b'{"type":"url_fetch","url":1,"focus":null}\n'
        b'{"type":"shutdown","extra":true}\n'
    )
    assert len(errors) == 5
    assert all(isinstance(item, ErrorResponse) for item in errors)
    assert all(
        item.error is ErrorCode.BAD_REQUEST for item in errors if isinstance(item, ErrorResponse)
    )

    success = encode_response(SuccessResponse("done"))
    error = encode_response(ErrorResponse(ErrorCode.BAD_REQUEST, "bad"))
    assert success == b'{"ok":true,"text":"done"}\n'
    assert error == b'{"ok":false,"error":"bad_request","message":"bad"}\n'
    assert encode_request(LLMSearchRequest("find")) == (b'{"type":"llm_search","prompt":"find"}\n')
    assert encode_request(LLMSearchRequest("find", "paper")) == (
        b'{"type":"llm_search","prompt":"find","scope":"paper"}\n'
    )
    assert decode_request_frame(
        b'{"type":"llm_search","prompt":"find","scope":"all"}'
    ) == LLMSearchRequest("find", "all")
    assert decode_request_frame(
        b'{"type":"llm_search","prompt":"find","scope":"invalid"}'
    ) == ErrorResponse(ErrorCode.BAD_REQUEST, "Request fields do not match schema")
    assert encode_request(PaperSearchRequest("papers")) == (
        b'{"type":"paper_search","query":"papers"}\n'
    )
    assert encode_request(ShutdownRequest()) == b'{"type":"shutdown"}\n'
    assert decode_request_frame(b'{"type":"paper_search","query":"papers"}') == (
        PaperSearchRequest("papers")
    )
    assert decode_request_frame(b'{"type":"paper_search"}') == ErrorResponse(
        ErrorCode.BAD_REQUEST,
        "Request fields do not match schema",
    )
    assert decode_request_frame(
        b'{"type":"paper_search","query":"papers","extra":true}'
    ) == ErrorResponse(ErrorCode.BAD_REQUEST, "Request fields do not match schema")
    assert success.count(b"\n") == 1


def test_ndjson_decoder_rejects_oversized_frame_and_resynchronizes() -> None:
    decoder = NDJSONDecoder()
    oversized = decoder.feed(b"x" * (_MAX_REQUEST_FRAME_BYTES + 1))
    assert oversized == [ErrorResponse(ErrorCode.BAD_REQUEST, "Request frame is too large")]

    decoded = decoder.feed(b'discard-the-rest\n{"type":"shutdown"}\n')
    assert decoded == [ShutdownRequest()]


def test_keyword_provider_round_trips() -> None:
    request = KeywordSearchRequest("hello", provider="tavily")
    frame = b'{"type":"keyword_search","query":"hello","provider":"tavily"}\n'
    assert encode_request(request) == frame
    assert decode_request_frame(frame) == request


@pytest.mark.parametrize("request_type", [KeywordSearchRequest, PaperSearchRequest])
@pytest.mark.parametrize("provider", [None, "alias", "", " \t "])
def test_discovery_provider_codec(
    request_type: type[KeywordSearchRequest] | type[PaperSearchRequest], provider: str | None
) -> None:
    request = request_type("query", provider=provider)
    frame = encode_request(request)
    payload = json.loads(frame)
    assert ("provider" in payload) is (provider is not None)
    if provider is not None:
        assert payload["provider"] == provider
    assert decode_request_frame(frame) == request


@pytest.mark.parametrize("scope", ["web", "paper", "all"])
@pytest.mark.parametrize("provider", [None, "openai_main", ""])
@pytest.mark.parametrize("model", [None, "gpt-5", ""])
def test_llm_selector_codec(scope: str, provider: str | None, model: str | None) -> None:
    # Decode the scope first so its Literal type is established by the real codec.
    legacy = decode_request_frame(
        json.dumps({"type": "llm_search", "prompt": "find", "scope": scope}).encode()
    )
    assert isinstance(legacy, LLMSearchRequest)
    request = LLMSearchRequest("find", legacy.scope, provider=provider, model=model)
    frame = encode_request(request)
    payload = json.loads(frame)
    assert ("scope" in payload) is (scope != "web")
    assert ("provider" in payload) is (provider is not None)
    assert ("model" in payload) is (model is not None)
    assert decode_request_frame(frame) == request


@pytest.mark.parametrize(
    ("kind", "text_key", "selector"),
    [
        ("keyword_search", "query", "provider"),
        ("paper_search", "query", "provider"),
        ("llm_search", "prompt", "provider"),
        ("llm_search", "prompt", "model"),
    ],
)
@pytest.mark.parametrize("value", [None, 1, True, [], {}])
def test_selector_codec_rejects_non_strings(
    kind: str, text_key: str, selector: str, value: object
) -> None:
    frame = json.dumps({"type": kind, text_key: "text", selector: value}).encode()
    assert decode_request_frame(frame) == ErrorResponse(
        ErrorCode.BAD_REQUEST, "Request fields do not match schema"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "keyword_search", "query": "x", "model": "m"},
        {"type": "paper_search", "query": "x", "model": "m"},
        {"type": "llm_search", "prompt": "x", "extra": True},
        {"type": "keyword_search", "provider": "p"},
        {"type": "paper_search", "provider": "p"},
        {"type": "llm_search", "provider": "p"},
        {"type": "url_fetch", "url": "https://example.com", "focus": None, "provider": "p"},
        {"type": "url_fetch", "url": "https://example.com", "focus": None, "model": "m"},
        {"type": "shutdown", "provider": "p"},
        {"type": "shutdown", "model": "m"},
    ],
)
def test_selectors_do_not_relax_request_schema(payload: dict[str, object]) -> None:
    assert decode_request_frame(json.dumps(payload).encode()) == ErrorResponse(
        ErrorCode.BAD_REQUEST, "Request fields do not match schema"
    )


def test_selector_frames_buffer_and_split_without_losing_metadata() -> None:
    requests: list[KeywordSearchRequest | LLMSearchRequest | PaperSearchRequest] = [
        KeywordSearchRequest("hello", provider="exa"),
        LLMSearchRequest("find", "all", provider="openai_main", model="gpt-5"),
        PaperSearchRequest("papers", provider="arxiv"),
    ]
    wire = b"".join(encode_request(request) for request in requests)
    decoder = NDJSONDecoder()
    assert decoder.feed(wire[:17]) == []
    assert decoder.feed(wire[17:]) == requests
