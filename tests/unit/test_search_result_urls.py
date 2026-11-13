from urllib.parse import quote

import pytest

from agent_search_gateway.search_result_urls import (
    InvalidSearchResultRedirect,
    unwrap_search_result_url,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://www.google.com/goto?url=https%3A%2F%2Fexample.org%2Fpaper%3Fx%3D1",
            "https://example.org/paper?x=1",
        ),
        ("http://google.com/goto?url=http%3A%2F%2Fexample.org%2Fa", "http://example.org/a"),
    ],
)
def test_google_goto_destination(raw: str, expected: str) -> None:
    assert unwrap_search_result_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.org/a",
        "https://[broken/a",
        "https://example.org:bad/a",
        "not a URL",
        "https://example.org/%GG",
        "https://google.com:bad/other?url=unused",
        "https://www.google.com/url?q=https%3A%2F%2Fexample.org",
        "https://google.com.evil.test/goto?url=https%3A%2F%2Fexample.org",
        "https://google.com@evil.test/goto?url=https%3A%2F%2Fexample.org",
        "https://example.org/goto?url=https%3A%2F%2Fexample.org",
        "https://google.com/goto/?url=https%3A%2F%2Fexample.org",
        "https://google.com/GOTO?url=https%3A%2F%2Fexample.org",
        "//google.com/goto?url=https%3A%2F%2Fexample.org",
        "ftp://google.com/goto?url=https%3A%2F%2Fexample.org",
    ],
)
def test_unknown_shapes_are_unchanged(raw: str) -> None:
    assert unwrap_search_result_url(raw) == raw


@pytest.mark.parametrize(
    "query",
    [
        "",
        "x=1",
        "url=",
        "url",
        "url=https%3A%2F%2Fexample.org&url=",
        "url=https%3A%2F%2Fexample.org&%75rl=https%3A%2F%2Fevil.test",
    ],
)
def test_missing_empty_or_ambiguous_destination_is_rejected(query: str) -> None:
    with pytest.raises(InvalidSearchResultRedirect, match="^recognized_invalid_redirect$"):
        unwrap_search_result_url(f"https://google.com/goto?{query}")


@pytest.mark.parametrize(
    "authority",
    [
        "user@google.com",
        "@google.com",
        "user:pass@www.google.com",
        "google.com:bad",
        "google.com:65536",
        "google.com:-1",
    ],
)
def test_untrustworthy_wrapper_authority_is_rejected(authority: str) -> None:
    with pytest.raises(InvalidSearchResultRedirect, match="^recognized_invalid_redirect$"):
        unwrap_search_result_url(f"https://{authority}/goto?url=https%3A%2F%2Fexample.org")


@pytest.mark.parametrize(
    "target",
    [
        "javascript:alert(1)",
        "file:///tmp/paper",
        "/relative",
        "https:///missing-host",
        "https://example.org:bad/a",
        "https://example.org:65536/a",
        "https://[broken/a",
    ],
)
def test_unsafe_destination_is_rejected(target: str) -> None:
    with pytest.raises(InvalidSearchResultRedirect, match="^recognized_invalid_redirect$"):
        unwrap_search_result_url(f"https://google.com/goto?url={quote(target, safe='')}")


@pytest.mark.parametrize(
    "query",
    [
        "url=https%3A%2F%2Fexample.org%2F%",
        "url=https%3A%2F%2Fexample.org%2F%2",
        "url=https%3A%2F%2Fexample.org%2F%GG",
        "url=https%3A%2F%2Fexample.org%2F%FF",
        "url=https%3A%2F%2Fexample.org%2F%C0%AF",
        "url=https%3A%2F%2Fexample.org&extra=%FF",
        "url=https%3A%2F%2Fexample.org&%GG=unused",
    ],
)
def test_malformed_outer_query_is_rejected(query: str) -> None:
    with pytest.raises(InvalidSearchResultRedirect, match="^recognized_invalid_redirect$"):
        unwrap_search_result_url(f"https://google.com/goto?{query}")


@pytest.mark.parametrize("suffix", ["%", "%2", "%GG"])
def test_malformed_destination_escape_is_rejected(suffix: str) -> None:
    target = f"https://example.org/path{suffix}"
    with pytest.raises(InvalidSearchResultRedirect, match="^recognized_invalid_redirect$"):
        unwrap_search_result_url(f"https://google.com/goto?url={quote(target, safe='')}")


@pytest.mark.parametrize(
    "raw",
    [
        "\nhttps://google.com/goto?url=https%3A%2F%2Fexample.org",
        "https://goo\tgle.com/goto?url=https%3A%2F%2Fexample.org",
        "https://google.com/go\rto?url=https%3A%2F%2Fexample.org",
        "https://google.com/goto?url=https%3A%2F%2Fexample.org\n",
        "https://google.com/goto?url=https%3A%2F%2Fexample.org#\x00",
        "https://google.com/goto?url=https%3A%2F%2Fexample.org%0Apath",
        "https://google.com/goto?url=%09https%3A%2F%2Fexample.org",
        "https://google.com/goto?url=https%3A%2F%2Fexample.org%00",
        "https://google.com/goto?url=https%3A%2F%2Fexample.org%7F",
        "https://google.com/goto?url=https%3A%2F%2Fexample.org%C2%85",
    ],
)
def test_raw_wrapper_and_decoded_target_controls_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidSearchResultRedirect, match="^recognized_invalid_redirect$"):
        unwrap_search_result_url(raw)


@pytest.mark.parametrize(
    "target",
    [
        "https://例子.org/论文?q=雪",
        "https://example.org/a%2Fb?next=a%26b%3Dc&literal=%252F",
        "https://example.org/a?plus=%2B&raw=+",
        "https://example.org/a?signature=[REDACTED_SECRET]&expires=123#part",
        "https://google.com/goto?url=https%3A%2F%2Fexample.org%2Finner",
    ],
)
def test_destination_encoding_is_preserved_after_one_outer_decode(target: str) -> None:
    assert (
        unwrap_search_result_url(f"https://www.google.com/goto?url={quote(target, safe='')}")
        == target
    )


def test_outer_encoded_plus_is_not_decoded_twice() -> None:
    assert (
        unwrap_search_result_url(
            "https://google.com/goto?url=https%3A%2F%2Fexample.org%2F%252F%3Fx%3D%2B"
        )
        == "https://example.org/%2F?x=+"
    )


def test_duplicate_destination_is_rejected() -> None:
    with pytest.raises(InvalidSearchResultRedirect, match="^recognized_invalid_redirect$"):
        unwrap_search_result_url(
            "https://google.com/goto?url=https%3A%2F%2Fexample.org&url=https%3A%2F%2Fevil.test"
        )
