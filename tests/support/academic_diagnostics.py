"""Shared contracts for academic response diagnostics, exercised with provider fixtures."""

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager

import httpx
import pytest

from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor
from agent_search_gateway.providers.http import HttpJsonExecutor


@asynccontextmanager
async def academic_response(
    provider: str,
    *,
    payload: object = None,
    response: httpx.Response | None = None,
) -> AsyncIterator[HttpJsonExecutor]:
    """Exercise adapters through the production executor with a single offline response."""
    if response is None:
        response = httpx.Response(200, json=payload)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        yield HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name=provider)


def candidate_case(
    case: str, valid: object, invalid: list[object]
) -> tuple[list[object], int, int]:
    items = {"valid": [valid], "mixed": [valid, *invalid], "all_invalid": invalid, "empty": []}[
        case
    ]
    accepted = int(case in ("valid", "mixed"))
    return items, accepted, len(items) - accepted


def event_fields(records: Sequence[logging.LogRecord], event: str) -> list[dict[str, object]]:
    return [
        vars(record)["gateway_fields"]
        for record in records
        if getattr(record, "gateway_event", None) == event
    ]


def assert_candidate_counts(
    records: Sequence[logging.LogRecord],
    provider: str,
    received: int,
    accepted: int,
    rejection_counts: Mapping[str, int],
    *,
    summary_expected: bool = True,
) -> None:
    summaries = event_fields(records, "provider_candidates_summary")
    assert len(summaries) == int(summary_expected)
    if summaries:
        fields = summaries[0]
        assert fields["provider"] == provider
        assert fields["stage"] == "paper_search"
        assert fields["phase"] == "adapter"
        assert fields["received"] == received
        assert fields["accepted"] == accepted
        assert fields["rejected"] == sum(rejection_counts.values())
        assert fields["rejection_counts"] == rejection_counts
    rejects = [fields["reason"] for fields in event_fields(records, "paper_candidate_rejected")]
    assert len(rejects) == sum(rejection_counts.values())
    assert {reason: rejects.count(reason) for reason in set(rejects)} == rejection_counts


def assert_json_candidates(
    caplog: pytest.LogCaptureFixture,
    provider: str,
    hits: Sequence[object],
    payload: object,
    received: int,
    accepted: int,
    rejected: int,
) -> None:
    from agent_search_gateway.providers.contracts import PaperSearchHit

    assert len(hits) == accepted
    if hits:
        assert isinstance(hits[0], PaperSearchHit)
        assert hits[0].authors == ()
        assert hits[0].abstract == ""
    expected = {"invalid_record_shape": 1, "missing_required_field": 3} if rejected else {}
    assert_candidate_counts(caplog.records, provider, received, accepted, expected)
    diagnostics = event_fields(caplog.records, "provider_response_diagnostic")
    assert len(diagnostics) == 1
    assert diagnostics[0]["semantic_result"] == "success"
    assert diagnostics[0]["response_kind"] == "object"
    assert diagnostics[0]["response_bytes"] == len(httpx.Response(200, json=payload).content)


def private_json_response(case: str, payload: object) -> httpx.Response:
    if case == "unknown":
        payload = {
            "error": {"message": "PRIVATE_NESTED_ERROR", "code": "PRIVATE_NESTED_CODE"},
            "body": "PRIVATE_BODY",
            "unexpected": {"error": "PRIVATE_RECURSIVE_ERROR"},
        }
    elif case == "array":
        payload = [{"message": "PRIVATE_ARRAY_MESSAGE"}]
    return (
        httpx.Response(
            200, content=b"PRIVATE_INVALID_JSON", headers={"content-type": "application/json"}
        )
        if case == "decode"
        else httpx.Response(200, json=payload)
    )


def assert_response_classification(
    caplog: pytest.LogCaptureFixture,
    response: httpx.Response,
    case: str,
    *,
    response_kind: str | None = None,
    invalid_envelope: bool | None = None,
    content_type: str | None = None,
) -> list[logging.LogRecord]:
    records = [record for record in caplog.records if getattr(record, "gateway_event", None)]
    diagnostics = event_fields(records, "provider_response_diagnostic")
    assert len(diagnostics) == 1
    fields = diagnostics[0]
    invalid = case != "success" if invalid_envelope is None else invalid_envelope
    assert fields["semantic_result"] == ("unknown" if invalid else "success")
    if invalid:
        assert fields["category"] == ("decode" if case == "decode" else "invalid_envelope")
    if response_kind is not None:
        assert fields["response_kind"] == response_kind
    assert not any(name.startswith("upstream_") for name in fields)
    assert fields["http_status"] == 200
    assert fields["response_bytes"] == len(response.content)
    assert len(event_fields(records, "http_attempt_completed")) == 1
    if content_type is not None:
        assert fields["content_type"] == content_type
    return records


def assert_private_payload_absent(records: Sequence[logging.LogRecord]) -> None:
    formatter = KeyValueFormatter(SecretRedactor([]))
    assert "PRIVATE_" not in "\n".join(formatter.format(record) for record in records)
