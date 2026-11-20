"""Exercise body previews through the structured logger and its real formatter."""

import asyncio
import io
import json
import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_search_gateway import observability
from tests.support.logging import structured_test_logger


def test_body_preview_redacts_registered_secret_before_truncating() -> None:
    sample = "BOUNDARY_PRIVATE_PREFIX_BOUNDARY_PRIVATE_SUFFIX"
    logger, stream = structured_test_logger(
        "tests.preview.before_cut", secrets=[observability.SecretValue(sample)]
    )
    raw = ("VISIBLE" + "x" * 6515 + sample + "x" * 1600 + sample + "y" * 2400).encode()
    observability.log_upstream_body_preview(
        logger,
        provider="sample",
        stage="fetch",
        endpoint=None,
        attempt=1,
        transport="http",
        response_body=raw,
    )
    line = stream.getvalue()
    assert "VISIBLE" in line
    assert "BOUNDARY_" not in line
    assert "[... truncated ...]" in line
    assert "response_preview_truncated=true" in line
    assert len(line.splitlines()) == 1


def test_nested_json_redacts_entire_sensitive_values_and_preserves_safe_siblings() -> None:
    logger, stream = structured_test_logger("tests.preview.structure")
    inner = r'{"\u0061pi_key":"PRIVATE_EMBEDDED","ok":"VISIBLE"}'
    keys = [
        "Api-Key",
        "authorization",
        "PROXY_authorization",
        "Set-Cookie",
        "access-Token",
        "refresh_token",
        "PASSWORD",
        "access_secret",
        "secret",
        "cookie",
        "token",
    ]
    row = {key: {"nested": ["PRIVATE_" + key]} for key in keys}
    body = json.dumps({"rows": [row, {"content": "prefix\n" + inner}]})
    observability.log_upstream_body_preview(
        logger,
        provider="sample",
        stage="fetch",
        endpoint=None,
        attempt=1,
        transport="http",
        response_body=body.encode(),
    )
    line = stream.getvalue()
    assert "PRIVATE_" not in line
    assert "VISIBLE" in line
    assert "<redacted>" in line


def _emit(logger: logging.Logger, body: bytes | Callable[[], bytes] | None) -> None:
    observability.log_upstream_body_preview(
        logger,
        provider="sample",
        stage="fetch",
        endpoint=None,
        attempt=1,
        transport="http",
        response_body=body,
    )


@pytest.mark.parametrize("inside_json", [False, True])
def test_embedded_html_urls_and_plain_credentials(inside_json: bool) -> None:
    logger, stream = structured_test_logger("tests.preview.text")
    url = "https://" + "PRIVATE_USER" + "@example.test/public?q=PRIVATE_QUERY#PRIVATE_FRAGMENT"
    content = "\n".join(
        [
            f'VISIBLE <a href="{url}">link</a>',
            "Authorization: " + "Bearer PRIVATE_AUTH",
            "Cookie: " + "sid=PRIVATE_SESSION; x=PRIVATE_X",
            "Basic " + "PRIVATE_BASIC",
            "Key " + "PRIVATE_KEY",
            "password=" + '"PRIVATE WITH SPACES"',
        ]
    )
    _emit(logger, (json.dumps({"text": content}) if inside_json else content).encode())
    line = stream.getvalue()
    assert "PRIVATE" not in line
    assert "VISIBLE" in line and "example.test/public" in line
    assert len(line.splitlines()) == 1


@pytest.mark.parametrize("size", [8191, 8192, 8193])
@pytest.mark.parametrize("chunk", ["x", '猫😀\t"\\\n'])
def test_physical_preview_field_budget(size: int, chunk: str) -> None:
    logger, stream = structured_test_logger("tests.preview.budget")
    fill, remainder = divmod(size, len(chunk.encode()))
    body = chunk * fill + "z" * remainder
    _emit(logger, body.encode())
    line = stream.getvalue()
    field = line.split("response_body_preview=", 1)[1].split(" response_preview_truncated=", 1)[0]
    value = json.loads(field) if field.startswith('"') else field
    assert len(field.encode()) <= 8192
    assert len(value.encode()) <= 8192
    needs_cut = size > 8192 or chunk != "x"
    assert (value != body) == needs_cut
    assert f"response_preview_truncated={str(needs_cut).lower()}" in line
    if needs_cut:
        head, tail = value.split("[... truncated ...]")
        assert head and tail and body.startswith(head) and body.endswith(tail)


@pytest.mark.parametrize(
    "raw", [None, b"", b"\xff", b"abc\x00PRIVATE", b'{"token":"PRIVATE unfinished']
)
def test_unavailable_body_preserves_stats_in_record_without_raw_data(raw: bytes | None) -> None:
    from logging.handlers import BufferingHandler

    logger, stream = structured_test_logger("tests.preview.unavailable")
    capture = BufferingHandler(10)
    logger.addHandler(capture)
    observability.log_upstream_body_preview(
        logger,
        provider="sample",
        stage="fetch",
        endpoint="https://user@example.test/path?q=PRIVATE#x",
        attempt=2,
        protocol_attempt=3,
        transport="http",
        request_body=b"VISIBLE",
        response_body=raw,
    )
    (record,) = capture.buffer
    assert record.__dict__["gateway_fields"] == {
        "provider": "sample",
        "stage": "fetch",
        "endpoint": "https://example.test/path",
        "attempt": 2,
        "protocol_attempt": 3,
        "transport": "http",
        "request_body_preview": "VISIBLE",
        "request_body_original_bytes": 7,
        "request_body_sent_bytes": 7,
        "request_input_truncated": False,
        "request_preview_truncated": False,
        "response_body_preview": None,
        "response_body_bytes": len(raw or b""),
        "response_preview_truncated": False,
    }
    assert "PRIVATE" not in repr(record.__dict__)
    assert "PRIVATE" not in stream.getvalue()


@pytest.mark.parametrize("mode", ["info", "no_formatter", "level_blocks", "stop_propagation"])
def test_inactive_preview_never_materializes_bodies_or_encodes_views(mode: str) -> None:
    from logging.handlers import BufferingHandler

    from agent_search_gateway import input_limits

    parent, _ = structured_test_logger("tests.preview.inactive")
    child = logging.getLogger("tests.preview.inactive.child")
    capture = BufferingHandler(10)
    child.handlers[:] = [capture]
    child.setLevel(logging.INFO if mode == "info" else logging.DEBUG)
    child.propagate = mode not in {"stop_propagation", "no_formatter"}
    if mode == "level_blocks":
        parent.handlers[0].setLevel(logging.INFO)
    calls = []

    def copy_body() -> bytes:
        calls.append("copy")
        raise AssertionError("unexpected copy")

    def encode(messages: object) -> bytes:
        calls.append("encode")
        return b"wire"

    prepared = input_limits.fit_messages_to_budget(
        [],
        max_bytes=100,
        encode=encode,
        preview_projector=observability.make_input_preview_projector(child),
    )
    observability.log_upstream_body_preview(
        child,
        provider="p",
        stage="s",
        attempt=1,
        endpoint=None,
        transport="agent_cli",
        request_body=copy_body,
        request_body_sent_bytes=4,
        response_body=copy_body,
        response_body_bytes=13,
    )
    assert calls == ["encode"] and prepared.preview_body is None
    assert len(capture.buffer) == (0 if mode == "info" else 1)
    if capture.buffer:
        fields = capture.buffer[0].__dict__["gateway_fields"]
        assert fields["request_body_preview"] is None and fields["response_body_preview"] is None
        assert fields["request_body_sent_bytes"] == 4 and fields["response_body_bytes"] == 13


@pytest.mark.parametrize("truncated,failed", [(True, False), (True, True), (False, True)])
def test_unavailable_prepared_view_never_falls_back(truncated: bool, failed: bool) -> None:
    from agent_search_gateway.input_limits import PreparedInput, PreparedPreview

    logger, stream = structured_test_logger("tests.preview.late_debug")
    prepared = PreparedInput(
        b"PRIVATE_WIRE", 40, truncated, PreparedPreview(None) if failed else None
    )
    observability.log_upstream_body_preview(
        logger,
        provider="p",
        stage="s",
        attempt=1,
        endpoint=None,
        transport="http",
        request_body=b"PRIVATE_RAW",
        request_body_sent_bytes=13,
        prepared_body=prepared,
        response_body=b"VISIBLE",
    )
    line = stream.getvalue()
    assert "PRIVATE" not in line and "request_body_preview=null" in line and "VISIBLE" in line
    assert "request_body_original_bytes=40" in line and "request_body_sent_bytes=13" in line


@pytest.mark.parametrize(
    "raw,expected,clipped",
    [
        (b"", "null", False),
        (b'{"token":' + b'"PRIVATE words', "null", True),
        (b'{"ok":"VISIBLE"}', "VISIBLE", True),
    ],
)
def test_incomplete_body_cannot_fall_back_to_text(raw: bytes, expected: str, clipped: bool) -> None:
    logger, stream = structured_test_logger("tests.preview.incomplete")
    observability.log_upstream_body_preview(
        logger,
        provider="p",
        stage="s",
        attempt=1,
        endpoint=None,
        transport="agent_cli",
        response_body=lambda: raw,
        response_body_bytes=len(raw),
        response_incomplete=True,
    )
    line = stream.getvalue()
    assert expected in line and "PRIVATE" not in line
    assert f"response_preview_truncated={str(clipped).lower()}" in line
    assert f"response_body_bytes={len(raw)}" in line


@pytest.mark.parametrize("cancel", [False, True])
def test_supplier_failure_is_side_local_and_cancellation_propagates(cancel: bool) -> None:
    logger, stream = structured_test_logger("tests.preview.failure")

    def fetch() -> bytes:
        if cancel:
            raise asyncio.CancelledError
        raise ValueError("PRIVATE_ERROR")

    def emit() -> None:
        observability.log_upstream_body_preview(
            logger,
            provider="p",
            stage="s",
            attempt=1,
            endpoint=None,
            transport="http",
            request_body=fetch,
            request_body_sent_bytes=7,
            response_body=b"VISIBLE",
        )

    if cancel:
        with pytest.raises(asyncio.CancelledError):
            emit()
        return
    emit()
    line = stream.getvalue()
    assert "PRIVATE" not in line and "request_body_preview=null" in line and "VISIBLE" in line
    assert "request_body_sent_bytes=7" in line


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps(r'{"\u0061pi_key":"PRIVATE_SENTINEL","ok":"VISIBLE"}'),
        json.dumps([0, r'{"\u0061pi_key":"PRIVATE_SENTINEL","ok":"VISIBLE"}']),
        "VISIBLE text " + "password" + "=" + '"first PRIVATE_SENTINEL',
        "VISIBLE " + "https://example.test/path%3Fq%3DPRIVATE_SENTINEL%23frag",
    ],
)
def test_credentials_in_escaped_or_unterminated_text_are_fully_removed(raw: str) -> None:
    logger, stream = structured_test_logger("tests.preview.escapes")
    _emit(logger, raw.encode())
    assert "PRIVATE_SENTINEL" not in stream.getvalue()
    assert "VISIBLE" in stream.getvalue()


@pytest.mark.parametrize(
    "fragment",
    [
        "grant_type=client_credentials&client_secret=PRIVATE_FIXTURE",
        """password='{"label":"PRIVATE_FIXTURE"}'""",
        'password={"label": "PRIVATE_FIXTURE", "list": [1, 2]}',
        'Cookie: state={"theme":"PRIVATE_FIXTURE"}',
        "See https://example.test/it's-here?q=PRIVATE_FIXTURE#PRIVATE_FRAGMENT",
        "<a href='https://example.test/page?q=PRIVATE_FIXTURE'>link</a>",
    ],
    ids=[
        "form-secret",
        "quoted-json-password",
        "unquoted-json-password",
        "json-cookie",
        "url-apostrophe",
        "quoted-attribute",
    ],
)
@pytest.mark.parametrize("inside_json", [False, True])
def test_plain_credentials_enclosing_json_and_url_punctuation(
    fragment: str, inside_json: bool
) -> None:
    from logging.handlers import BufferingHandler

    logger, stream = structured_test_logger("tests.preview.plain_boundaries")
    capture = BufferingHandler(10)
    logger.addHandler(capture)
    content = f"VISIBLE_HEAD\n{fragment}\nVISIBLE_TAIL"
    _emit(logger, (json.dumps({"note": content}) if inside_json else content).encode())
    (record,) = capture.buffer
    for observed in (repr(record.__dict__), stream.getvalue()):
        assert "PRIVATE_" not in observed
        assert "VISIBLE_HEAD" in observed and "VISIBLE_TAIL" in observed
        if fragment.startswith("<a"):
            assert ">link</a>" in observed


def test_registered_credential_overlapping_a_url_replacement_is_fully_removed() -> None:
    from logging.handlers import BufferingHandler

    logger, stream = structured_test_logger(
        "tests.preview.overlap", secrets=[observability.SecretValue("PRIVATE_HEAD?q=PRIVATE_TAIL")]
    )
    capture = BufferingHandler(10)
    logger.addHandler(capture)
    _emit(logger, b"VISIBLE https://example.test/PRIVATE_HEAD?q=PRIVATE_TAIL")
    (record,) = capture.buffer
    for observed in (repr(record.__dict__), stream.getvalue()):
        assert "PRIVATE_" not in observed
        assert "VISIBLE" in observed


def test_input_cut_inside_json_string_preserves_the_safe_edges() -> None:
    from collections.abc import Sequence

    from agent_search_gateway import input_limits
    from agent_search_gateway.providers.contracts import ChatMessage

    logger, stream = structured_test_logger("tests.preview.json_edges")
    original = json.dumps(
        {"note": "HEAD" + "a" * 1000 + " password=PRIVATE_FIXTURE " + "b" * 300 + " VISIBLE_TAIL"}
    )

    def encode(messages: Sequence[ChatMessage]) -> bytes:
        return messages[0]["content"].encode()

    prepared = input_limits.fit_messages_to_budget(
        [input_limits.TruncatableMessage("", original)],
        max_bytes=100,
        encode=encode,
        preview_projector=observability.make_input_preview_projector(logger),
    )
    assert prepared.input_truncated
    assert b"HEAD" in prepared.content and b"VISIBLE_TAIL" in prepared.content
    assert prepared.preview_body is not None and prepared.preview_body.content is not None
    _emit(logger, prepared.preview_body.content)
    assert "HEAD" in stream.getvalue() and "VISIBLE_TAIL" in stream.getvalue()
    assert "PRIVATE_FIXTURE" not in stream.getvalue()


def test_date_tag_does_not_hide_ordinary_text() -> None:
    logger, stream = structured_test_logger("tests.preview.date_tag")
    _emit(logger, b"[2026-11-19] Import complete")
    assert "[2026-11-19] Import complete" in stream.getvalue()


def test_preview_can_only_emit_one_physical_line_for_unicode_separators() -> None:
    logger, stream = structured_test_logger("tests.preview.unicode_lines")
    _emit(logger, "HEAD\u2028MIDDLE\u2029TAIL".encode())
    assert len(stream.getvalue().splitlines()) == 1
    assert all(value in stream.getvalue() for value in ("HEAD", "MIDDLE", "TAIL"))


@pytest.mark.parametrize("failure", ["formatter", "sink", "preparation"])
def test_preview_failures_preserve_results_and_never_dump_private_exception(
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class BrokenDisk(io.StringIO):
        def write(self, text: str) -> int:
            raise OSError(28, "PRIVATE_DISK")

    def broken(*args: object) -> str:
        raise ValueError("PRIVATE_EXCEPTION")

    log_path = tmp_path / "preview.log"
    stderr = io.StringIO()
    session = observability.configure_debug_logging(log_path, stderr=stderr)
    try:
        logger = logging.getLogger("agent_search_gateway")
        if failure == "formatter":
            monkeypatch.setattr(observability.KeyValueFormatter, "format", broken)
        elif failure == "sink":
            file_handler = next(
                h for h in logger.handlers if isinstance(h, observability.SafeRotatingFileHandler)
            )
            monkeypatch.setattr(file_handler, "stream", BrokenDisk())
        else:
            monkeypatch.setattr(observability.KeyValueFormatter, "redact_for_preview", broken)

        def result() -> int:
            _emit(logger, b"PRIVATE_SOURCE")
            return 42

        if failure == "sink":
            session.add_secrets(observability.SecretValue("PRIVATE_SOURCE"))
        assert result() == 42
    finally:
        session.close()
    output = stderr.getvalue() + capsys.readouterr().err + log_path.read_text()
    assert "PRIVATE" not in output
    assert (
        "response_body_preview=null" in output
        if failure == "preparation"
        else "sink failure" in output
    )


@pytest.mark.parametrize(
    "raw",
    [
        '{"api' + '_key": "PRIVATE_FIRST", "api' + '_key": "PRIVATE_SECOND", "ok":"VISIBLE"}',
        json.dumps({"https:" + "//example.test/public?q=PRIVATE_KEY": "VISIBLE"}),
        "VISIBLE " + "password" + "=" + '"contains PRIVATE_TRAILING' + "\\",
        '"' + "VISIBLE\\😀\n" * 1400,
    ],
    ids=["duplicate-key", "url-key", "unfinished-quote", "leading-quote"],
)
def test_source_tokens_and_plain_leading_quotes_remain_safe(raw: str) -> None:
    logger, stream = structured_test_logger("tests.preview.tokens")
    _emit(logger, raw.encode())
    assert "PRIVATE" not in stream.getvalue()
    assert "VISIBLE" in stream.getvalue()


def test_last_formatter_pass_cannot_expand_field_past_budget() -> None:
    logger, stream = structured_test_logger(
        "tests.preview.final_bound", secrets=[observability.SecretValue("red")]
    )
    _emit(logger, ("VISIBLE " + "red " * 2200).encode())
    field = (
        stream.getvalue()
        .split("response_body_preview=", 1)[1]
        .split(" response_preview_truncated=", 1)[0]
    )
    assert len(field.encode()) <= 8192


@pytest.mark.parametrize(
    "cap, input_cut, preview_cut", [(30000, False, True), (10000, True, True), (500, True, False)]
)
def test_input_and_preview_flags_follow_independent_cuts(
    cap: int, input_cut: bool, preview_cut: bool
) -> None:
    from collections.abc import Sequence

    from agent_search_gateway import input_limits
    from agent_search_gateway.providers.contracts import ChatMessage

    logger, stream = structured_test_logger("tests.preview.flags")
    marker = input_limits.TRUNCATION_MARKER
    raw = "HEAD" + marker + "x" * 20000 + "TAIL"

    def encode(messages: Sequence[ChatMessage]) -> bytes:
        return messages[0]["content"].encode()

    prepared = input_limits.fit_messages_to_budget(
        [input_limits.TruncatableMessage("", raw)],
        max_bytes=cap,
        encode=encode,
        preview_projector=observability.make_input_preview_projector(logger),
    )
    observability.log_upstream_body_preview(
        logger,
        provider="p",
        stage="s",
        attempt=1,
        endpoint=None,
        transport="agent_cli",
        prepared_body=prepared,
    )
    line = stream.getvalue()
    assert f"request_input_truncated={str(input_cut).lower()}" in line
    assert f"request_preview_truncated={str(preview_cut).lower()}" in line
    assert "HEAD" in line and "TAIL" in line
    assert f"request_body_sent_bytes={len(prepared.content)}" in line
    assert f"request_body_original_bytes={len(raw.encode())}" in line


def test_record_does_not_keep_registered_values_in_correlation_fields() -> None:
    from logging.handlers import BufferingHandler

    logger, stream = structured_test_logger(
        "tests.preview.correlation", secrets=[observability.SecretValue("PRIVATE_NAME")]
    )
    capture = BufferingHandler(5)
    logger.addHandler(capture)
    observability.log_upstream_body_preview(
        logger,
        provider="provider_PRIVATE_NAME",
        stage="PRIVATE_NAME_stage",
        attempt=1,
        endpoint=None,
        transport="http",
        request_body=b"VISIBLE",
    )
    (record,) = capture.buffer
    assert "PRIVATE_NAME" not in repr(record.__dict__)
    assert "provider_<redacted>" in stream.getvalue() and "VISIBLE" in stream.getvalue()


def test_debug_enabled_after_input_was_fitted_never_previews_partial_wire_bytes() -> None:
    from collections.abc import Sequence

    from agent_search_gateway import input_limits
    from agent_search_gateway.providers.contracts import ChatMessage

    logger, stream = structured_test_logger("tests.preview.debug_switch")
    logger.setLevel(logging.INFO)

    def encode(messages: Sequence[ChatMessage]) -> bytes:
        return messages[0]["content"].encode()

    message = input_limits.TruncatableMessage("", "PRIVATE_PARTIAL" * 50)
    ready = input_limits.fit_messages_to_budget(
        [message],
        max_bytes=150,
        encode=encode,
        preview_projector=observability.make_input_preview_projector(logger),
    )
    assert ready.input_truncated and ready.preview_body is None and b"PRIVATE" in ready.content
    logger.setLevel(logging.DEBUG)
    observability.log_upstream_body_preview(
        logger,
        provider="sample",
        stage="fetch",
        endpoint=None,
        attempt=1,
        transport="agent_cli",
        prepared_body=ready,
        response_body=b"VISIBLE",
    )
    line = stream.getvalue()
    assert "request_body_preview=null" in line and "PRIVATE" not in line and "VISIBLE" in line
