"""Check response preview fields independently of nullable correlation fields."""

from logging.handlers import BufferingHandler

import pytest

from agent_search_gateway import observability
from tests.support.logging import structured_test_logger


@pytest.mark.parametrize(
    "raw,expected,clipped",
    [
        (b"", None, False),
        (b'{"value":"UNFINISHED', None, True),
        (b'{"ok":"VISIBLE"}', "VISIBLE", True),
    ],
    ids=["empty", "malformed-json", "complete-json"],
)
def test_incomplete_response_preview_uses_its_own_field(
    raw: bytes, expected: str | None, clipped: bool
) -> None:
    logger, stream = structured_test_logger("tests.preview.response_field")
    capture = BufferingHandler(10)
    logger.addHandler(capture)
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
    (record,) = capture.buffer
    fields = vars(record)["gateway_fields"]
    preview = fields["response_body_preview"]
    if expected is None:
        assert preview is None
    else:
        assert isinstance(preview, str)
        assert expected in preview
    assert fields["response_preview_truncated"] is clipped
    assert fields["response_body_bytes"] == len(raw)
    assert "UNFINISHED" not in stream.getvalue()
