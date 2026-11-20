"""Verify the debug preview's UTF-8 work budget with ordinary text."""

import logging
from collections.abc import Callable
from logging.handlers import BufferingHandler

import pytest

from agent_search_gateway.body_preview import BodyScrubber
from agent_search_gateway.input_limits import (
    PreparedInput,
    PreviewScanBudgetExceeded,
    TruncatableMessage,
    fit_messages_to_budget,
)
from agent_search_gateway.observability import (
    log_upstream_body_preview,
    make_input_preview_projector,
)
from tests.support.logging import structured_test_logger

BUDGET = 256 * 1024


def _record(
    logger: logging.Logger,
    *,
    response: bytes | Callable[[], bytes] = b"visible response",
    size: int | None = None,
    prepared: PreparedInput | None = None,
) -> dict[str, object]:
    capture = BufferingHandler(10)
    logger.addHandler(capture)
    log_upstream_body_preview(
        logger,
        provider="example",
        stage="summary",
        endpoint=None,
        attempt=1,
        transport="http",
        request_body=b"visible request",
        prepared_body=prepared,
        response_body=response,
        response_body_bytes=size,
    )
    (record,) = capture.buffer
    fields: dict[str, object] = vars(record)["gateway_fields"]
    return fields


@pytest.mark.parametrize("known_size", [True, False])
def test_over_budget_response_keeps_counts_and_request(known_size: bool) -> None:
    raw = ("é" * (BUDGET // 2) + "x").encode()
    calls = 0

    def provide_bytes() -> bytes:
        nonlocal calls
        calls += 1
        return raw

    logger, output = structured_test_logger("tests.preview.over_budget")
    fields = _record(logger, response=provide_bytes, size=len(raw) if known_size else None)
    assert calls == (0 if known_size else 1)
    assert fields["response_body_preview"] is None and fields["response_preview_truncated"] is True
    assert fields["response_body_bytes"] == len(raw)
    assert fields["request_body_preview"] == "visible request"
    assert "é" not in output.getvalue()


def test_scrub_budget_rejects_multibyte_text_before_scanning() -> None:
    visited = []

    def observe(text: str) -> list[tuple[int, int]]:
        visited.append(len(text))
        return []

    scrubber = BodyScrubber(str, observe, str)
    with pytest.raises(PreviewScanBudgetExceeded):
        scrubber.scrub("猫" * (BUDGET // 3 + 1))
    assert visited == []


@pytest.mark.parametrize("size", [96 * 1024, 128 * 1024, BUDGET])
def test_measured_input_at_or_below_work_budget_is_visible(size: int) -> None:
    logger, _ = structured_test_logger("tests.preview.allowed_budget")
    text = "é" * (size // 2)
    prepared = fit_messages_to_budget(
        [TruncatableMessage("", text)],
        max_bytes=size,
        encode=lambda messages: messages[0]["content"].encode(),
        preview_projector=make_input_preview_projector(logger),
    )
    fields = _record(logger, prepared=prepared)
    assert fields["request_body_sent_bytes"] == fields["request_body_original_bytes"] == size
    assert fields["request_input_truncated"] is False
    assert isinstance(fields["request_body_preview"], str)
    assert fields["request_preview_truncated"] is True


def test_large_original_projected_at_smaller_send_budget_preserves_wire_input() -> None:
    logger, output = structured_test_logger("tests.preview.original_budget")
    original = "HEAD" + "猫" * (BUDGET // 3) + "TAIL"
    prepared = fit_messages_to_budget(
        [TruncatableMessage("", original)],
        max_bytes=128 * 1024,
        encode=lambda messages: messages[0]["content"].encode(),
        preview_projector=make_input_preview_projector(logger),
    )
    assert prepared.content.startswith(b"HEAD") and prepared.content.endswith(b"TAIL")
    assert 0 < len(prepared.content) <= 128 * 1024 < len(original.encode())
    fields = _record(logger, prepared=prepared)
    assert fields["request_body_preview"] is None and fields["request_preview_truncated"] is True
    assert fields["request_input_truncated"] is True
    assert fields["request_body_original_bytes"] == len(original.encode())
    assert fields["request_body_sent_bytes"] == len(prepared.content)
    assert fields["response_body_preview"] == "visible response"
    assert "猫" not in output.getvalue()
