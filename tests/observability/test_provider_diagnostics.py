import asyncio
import logging
from collections.abc import Mapping

import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.observability import DiagnosticText, log_event
from agent_search_gateway.providers.diagnostics import (
    CandidateCounts,
    best_effort_diagnostics,
    diagnostic_fields_from_failure,
    safe_diagnostic_fields,
)
from tests.support.logging import structured_test_logger


def test_candidate_counts_render_as_bounded_structured_values() -> None:
    logger, stream = structured_test_logger("test.provider_diagnostics.counts")

    log_event(
        logger,
        logging.DEBUG,
        "provider_candidates_summary",
        rejection_counts={"missing_url": 3, "invalid_record": 2},
    )

    assert 'rejection_counts={"invalid_record":2,"missing_url":3}' in stream.getvalue()


def test_upstream_text_is_bounded_with_source_lengths() -> None:
    logger, stream = structured_test_logger("test.provider_diagnostics.lengths")
    log_event(
        logger,
        logging.DEBUG,
        "provider_response_diagnostic",
        upstream_code="c" * 129,
        upstream_message="界" * 513,
    )
    rendered = stream.getvalue()
    assert "upstream_code=" + "c" * 127 + "…" in rendered
    assert "c" * 128 not in rendered
    assert "upstream_message=" + "界" * 511 + "…" in rendered
    assert "界" * 512 not in rendered
    assert "upstream_message_chars=513" in rendered
    assert "upstream_message_bytes=1539" in rendered
    assert "upstream_message_truncated=true" in rendered


def test_upstream_text_removes_sensitive_material_and_control_characters() -> None:
    logger, stream = structured_test_logger("test.provider_diagnostics.material")
    credential = "PRIVATE_MATERIAL"
    log_event(
        logger,
        logging.DEBUG,
        "provider_response_diagnostic",
        upstream_message=(
            f"see https://{credential}@example.test/a?signature={credential}#{credential} "
            f"token={credential} Authorization: Bearer {credential}\r\n"
            "next\x00\x1b[31m line"
        ),
    )
    rendered = stream.getvalue()
    assert credential not in rendered
    assert "https://example.test/a" in rendered
    assert "\\u0000" not in rendered
    assert "\\u001b" not in rendered
    assert len(rendered.splitlines()) == 1
    assert "upstream_message_truncated=false" in rendered


def test_log_event_snapshots_rejection_counts_before_later_mutation() -> None:
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger, _ = structured_test_logger("test.provider_diagnostics.snapshot")
    logger.handlers[:] = [Capture()]
    counts = {"missing_url": 1}
    log_event(logger, logging.DEBUG, "provider_candidates_summary", rejection_counts=counts)
    counts["missing_url"] = 9
    assert vars(records[0])["gateway_fields"]["rejection_counts"] == {"missing_url": 1}


def test_selected_upstream_status_is_rendered_with_its_safe_text_limit() -> None:
    logger, stream = structured_test_logger("test.provider_diagnostics.status")
    log_event(
        logger, logging.DEBUG, "provider_response_diagnostic",
        exc_info=False,
        **safe_diagnostic_fields({"upstream_status": "ERROR", "upstream_code": -32000}),
    )
    assert "upstream_status=ERROR" in stream.getvalue()
    assert "upstream_code=-32000" in stream.getvalue()


class DangerousValue:
    def __str__(self) -> str:
        raise AssertionError("untrusted str called")

    def __repr__(self) -> str:
        raise AssertionError("untrusted repr called")


def test_field_selection_omits_unknown_nested_values_and_owner_fields() -> None:
    dangerous = DangerousValue()
    fields = safe_diagnostic_fields(
        {
            "provider": dangerous, "stage": dangerous, "model": dangerous,
            "attempt": dangerous, "event": dangerous, "reason": dangerous,
            "error_type": dangerous, "elapsed_ms": dangerous, "exc_info": dangerous,
            "logger": dangerous, "level": dangerous, "body": dangerous,
            "prompt": ["PRIVATE_CONTENT"], "response": {"message": "PRIVATE_CONTENT"},
            "stdout": dangerous, "stderr": dangerous, "unknown": dangerous,
            "upstream_code": dangerous, "upstream_status": {"code": dangerous},
            "upstream_message": [dangerous], "received": True, "response_bytes": -1,
            "category": "UNRECOGNIZED", "http_status": "200",
            "content_type": "application/json; credential=PRIVATE_CONTENT",
        }
    )
    assert fields == {"content_type": "application/json"}
    logger, stream = structured_test_logger("test.provider_diagnostics.unknown")
    log_event(logger, logging.DEBUG, "provider_response_diagnostic", exc_info=False, **fields)
    assert "PRIVATE_CONTENT" not in stream.getvalue()
    assert "DangerousValue" not in stream.getvalue()


@pytest.mark.parametrize(
    "counts",
    [
        {"unknown_reason": 1}, {"missing_url": -1}, {"missing_url": True},
        {"missing_url": 1.5}, {"missing_url": DangerousValue()},
        {f"unknown_{index}": 1 for index in range(100)},
    ],
)
def test_invalid_rejection_counts_never_become_serialized_containers(
    counts: dict[str, object],
) -> None:
    assert "rejection_counts" not in safe_diagnostic_fields({"rejection_counts": counts})
    logger, stream = structured_test_logger("test.provider_diagnostics.invalid_counts")
    log_event(
        logger, logging.DEBUG, "summary", rejection_counts=counts, other={"secret": "PRIVATE"}
    )
    rendered = stream.getvalue()
    assert "rejection_counts=<dict>" in rendered
    assert "other=<dict>" in rendered
    assert "PRIVATE" not in rendered
    assert "unknown_" not in rendered


def test_selected_fields_snapshot_counts_and_ignore_supplied_length_metadata() -> None:
    counts = {"invalid_record": 2}
    fields = safe_diagnostic_fields(
        {
            "upstream_message": "界", "upstream_message_chars": 999,
            "upstream_message_bytes": 999, "upstream_message_truncated": True,
            "rejection_counts": counts, "response_bytes": 42, "http_status": 200,
        }
    )
    counts["invalid_record"] = 99
    assert fields["rejection_counts"] == {"invalid_record": 2}
    assert isinstance(fields["upstream_message"], DiagnosticText)
    logger, stream = structured_test_logger("test.provider_diagnostics.metadata")
    log_event(logger, logging.DEBUG, "provider_response_diagnostic", exc_info=False, **fields)
    rendered = stream.getvalue()
    assert "upstream_message_chars=1" in rendered
    assert "upstream_message_bytes=3" in rendered
    assert "upstream_message_truncated=false" in rendered


def test_failure_extraction_ignores_arbitrary_exceptions_and_revalidates_known_data() -> None:
    class ForeignFailure(Exception):
        @property
        def diagnostics(self) -> object:
            raise AssertionError("arbitrary exception attribute inspected")

        def __str__(self) -> str:
            raise AssertionError("arbitrary exception string inspected")

    assert diagnostic_fields_from_failure(ForeignFailure()) == {}
    failure = ExecutionFailure(
        ErrorCode.PROTOCOL_ERROR, "public",
        diagnostics={"http_status": 502, "provider": "override", "received": 10},
    )
    failure.__cause__ = ForeignFailure()
    assert isinstance(failure.diagnostics, dict)
    failure.diagnostics.update(
        upstream_message=DangerousValue(), response_bytes=-1, reason="override"
    )
    assert diagnostic_fields_from_failure(failure) == {"http_status": 502}


def test_candidate_counts_are_local_and_track_zero_all_invalid_and_unprocessed() -> None:
    empty = CandidateCounts(0)
    assert empty.as_fields() == {
        "received": 0, "accepted": 0, "rejected": 0, "rejection_counts": {},
        "rewritten": 0, "processed": 0, "unprocessed": 0,
    }
    rejected = CandidateCounts(2)
    rejected.reject("missing_url")
    rejected.reject("invalid_record")
    assert rejected.rejected == 2
    assert rejected.processed == 2
    assert rejected.unprocessed == 0
    assert rejected.rejection_counts == {"missing_url": 1, "invalid_record": 1}
    counts = CandidateCounts(4)
    counts.accept(rewritten=True)
    counts.reject("missing_url")
    assert counts.as_fields() == {
        "received": 4, "accepted": 1, "rejected": 1, "rejection_counts": {"missing_url": 1},
        "rewritten": 1, "processed": 2, "unprocessed": 2,
    }
    snapshot = counts.as_fields()
    counts.accept()
    assert snapshot["accepted"] == 1
    assert rejected.accepted == 0
    assert empty.rejection_counts == {}


@pytest.mark.parametrize("received", [-1, True, 1.5])
def test_candidate_counts_reject_invalid_received_values(received: int) -> None:
    with pytest.raises(ValueError, match="nonnegative integer"):
        CandidateCounts(received)


def test_candidate_counts_reject_invalid_reasons_without_mutating_counts() -> None:
    counts = CandidateCounts(1)
    with pytest.raises(ValueError, match="unknown candidate rejection"):
        counts.reject("PRIVATE_REJECTION")
    assert counts.processed == 0
    counts.accept()
    with pytest.raises(ValueError, match="already been processed"):
        counts.reject("missing_url")
    assert counts.rejection_counts == {}


def test_best_effort_extractor_failure_does_not_change_business_return() -> None:
    def broken() -> Mapping[str, object]:
        raise ValueError("PRIVATE_EXTRACTOR_DATA")

    def business() -> str:
        logger, stream = structured_test_logger("test.provider_diagnostics.extractor")
        log_event(logger, logging.DEBUG, "provider_response_diagnostic",
                  exc_info=False, **best_effort_diagnostics(broken))
        assert "PRIVATE_EXTRACTOR_DATA" not in stream.getvalue()
        return "business result"

    assert business() == "business result"


def test_best_effort_extractor_propagates_cancellation() -> None:
    def cancelled() -> Mapping[str, object]:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        best_effort_diagnostics(cancelled)


def test_field_selection_contains_mapping_failures_and_preserves_cancellation() -> None:
    class BrokenMapping(dict[str, object]):
        def get(self, key: str, default: object = None) -> object:
            raise ValueError("PRIVATE_MAPPING_DATA")

    class CancelMapping(dict[str, object]):
        def get(self, key: str, default: object = None) -> object:
            raise asyncio.CancelledError

    assert safe_diagnostic_fields(BrokenMapping()) == {}
    with pytest.raises(asyncio.CancelledError):
        safe_diagnostic_fields(CancelMapping())


def test_arbitrary_scalar_subclasses_do_not_run_their_representations() -> None:
    class DangerousInteger(int):
        def __repr__(self) -> str:
            raise AssertionError("untrusted numeric repr called")

    logger, stream = structured_test_logger("test.provider_diagnostics.scalar_subclasses")
    log_event(
        logger, logging.DEBUG, "diagnostic", unknown=DangerousValue(), number=DangerousInteger(1)
    )
    rendered = stream.getvalue()
    assert "unknown=<DangerousValue>" in rendered
    assert "number=<DangerousInteger>" in rendered


def test_failure_extraction_leaves_category_to_the_terminal_owner() -> None:
    fields = {"category": "decode", "http_status": 200}
    assert safe_diagnostic_fields(fields)["category"] == "decode"
    failure = ExecutionFailure(ErrorCode.PROTOCOL_ERROR, "public", diagnostics=fields)
    assert diagnostic_fields_from_failure(failure) == {"http_status": 200}


def test_numeric_upstream_code_obeys_the_same_text_limit() -> None:
    logger, stream = structured_test_logger("test.provider_diagnostics.numeric_code")
    log_event(
        logger, logging.DEBUG, "provider_response_diagnostic", exc_info=False,
        **safe_diagnostic_fields({"upstream_code": 10 ** 200}),
    )
    rendered = stream.getvalue()
    assert "upstream_code=1" + "0" * 126 + "…" in rendered
    assert "0" * 127 not in rendered


@pytest.mark.parametrize(
    "reason",
    [
        "duplicate", "missing_title", "missing_source_id", "invalid_identifier",
        "identifier_conflict", "invalid_date", "invalid_landing_url",
    ],
)
def test_candidate_counts_preserve_existing_keyword_and_paper_rejection_reasons(
    reason: str,
) -> None:
    logger, stream = structured_test_logger("test.provider_diagnostics.existing_reasons")
    counts = CandidateCounts(1)
    counts.reject(reason)
    log_event(
        logger, logging.DEBUG, "provider_candidates_summary", exc_info=False, **counts.as_fields()
    )
    assert f'rejection_counts={{"{reason}":1}}' in stream.getvalue()
