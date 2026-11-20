"""Exercise contextual body redaction through the real record and formatter."""

import json
from logging.handlers import BufferingHandler

import pytest

from agent_search_gateway.observability import log_upstream_body_preview
from tests.support.logging import structured_test_logger

_FIXTURE = "synthetic-example"


def _response_fields(body: str) -> tuple[dict[str, object], str]:
    logger, stream = structured_test_logger("tests.preview.context")
    capture = BufferingHandler(10)
    logger.addHandler(capture)
    raw = body.encode()
    log_upstream_body_preview(
        logger,
        provider="example",
        stage="search",
        endpoint=None,
        attempt=1,
        transport="http",
        response_body=raw,
    )
    (record,) = capture.buffer
    fields: dict[str, object] = vars(record)["gateway_fields"]
    assert fields["response_body_bytes"] == len(raw)
    return fields, stream.getvalue()


@pytest.mark.parametrize(
    "field",
    [
        "authToken",
        "AUTH-TOKEN",
        "auth_token",
        "private_key",
        "privateKey",
        "PRIVATE-KEY",
        "credentials",
        "jwt",
    ],
)
@pytest.mark.parametrize("context", ["json", "quoted_text"])
def test_credential_field_is_redacted_before_logging(field: str, context: str) -> None:
    if context == "json":
        body = json.dumps({field: {"nested": _FIXTURE}, "publicKey": "VISIBLE"})
    else:
        body = f'Details: "{field}": "{_FIXTURE}", publicKey: VISIBLE'
    fields, output = _response_fields(body)
    for observed in (repr(fields), output):
        assert _FIXTURE not in observed
        assert "VISIBLE" in observed and "<redacted>" in observed


@pytest.mark.parametrize(
    "label",
    [
        "PRIVATE KEY",
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
    ],
)
@pytest.mark.parametrize("wrapped", [False, True])
def test_unquoted_pem_private_key_preserves_adjacent_fields(label: str, wrapped: bool) -> None:
    snippet = "\n".join(
        [
            f"private_key: -----BEGIN {label}-----",
            _FIXTURE,
            f"-----END {label}-----",
            "note: VISIBLE",
        ]
    )
    fields, output = _response_fields(json.dumps({"snippet": snippet}) if wrapped else snippet)
    for observed in (repr(fields), output):
        assert _FIXTURE not in observed and "-----END" not in observed
        assert "VISIBLE" in observed and "<redacted>" in observed


def test_pem_footer_inside_a_line_does_not_expose_the_remaining_value() -> None:
    lines = [
        "private_key: -----BEGIN RSA PRIVATE KEY-----",
        "prefix-----END RSA PRIVATE KEY-----suffix",
        "-----END RSA PRIVATE KEY-----suffix",
        "-----END RSA PRIVATE KEY----- is a diagnostic example",
        _FIXTURE,
        "-----END RSA PRIVATE KEY-----  \t",
        "note: VISIBLE",
    ]
    fields, output = _response_fields("\r\n".join(lines))
    for observed in (repr(fields), output):
        assert _FIXTURE not in observed and "VISIBLE" in observed


def test_quote_inside_pem_metadata_does_not_end_the_redaction() -> None:
    lines = [
        'private_key="-----BEGIN RSA PRIVATE KEY-----',
        'Comment: "example"',
        _FIXTURE,
        '-----END RSA PRIVATE KEY-----"',
        "note: VISIBLE",
    ]
    fields, output = _response_fields("\n".join(lines))
    for observed in (repr(fields), output):
        assert _FIXTURE not in observed and "VISIBLE" in observed


@pytest.mark.parametrize("prefix", ["", "Authorization: ", "token="])
def test_two_unquoted_pem_blocks_are_masked_even_inside_a_header(prefix: str) -> None:
    lines = [
        prefix + "-----BEGIN PRIVATE KEY-----",
        _FIXTURE,
        "-----END PRIVATE KEY-----",
        "VISIBLE",
        "-----BEGIN EC PRIVATE KEY-----",
        "SYNTHETIC_SECOND",
        "-----END EC PRIVATE KEY-----",
    ]
    fields, output = _response_fields("\n".join(lines))
    for observed in (repr(fields), output):
        assert _FIXTURE not in observed and "SYNTHETIC_SECOND" not in observed
        assert "VISIBLE" in observed


@pytest.mark.parametrize("end_label", [None, "EC PRIVATE KEY"])
def test_unquoted_pem_private_key_with_no_matching_footer_masks_to_end(
    end_label: str | None,
) -> None:
    lines = ["VISIBLE private_key: -----BEGIN RSA PRIVATE KEY-----", _FIXTURE]
    if end_label is not None:
        lines.append(f"-----END {end_label}-----")
    lines.append("SYNTHETIC_CONTINUATION")
    fields, output = _response_fields("\n".join(lines))
    for observed in (repr(fields), output):
        assert "VISIBLE" in observed
        assert _FIXTURE not in observed and "SYNTHETIC_CONTINUATION" not in observed


def test_quoted_member_name_with_unquoted_pem_uses_only_the_matching_footer() -> None:
    lines = [
        '"private_key": -----BEGIN RSA PRIVATE KEY-----',
        _FIXTURE,
        "-----END EC PRIVATE KEY-----",
        "SYNTHETIC_CONTINUATION",
        "-----END RSA PRIVATE KEY-----",
        "note: VISIBLE",
    ]
    fields, output = _response_fields("\n".join(lines))
    for observed in (repr(fields), output):
        assert _FIXTURE not in observed and "SYNTHETIC_CONTINUATION" not in observed
        assert "VISIBLE" in observed and "<redacted>" in observed


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_escaped_separator_created_by_redaction_stays_single_line(separator: str) -> None:
    body = json.dumps({"note": f"HEAD{separator}password={_FIXTURE} TAIL"})
    assert separator not in body
    fields, output = _response_fields(body)
    preview = fields["response_body_preview"]
    assert isinstance(preview, str)
    for observed in (preview, output):
        assert separator not in observed and _FIXTURE not in observed
        assert "HEAD" in observed and "TAIL" in observed
    assert len(output.splitlines()) == 1


def test_unsafe_control_created_by_redaction_is_withheld() -> None:
    fields, output = _response_fields(json.dumps({"note": f"HEAD\u009bpassword={_FIXTURE} TAIL"}))
    assert fields["response_body_preview"] is None
    assert "\u009b" not in output and _FIXTURE not in output


def test_registered_secret_spans_coalesce_overlaps_but_keep_adjacent_matches() -> None:
    from agent_search_gateway.observability import SecretRedactor, SecretValue

    redactor = SecretRedactor([SecretValue("aba")])
    assert redactor.sensitive_spans("ababaaba aba") == [(0, 5), (5, 8), (9, 12)]
    repeated = "ab" * 4096 + "a"
    assert redactor.sensitive_spans(repeated) == [(0, len(repeated))]


@pytest.mark.parametrize("reference", ["/download", "../download", "", "#"])
@pytest.mark.parametrize("context", ["html", "json"])
def test_relative_url_in_explicit_context(reference: str, context: str) -> None:
    url = reference + (_FIXTURE if reference == "#" else f"?ticket={_FIXTURE}#{_FIXTURE}")
    if context == "html":
        body = f'VISIBLE <a href="{url}">link</a> END'
    else:
        body = json.dumps({"url": url, "note": "VISIBLE ordinary?question#topic END"})
    fields, output = _response_fields(body)
    for observed in (repr(fields), output):
        assert _FIXTURE not in observed
        assert "VISIBLE" in observed and "END" in observed
        if "download" in reference:
            assert reference in observed
        if context == "json":
            assert "ordinary?question#topic" in observed


@pytest.mark.parametrize("delimiter", ["&quest;", "&#63;"])
def test_entity_in_html_url_does_not_hide_the_query(delimiter: str) -> None:
    body = f'VISIBLE <a href="/download{delimiter}ticket={_FIXTURE}">link</a> END'
    fields, output = _response_fields(json.dumps({"html": body}))
    for observed in (repr(fields), output):
        assert _FIXTURE not in observed
        assert "VISIBLE" in observed and ">link</a>" in observed and "END" in observed


@pytest.mark.parametrize("wrapped", [False, True])
def test_leading_quoted_credential_member(wrapped: bool) -> None:
    snippet = json.dumps({"access_token": _FIXTURE, "note": "VISIBLE"})[1:-1]
    fields, output = _response_fields(json.dumps({"snippet": snippet}) if wrapped else snippet)
    for observed in (repr(fields), output):
        assert _FIXTURE not in observed
        assert "VISIBLE" in observed


@pytest.mark.parametrize("citation", ["[1–3]", "[10-12]"])
def test_numeric_citation_in_json_abstract(citation: str) -> None:
    abstract = f"Earlier studies {citation} agree."
    fields, output = _response_fields(json.dumps({"abstract": abstract}, ensure_ascii=False))
    for observed in (repr(fields), output):
        assert abstract in observed


def test_incomplete_array_after_citation_still_fails_closed() -> None:
    fragment = json.dumps([2, {"token": _FIXTURE}])[:-1]
    fields, output = _response_fields("Nearby [1-3]. Data: " + fragment)
    assert fields["response_body_preview"] is None
    assert _FIXTURE not in repr(fields) + output


@pytest.mark.parametrize("context", ["html", "json"])
def test_relative_url_across_a_prompt_cut_keeps_only_safe_edges(context: str) -> None:
    from agent_search_gateway.input_limits import TruncatableMessage, fit_messages_to_budget
    from agent_search_gateway.observability import make_input_preview_projector

    logger, stream = structured_test_logger("tests.preview.relative_cut")
    capture = BufferingHandler(10)
    logger.addHandler(capture)
    url = f"/file?ticket={_FIXTURE}" + "discarded" * 200 + f"#{_FIXTURE}"
    if context == "html":
        text = f'HEAD <a href="{url}">file</a> TAIL'
    else:
        text = json.dumps({"note": "HEAD", "url": url, "end": "TAIL"})
    prepared = fit_messages_to_budget(
        [TruncatableMessage("", text)],
        max_bytes=160,
        encode=lambda messages: messages[0]["content"].encode(),
        preview_projector=make_input_preview_projector(logger),
    )
    assert prepared.input_truncated and _FIXTURE.encode() in prepared.content
    log_upstream_body_preview(
        logger,
        provider="example",
        stage="llm",
        endpoint=None,
        attempt=1,
        transport="http",
        prepared_body=prepared,
    )
    (record,) = capture.buffer
    for observed in (repr(vars(record)["gateway_fields"]), stream.getvalue()):
        assert _FIXTURE not in observed and "discarded" not in observed
        assert "HEAD" in observed and "/file" in observed and "TAIL" in observed
