"""Offline smoke validation for previews omitted by their work budget."""

import json
import logging

import pytest

from agent_search_gateway.input_limits import MAX_PREVIEW_SCAN_BYTES
from agent_search_gateway.observability import log_event, log_upstream_body_preview
from tests.integration.test_live_rate_limits_and_inputs import _Observation, _probe


@pytest.mark.parametrize("size", [MAX_PREVIEW_SCAN_BYTES, MAX_PREVIEW_SCAN_BYTES + 1])
def test_live_probe_accepts_response_at_or_above_preview_budget(
    size: int, capsys: pytest.CaptureFixture[str]
) -> None:
    obs = _Observation("preview_check", "none", attempts=1, status=200, valid=True)
    with _probe(obs, ()):
        log_upstream_body_preview(
            logging.getLogger("agent_search_gateway"),
            provider=obs.provider,
            stage="search",
            endpoint="https://example.test/search",
            attempt=1,
            transport="http",
            response_body=b"x" * size,
            response_body_bytes=size,
        )
    assert json.loads(capsys.readouterr().out)["category"] == "success"


def test_live_probe_rejects_non_budget_preview_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    obs = _Observation("preview_check", "none", attempts=1, status=200, valid=True)
    with pytest.raises(pytest.fail.Exception, match="response_contract"), _probe(obs, ()):
        log_upstream_body_preview(
            logging.getLogger("agent_search_gateway"),
            provider=obs.provider,
            stage="search",
            endpoint="https://example.test/search",
            attempt=1,
            transport="http",
            response_body=b"\x00",
        )
    assert json.loads(capsys.readouterr().out)["category"] == "response_contract"


def test_live_probe_rejects_missing_budget_omission_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    obs = _Observation("preview_check", "none", attempts=1, status=200, valid=True)
    with pytest.raises(pytest.fail.Exception, match="response_contract"), _probe(obs, ()):
        log_event(
            logging.getLogger("agent_search_gateway"),
            logging.DEBUG,
            "upstream_body_preview",
            request_body_preview=None,
            request_body_sent_bytes=0,
            request_body_original_bytes=0,
            request_input_truncated=False,
            request_preview_truncated=False,
            response_body_preview=None,
            response_body_bytes=MAX_PREVIEW_SCAN_BYTES + 1,
            response_preview_truncated=False,
        )
    assert json.loads(capsys.readouterr().out)["category"] == "response_contract"
