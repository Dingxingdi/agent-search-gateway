"""Tests for the gateway's outbound input byte budget."""

import asyncio
import json
from collections.abc import Callable, Sequence

import pytest

from agent_search_gateway import input_limits
from agent_search_gateway.llm import prompts
from agent_search_gateway.providers.contracts import ChatMessage

MARKER = "[... truncated ...]"


def test_truncate_utf8_preserves_head_tail_and_counts_marker() -> None:
    body = "HEAD" + "abcdefgh" * 20 + "MIDDLE" + "xyz" * 20 + "TAIL"
    cap = len(MARKER.encode()) + 20
    result = input_limits.truncate_utf8_head_tail(body, cap)
    assert result.text == body[:16] + MARKER + body[-4:]
    assert result.text.count(MARKER) == 1
    assert "MIDDLE" not in result.text
    assert result.original_bytes == len(body.encode())
    assert result.sent_bytes == len(result.text.encode()) == cap
    assert result.truncated


def _encode(messages: Sequence[ChatMessage]) -> bytes:
    return json.dumps([dict(message) for message in messages], ensure_ascii=False).encode()


@pytest.mark.parametrize("multibody", [False, True])
def test_near_limit_json_keeps_the_available_context(multibody: bool) -> None:
    import httpx

    def encode(messages: Sequence[ChatMessage]) -> bytes:
        envelope = {"model": "m", "messages": [dict(message) for message in messages]}
        return httpx.Request("POST", "https://example.test/chat", json=envelope).content

    cap = 131072
    messages: list[ChatMessage] = [input_limits.TruncatableMessage("prefix", "\n", "suffix")]
    if multibody:
        messages.append(input_limits.TruncatableMessage("第二", "头😀" + "\\\n猫" * 4000 + "尾"))
    padding = cap + 1 - len(encode(messages))
    messages[0] = input_limits.TruncatableMessage("prefix", "\n" + "x" * padding, "suffix")
    result = input_limits.fit_messages_to_budget(messages, max_bytes=cap, encode=encode)

    assert result.original_bytes == cap + 1
    assert cap - 32 <= len(result.content) <= cap
    actual = json.loads(result.content)["messages"]
    assert actual[0]["content"].startswith("prefix\n") and actual[0]["content"].endswith("suffix")
    assert MARKER in actual[0]["content"]
    if multibody:
        assert actual[1]["content"].startswith("第二头😀") and actual[1]["content"].endswith("尾")


@pytest.mark.parametrize("structured", [True, False])
def test_key_and_identity_tokens_are_masked_in_the_record_and_output(structured: bool) -> None:
    from logging.handlers import BufferingHandler

    from agent_search_gateway import observability
    from tests.support.logging import structured_test_logger

    fields = dict.fromkeys(("KEY", "Id-Token", "Session_Token"), "FIXTURE_ONLY")
    if structured:
        body = json.dumps({"nested": [fields], "keyboard": "VISIBLE"})
    else:
        body = " ".join(f"{name} = {value}" for name, value in fields.items()) + " keyboard=VISIBLE"
    logger, stream = structured_test_logger("tests.preview.identity")
    capture = BufferingHandler(10)
    logger.addHandler(capture)
    observability.log_upstream_body_preview(
        logger,
        provider="p",
        stage="s",
        endpoint=None,
        attempt=1,
        transport="http",
        response_body=body.encode(),
    )
    (record,) = capture.buffer
    for observed in (repr(record.__dict__), stream.getvalue()):
        assert "FIXTURE_ONLY" not in observed
        assert "VISIBLE" in observed


@pytest.mark.parametrize("authority", ["//", r"\/\/", "%2F%2F"])
def test_network_path_urls_in_html_remain_safe_in_the_record_and_output(authority: str) -> None:
    from logging.handlers import BufferingHandler

    from agent_search_gateway import observability
    from tests.support.logging import structured_test_logger

    logger, stream = structured_test_logger("tests.preview.network_path")
    capture = BufferingHandler(10)
    logger.addHandler(capture)
    private = "FIXTURE_ONLY"
    url = f"{authority}sample:{private}@example.test/public?q={private}#{private}"
    html = f'VISIBLE_HEAD <a href="{url}">link</a> VISIBLE_TAIL'
    observability.log_upstream_body_preview(
        logger,
        provider="p",
        stage="s",
        endpoint=None,
        attempt=1,
        transport="http",
        response_body=json.dumps({"html": html}).encode(),
    )
    (record,) = capture.buffer
    for observed in (repr(record.__dict__), stream.getvalue()):
        assert "FIXTURE_ONLY" not in observed
        assert all(word in observed for word in ("VISIBLE_HEAD", ">link</a>", "VISIBLE_TAIL"))
        if authority == "//":
            assert "example.test/public" in observed


def test_network_path_projection_masks_credentials_across_the_input_cut() -> None:
    from agent_search_gateway import observability
    from tests.support.logging import structured_test_logger

    logger, stream = structured_test_logger("tests.preview.network_path_cut")
    url = "//example.test/page?FIXTURE_ONLY" + "OMITTED" * 100 + "#FIXTURE_ONLY"
    message = input_limits.TruncatableMessage("", f"VISIBLE {url} END")
    ready = input_limits.fit_messages_to_budget(
        [message],
        max_bytes=100,
        encode=lambda messages: messages[0]["content"].encode(),
        preview_projector=observability.make_input_preview_projector(logger),
    )
    assert ready.input_truncated and b"FIXTURE_ONLY" in ready.content
    observability.log_upstream_body_preview(
        logger,
        provider="p",
        stage="s",
        endpoint=None,
        attempt=1,
        transport="http",
        prepared_body=ready,
    )
    line = stream.getvalue()
    assert "FIXTURE_ONLY" not in line and "OMITTED" not in line
    assert all(word in line for word in ("VISIBLE", "END", input_limits.TRUNCATION_MARKER))


def test_utf8_boundaries_and_preexisting_marker() -> None:
    body = "汉😀𠮷e\u0301\n\\" * 4 + MARKER
    size = len(body.encode())
    for cap in (size, size + 1):
        unchanged = input_limits.truncate_utf8_head_tail(body, cap)
        assert unchanged.text == body
        assert unchanged.sent_bytes == unchanged.original_bytes == size
        assert not unchanged.truncated
    for cap in (size - 1, len(MARKER) + 6, len(MARKER)):
        retained = cap - len(MARKER)
        raw = body.encode()
        head = raw[: retained * 4 // 5].decode(errors="ignore")
        tail = raw[size - (retained - retained * 4 // 5) :].decode(errors="ignore")
        result = input_limits.truncate_utf8_head_tail(body, cap)
        assert result.text == head + MARKER + tail
        assert result.sent_bytes == len(result.text.encode()) <= cap
        assert result.truncated


@pytest.mark.parametrize(
    "body,cap",
    [("SENSITIVE" * 10, 3), ("SENSITIVE\ud800", 100)],
    ids=["marker-too-large", "invalid-utf8"],
)
def test_text_errors_are_safe(body: str, cap: int) -> None:
    with pytest.raises(input_limits.InputBudgetUnfit) as exc:
        input_limits.truncate_utf8_head_tail(body, cap)
    assert "SENSITIVE" not in repr(exc.value)
    assert exc.value.__context__ is None


def test_mapping_only_exposes_user_role_and_rendered_content() -> None:
    message = input_limits.TruncatableMessage("question: ", "private body", " end")
    assert message == {"role": "user", "content": "question: private body end"}
    assert list(message) == ["role", "content"]
    with pytest.raises(KeyError):
        message["body"]


def test_exact_fit_reuses_original_encoded_bytes_once() -> None:
    messages = [{"role": "tool", "content": MARKER, "name": "adapter"}]
    original = _encode(messages)
    seen = []

    def encode(candidate: Sequence[ChatMessage]) -> bytes:
        seen.append(candidate)
        return original

    result = input_limits.fit_messages_to_budget(messages, max_bytes=len(original), encode=encode)
    assert seen == [messages]
    assert result.content is original
    assert result.original_bytes == len(original)
    assert not result.input_truncated
    assert result.preview_body is None


@pytest.mark.parametrize(
    "short,keep_short",
    [("", True), ('"\\', True), ("e" * len(MARKER), True), ("\0" * 6, False)],
    ids=["empty", "cheaper-original", "equal-cost-original", "cheaper-marker"],
)
def test_minimum_uses_real_encoded_cost(short: str, keep_short: bool) -> None:
    short_body = input_limits.TruncatableMessage("a", short, "z")
    long_body = input_limits.TruncatableMessage("b", "long" * 99)
    messages = (short_body, long_body)
    short_minimum = short if keep_short else MARKER
    minimum = _encode(
        [
            {"role": "user", "content": "a" + short_minimum + "z"},
            {"role": "user", "content": "b" + MARKER},
        ]
    )
    result = input_limits.fit_messages_to_budget(messages, max_bytes=len(minimum), encode=_encode)
    assert result.content == minimum
    assert result.original_bytes == len(_encode(messages))
    assert result.input_truncated
    with pytest.raises(input_limits.InputBudgetUnfit):
        input_limits.fit_messages_to_budget(messages, max_bytes=len(minimum) - 1, encode=_encode)


@pytest.mark.parametrize("transport", ["http", "agent_cli"])
@pytest.mark.parametrize("structured", [False, True])
def test_projector_masks_original_ranges_across_real_encoding_cuts(
    transport: str,
    structured: bool,
) -> None:
    import httpx

    from agent_search_gateway import observability
    from agent_search_gateway.providers.agent.antigravity_cli import _prepare_prompt
    from tests.support.logging import structured_test_logger

    def encode(messages: Sequence[ChatMessage]) -> bytes:
        if transport == "http":
            envelope = {"model": "m", "messages": [dict(m) for m in messages]}
            return httpx.Request("POST", "https://example.test/chat", json=envelope).content
        return "\n\n".join(f"[{m['role'].upper()}]\n{m['content']}" for m in messages).encode()

    sample = "PRIVATE_HEAD_" + "a" * 12 + "_PRIVATE_TAIL"
    start = "VISIBLE " + MARKER
    if structured:
        start += r' {"\u0061pi_key":"'
        body = start + "PRIVATE_HEAD_" * 80 + "_PRIVATE_TAIL" * 15 + '","safe":"TAIL"}'
    else:
        body = start + "x" * (180 - len(start)) + sample + "x" * 600 + sample + "y" * 30 + "TAIL"

    def messages(text: str) -> tuple[ChatMessage, ...]:
        return (
            {"role": "system", "content": "KEEP SYSTEM"},
            input_limits.TruncatableMessage("prefix ", text, " suffix"),
        )

    # Account for HTTP JSON escaping of embedded JSON punctuation before selecting both cuts.
    cap = len(encode(messages(MARKER))) + (320 if structured else 250)
    wire = input_limits.fit_messages_to_budget(messages(body), max_bytes=cap, encode=encode)
    assert b"PRIVATE_" in wire.content and wire.input_truncated
    if transport == "agent_cli":
        assert _prepare_prompt(messages(body), cap).content == wire.content
    logger, stream = structured_test_logger(
        "tests.preview.fit", secrets=[] if structured else [observability.SecretValue(sample)]
    )
    result = input_limits.fit_messages_to_budget(
        messages(body),
        max_bytes=cap,
        encode=encode,
        preview_projector=observability.make_input_preview_projector(logger),
    )
    assert result.preview_body and result.preview_body.content
    assert b"PRIVATE_" not in result.preview_body.content
    assert result.content == wire.content and result.original_bytes == wire.original_bytes
    observability.log_upstream_body_preview(
        logger,
        provider="p",
        stage="s",
        endpoint=None,
        attempt=1,
        transport=transport,
        prepared_body=result,
    )
    line = stream.getvalue()
    assert "PRIVATE_" not in line
    assert all(word in line for word in ("prefix", "VISIBLE", "TAIL", "suffix", "KEEP SYSTEM"))
    assert result.preview_body.content.count(MARKER.encode()) == 2
    assert MARKER in line
    assert "request_input_truncated=true" in line and "request_preview_truncated=false" in line


def test_projection_masks_a_credential_introduced_in_the_protected_prefix() -> None:
    from agent_search_gateway import observability
    from tests.support.logging import structured_test_logger

    logger, stream = structured_test_logger("tests.preview.prefix")
    raw = "PRIVATE_FRAGMENT" * 70
    message = input_limits.TruncatableMessage("VISIBLE Bearer ", raw, "\nTAIL")
    result = input_limits.fit_messages_to_budget(
        [message],
        max_bytes=230,
        encode=_encode,
        preview_projector=observability.make_input_preview_projector(logger),
    )
    assert result.input_truncated and result.preview_body and result.preview_body.content
    assert b"PRIVATE" not in result.preview_body.content
    observability.log_upstream_body_preview(
        logger,
        provider="p",
        stage="s",
        attempt=1,
        transport="http",
        endpoint=None,
        prepared_body=result,
    )
    assert "PRIVATE" not in stream.getvalue() and "VISIBLE" in stream.getvalue()


def test_only_marked_bodies_share_capacity_with_bounded_encodes() -> None:
    first = "头😀" + "a" * (2 * 1024 * 1024) + "尾𠮷"
    second = "start" + "\\\n" * (512 * 1024) + "end"
    plain = {"role": "user", "content": "Page content:\nprotected", "name": "external"}
    messages = (
        plain,
        input_limits.TruncatableMessage("p", first, "s"),
        input_limits.TruncatableMessage("", second),
    )
    calls = 0

    def encode(candidate: Sequence[ChatMessage]) -> bytes:
        nonlocal calls
        calls += 1
        return _encode(candidate)

    cap = 128 * 1024
    result = input_limits.fit_messages_to_budget(messages, max_bytes=cap, encode=encode)
    assert len(result.content) <= cap
    assert result.input_truncated
    assert result.original_bytes == len(_encode(messages))
    assert calls <= len(messages) + cap.bit_length() + 2
    actual = json.loads(result.content)
    assert actual[0] == plain
    assert actual[1]["content"].startswith("p头😀")
    assert actual[1]["content"].endswith("尾𠮷s")
    assert actual[2]["content"].startswith("start")
    assert actual[2]["content"].endswith("end")
    assert all(row["content"].count(MARKER) == 1 for row in actual[1:])


def test_protected_only_overflow_and_encoder_errors_are_safe() -> None:
    messages = [{"role": "user", "content": "SENSITIVE " * 10}]
    with pytest.raises(input_limits.InputBudgetUnfit) as exc:
        input_limits.fit_messages_to_budget(messages, max_bytes=60, encode=_encode)
    assert "SENSITIVE" not in repr(exc.value)

    def bad_encode(_: Sequence[ChatMessage]) -> bytes:
        raise ValueError("SENSITIVE")

    with pytest.raises(input_limits.InputBudgetUnfit) as exc:
        input_limits.fit_messages_to_budget(messages, max_bytes=1000, encode=bad_encode)
    assert "SENSITIVE" not in repr(exc.value)
    assert exc.value.__context__ is None


def test_preview_uses_selected_original_unicode_ranges() -> None:
    body = "甲😀" + MARKER + "a" * 200 + "SECRET_MIDDLE" + "b" * 200 + "尾𠮷"
    messages = (
        {"role": "system", "content": "protected"},
        input_limits.TruncatableMessage("prefix", body, "suffix"),
    )
    cap = 220
    transport = input_limits.fit_messages_to_budget(messages, max_bytes=cap, encode=_encode)
    observed: list[input_limits.BodySelection] = []

    def project(
        original: Sequence[ChatMessage],
        candidate: Sequence[ChatMessage],
        selections: tuple[input_limits.BodySelection, ...],
    ) -> Sequence[ChatMessage]:
        assert original == messages
        assert _encode(candidate) == transport.content
        (selection,) = selections
        observed.append(selection)
        assert selection.message_index == 1
        assert selection.head_end > len("甲😀" + MARKER)
        assert selection.tail_start < len(body) - 1
        assert candidate[1]["content"] == (
            "prefix" + body[: selection.head_end] + MARKER + body[selection.tail_start :] + "suffix"
        )
        return [{"role": "user", "content": "preview only"}]

    result = input_limits.fit_messages_to_budget(
        messages, max_bytes=cap, encode=_encode, preview_projector=project
    )
    assert len(observed) == 1
    assert result.content == transport.content
    assert result.original_bytes == transport.original_bytes
    assert result.input_truncated
    assert result.preview_body == input_limits.PreparedPreview(
        _encode([{"role": "user", "content": "preview only"}])
    )
    for sensitive in ("prefix", "preview only", "SECRET_MIDDLE"):
        assert sensitive not in repr(result)
    assert b"SECRET_MIDDLE" not in result.content


@pytest.mark.parametrize(
    "outcome", ["none", "project_error", "encode_error", "cancelled", "encode_cancelled"]
)
def test_preview_failure_is_isolated(outcome: str) -> None:
    messages = [{"role": "user", "content": "transport"}]

    def project(
        original: Sequence[ChatMessage],
        candidate: Sequence[ChatMessage],
        selections: tuple[input_limits.BodySelection, ...],
    ) -> Sequence[ChatMessage] | None:
        assert original == candidate == messages
        assert not selections
        if outcome == "cancelled":
            raise asyncio.CancelledError
        if outcome == "project_error":
            raise ValueError("private projector detail")
        if outcome == "encode_cancelled":
            return [{"role": "user", "content": "cancel preview encode"}]
        if outcome == "encode_error":
            return [{"role": "user", "content": "\ud800"}]
        return None

    def encode(candidate: Sequence[ChatMessage]) -> bytes:
        if candidate[0]["content"] == "cancel preview encode":
            raise asyncio.CancelledError
        return _encode(candidate)

    if outcome in {"cancelled", "encode_cancelled"}:
        with pytest.raises(asyncio.CancelledError):
            input_limits.fit_messages_to_budget(
                messages, max_bytes=200, encode=encode, preview_projector=project
            )
        return
    result = input_limits.fit_messages_to_budget(
        messages, max_bytes=200, encode=encode, preview_projector=project
    )
    assert result.preview_body == input_limits.PreparedPreview(None)
    assert result.content == _encode(messages)
    assert result.original_bytes == len(result.content)
    assert not result.input_truncated


_FOCUS = "  focus\n\nPage content:\nkeep the entire instruction  "


def _focus_messages(body: str) -> tuple[ChatMessage, ...]:
    return prompts.focus_summary_messages(body, _FOCUS)


@pytest.mark.parametrize(
    "build,prefix,stripped,system",
    [
        (
            prompts.judge_messages,
            "Candidate content:\n",
            False,
            "Decide whether the supplied text is usable main-body content for the target "
            "web page. Return JSON with boolean field ok and optional short string field reason.",
        ),
        (
            prompts.safety_messages,
            "Final content:\n",
            False,
            "Decide whether the supplied final page content is safe to return. "
            "Return JSON with boolean field ok and optional short string field reason.",
        ),
        (
            prompts.content_clean_messages,
            "",
            False,
            "Return only the cleaned, readable main content from the supplied raw page text.",
        ),
        (
            _focus_messages,
            "Focus: " + _FOCUS.strip() + "\n\nPage content:\n",
            False,
            "Summarize only the supplied page content, emphasizing the requested focus.",
        ),
        (
            prompts.llm_search_messages,
            "",
            True,
            "Find relevant web results. Return only repeated blocks in this exact "
            "restricted format:\n## Result\nURL: https://example.com/page\n"
            "Abstract: concise non-empty summary\n"
            "Do not use Markdown links or other result formats.",
        ),
        (
            prompts.llm_paper_search_messages,
            "",
            True,
            "Find relevant academic papers. Return only repeated blocks in exactly this "
            "16-line restricted format, with every field present exactly once and in this order. "
            "Optional values may be empty. Authors and Topics are semicolon-separated. "
            "Dates are empty or YYYY-MM-DD. Citations are empty or a non-negative integer. "
            "Open Access is true, false, or unknown. Do not use Markdown links, Result blocks, "
            "or any text outside these blocks:\n"
            "## Paper\nTitle: paper title\nAuthors: Alice Author; Bob Author\n"
            "Abstract: concise abstract\nDOI: 10.1000/example\narXiv: 2401.12345\n"
            "Published: 2024-01-02\nUpdated: 2024-02-03\nURL: https://example.com/paper\n"
            "PDF: https://example.com/paper.pdf\nVenue: venue name\nTopics: topic one; topic two\n"
            "Citations: 0\nOpen Access: unknown\nOA Status: \nLicense: ",
        ),
    ],
    ids=["judge", "safety", "clean", "focus", "search", "paper"],
)
def test_builders_protect_explicit_segments_and_preserve_wire_text(
    build: Callable[[str], tuple[ChatMessage, ...]], prefix: str, stripped: bool, system: str
) -> None:
    body = "  A\n" + "Focus:\n\nPage content:\nCandidate content:\nFinal content:\n" * 9 + "Z  "
    messages = build(body)
    original_body = body.strip() if stripped else body
    system_message = {"role": "system", "content": system}
    assert [dict(message) for message in messages] == [
        system_message,
        {"role": "user", "content": prefix + original_body},
    ]
    minimum = _encode([system_message, {"role": "user", "content": prefix + MARKER}])
    result = input_limits.fit_messages_to_budget(messages, max_bytes=len(minimum), encode=_encode)
    assert result.content == minimum
    assert result.input_truncated
    with pytest.raises(input_limits.InputBudgetUnfit):
        input_limits.fit_messages_to_budget(messages, max_bytes=len(minimum) - 1, encode=_encode)
