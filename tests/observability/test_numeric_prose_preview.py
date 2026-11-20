"""Check numeric prose through the observable preview fields and their formatter."""

import json
from logging.handlers import BufferingHandler

import pytest

from agent_search_gateway.observability import log_upstream_body_preview
from tests.support.logging import structured_test_logger


def _observe(body: str, *, request: str = "visible request") -> tuple[dict[str, object], str]:
    logger, output = structured_test_logger("tests.preview.numeric_prose")
    capture = BufferingHandler(10)
    logger.addHandler(capture)
    raw = body.encode()
    log_upstream_body_preview(
        logger,
        provider="example",
        stage="fetch",
        endpoint=None,
        attempt=1,
        transport="http",
        request_body=request.encode(),
        response_body=raw,
    )
    (record,) = capture.buffer
    fields: dict[str, object] = vars(record)["gateway_fields"]
    assert fields["response_body_bytes"] == len(raw)
    assert fields["request_body_preview"] == request
    assert fields["response_preview_truncated"] is False
    return fields, output.getvalue()


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize(
    "destination,expected",
    [
        ("https://reader@example.test/reports", "https://example.test/reports"),
        ("../reports", "../reports"),
        ("//example.test/reports", "//example.test/reports"),
    ],
)
def test_simple_numeric_markdown_link_preserves_title_and_safe_path(
    destination: str, expected: str, wrapped: bool
) -> None:
    body = f"Before [2024 Annual Report]({destination}?edition=HIDDEN_QUERY#HIDDEN_FRAGMENT) after."
    fields, output = _observe(json.dumps({"snippet": body}) if wrapped else body)
    assert isinstance(fields["response_body_preview"], str)
    for observed in (str(fields["response_body_preview"]), output):
        assert f"Before [2024 Annual Report]({expected}) after." in observed
        assert "HIDDEN_" not in observed and "reader@" not in observed


@pytest.mark.parametrize("wrapped", [False, True])
def test_numeric_reference_list_keeps_following_credential_redaction(wrapped: bool) -> None:
    body = "See [1, 2 and 3]. More: Bearer EXAMPLE_VALUE end."
    fields, output = _observe(json.dumps({"snippet": body}) if wrapped else body)
    for observed in (str(fields["response_body_preview"]), output):
        assert "See [1, 2 and 3]." in observed and "end." in observed
        assert "EXAMPLE_VALUE" not in observed


@pytest.mark.parametrize(
    "destination,expected",
    [
        ("/reports/$annual?x=HIDDEN_FIRST&y=HIDDEN_SECOND#HIDDEN_FRAGMENT", "/reports/$annual"),
        ("/reports&quest;edition=HIDDEN_QUERY", "<redacted>"),
        ("/reports&#35;HIDDEN_FRAGMENT", "<redacted>"),
        ("/reports%3Fedition=HIDDEN_QUERY", "<redacted>"),
    ],
)
def test_numeric_link_destination_delimiters(destination: str, expected: str) -> None:
    fields, output = _observe(f"Before [2024 Annual Report]({destination}) after.")
    for observed in (str(fields["response_body_preview"]), output):
        assert f"[2024 Annual Report]({expected}) after." in observed
        assert "HIDDEN_" not in observed


def test_unescaped_space_in_numeric_link_destination_is_ambiguous() -> None:
    fields, _ = _observe("[2024 Annual Report](/two words)")
    assert fields["response_body_preview"] is None


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("before", [False, True])
@pytest.mark.parametrize(
    "fragment",
    [r'"\u006bey": "SYNTHETIC_QUERY"', '"url": "../reports?edition=SYNTHETIC_QUERY"'],
)
def test_bare_quoted_member_beside_numeric_prose_stays_unavailable(
    fragment: str, before: bool, wrapped: bool
) -> None:
    citation = "[1, 2 and 3]"
    first, second = (fragment, citation) if before else (citation, fragment)
    text = f"See {first}. Then {second}."
    fields, output = _observe(json.dumps({"snippet": text}) if wrapped else text)
    assert fields["response_body_preview"] is None
    assert "SYNTHETIC_QUERY" not in repr(fields) + output


@pytest.mark.parametrize("nested_citation", [False, True])
@pytest.mark.parametrize(
    "fragment",
    [r'"\u006bey": "SYNTHETIC_QUERY"', '"url": "../reports?edition=SYNTHETIC_QUERY"'],
)
def test_numeric_prose_and_bare_member_at_different_depths(
    fragment: str, nested_citation: bool
) -> None:
    citation = "See [1, 2 and 3]."
    member = "Detail: " + fragment
    inner, outer = (citation, member) if nested_citation else (member, citation)
    text = "Memo " + json.dumps({"snippet": inner}) + " Then " + outer
    fields, output = _observe(text)
    assert fields["response_body_preview"] is None
    assert "SYNTHETIC_QUERY" not in repr(fields) + output


def test_numeric_prose_in_a_leading_encoded_string_propagates() -> None:
    text = json.dumps("See [1, 2 and 3].") + ' Then "url": "../reports?edition=SYNTHETIC_QUERY"'
    fields, output = _observe(text)
    assert fields["response_body_preview"] is None
    assert "SYNTHETIC_QUERY" not in repr(fields) + output


@pytest.mark.parametrize("citation_first", [False, True])
def test_prose_classification_is_local_to_each_side(citation_first: bool) -> None:
    citation, note = "See [1, 2 and 3].", 'The author wrote "ready".'
    request, response = (citation, note) if citation_first else (note, citation)
    fields, _ = _observe(response, request=request)
    assert fields["response_body_preview"] == response


@pytest.mark.parametrize("wrapped", [False, True])
def test_complete_json_member_beside_numeric_prose_remains_visible(wrapped: bool) -> None:
    example = json.dumps({"url": "../reports?edition=SYNTHETIC_QUERY", "title": "ANNUAL"})
    body = f"See [1, 2 and 3]. Details {example} end."
    fields, output = _observe(json.dumps({"snippet": body}) if wrapped else body)
    for observed in (str(fields["response_body_preview"]), output):
        assert "[1, 2 and 3]" in observed and "../reports" in observed and "ANNUAL" in observed
        assert "SYNTHETIC_QUERY" not in observed


def test_numeric_title_still_runs_plaintext_redaction() -> None:
    fields, output = _observe("Before [2024 Bearer EXAMPLEVALUE](/reports) after.")
    for observed in (str(fields["response_body_preview"]), output):
        assert "EXAMPLEVALUE" not in observed
        assert "Before [2024 " in observed and "](/reports) after." in observed


@pytest.mark.parametrize("extra", [0, 1])
def test_numeric_bracket_window_is_bounded(extra: int) -> None:
    label = "[2024 " + "A" * (121 + extra) + "]"
    assert len(label) == 128 + extra
    fields, _ = _observe(label + "(/reports)")
    assert (fields["response_body_preview"] is None) == bool(extra)


@pytest.mark.parametrize("extra", [0, 1])
def test_numeric_markdown_destination_window_is_bounded(extra: int) -> None:
    path = "/" + "a" * (2047 + extra)
    fields, _ = _observe("[2024 Annual Report](" + path + ")")
    assert (fields["response_body_preview"] is None) == bool(extra)


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1,}',
        r'{"row":{"\u006bey":"EXAMPLE_VALUE",}}',
        '[2, {"note": "EXAMPLE_VALUE"}',
        "[[[[2024 Annual Report](/reports)",
        '[1, 2 and 3, "sample"]',
        r"[2024 Annual\ Report](/reports)",
        "[2024 Annual Report](/ambiguous(link)?edition=EXAMPLE_VALUE)",
    ],
)
def test_ambiguous_brackets_and_malformed_json_remain_unavailable(text: str) -> None:
    fields, output = _observe(text)
    assert fields["response_body_preview"] is None
    assert "EXAMPLE_VALUE" not in repr(fields) + output
