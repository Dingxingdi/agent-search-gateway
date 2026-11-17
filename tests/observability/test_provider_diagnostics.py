import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import (
    DiagnosticText,
    KeyValueFormatter,
    SecretRedactor,
    SecretValue,
    log_event,
)
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.diagnostics import (
    CandidateCounts,
    best_effort_diagnostics,
    diagnostic_fields_from_failure,
    safe_diagnostic_fields,
)
from agent_search_gateway.providers.http import (
    HttpJsonExecutor,
    HttpStatusFailure,
    ProviderResponseFailure,
)
from agent_search_gateway.providers.web.doubao import DoubaoAdapter
from agent_search_gateway.providers.web.keenable import KeenableAdapter
from agent_search_gateway.providers.web.open_websearch import OpenWebSearchAdapter
from agent_search_gateway.providers.web.youcom import YouComAdapter
from agent_search_gateway.url_normalization import normalize_url
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
    assert "<redacted-url>" in rendered
    assert "\\u0000" not in rendered
    assert "\\u001b" not in rendered
    assert len(rendered.splitlines()) == 1
    assert "upstream_message_truncated=false" in rendered


@pytest.mark.parametrize("serialize", [json.dumps, repr], ids=["json", "python"])
@pytest.mark.parametrize("field", ["cookie", "access_token", "password", "authorization"])
def test_upstream_text_redacts_quoted_sensitive_fields(
    serialize: Callable[[dict[str, str]], str], field: str
) -> None:
    logger, stream = structured_test_logger("test.provider_diagnostics.quoted_fields")
    credential = "PRIVATE_PART_A'\";PRIVATE_PART_B"
    fields = safe_diagnostic_fields(
        {"upstream_message": f"request rejected: {serialize({field: credential})}"}
    )
    log_event(logger, logging.DEBUG, "provider_response_diagnostic", exc_info=False, **fields)

    rendered = stream.getvalue()
    assert "PRIVATE_PART_A" not in rendered
    assert "PRIVATE_PART_B" not in rendered
    assert "request rejected:" in rendered
    assert "<redacted>" in rendered
    assert len(rendered.splitlines()) == 1


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


@pytest.mark.parametrize("field", ["upstream_status", "upstream_type"])
def test_selected_upstream_status_is_rendered_with_its_safe_text_limit(field: str) -> None:
    logger, stream = structured_test_logger("test.provider_diagnostics.status")
    fields = safe_diagnostic_fields({field: "ERROR", "upstream_code": -32000})
    failure = ExecutionFailure(ErrorCode.PROTOCOL_ERROR, "public", diagnostics=fields)
    log_event(
        logger,
        logging.DEBUG,
        "provider_response_diagnostic",
        exc_info=False,
        **diagnostic_fields_from_failure(failure),
    )
    assert f"{field}=ERROR" in stream.getvalue()
    assert "upstream_code=-32000" in stream.getvalue()


def test_upstream_type_is_sanitized_bounded_and_ignores_nested_values() -> None:
    logger, stream = structured_test_logger("test.provider_diagnostics.type")
    fields = safe_diagnostic_fields(
        {"upstream_type": "https://private@example.test/type?token=PRIVATE " + "t" * 140}
    )
    assert isinstance(fields["upstream_type"], DiagnosticText)
    assert repr(fields["upstream_type"]) == "<DiagnosticText>"
    log_event(logger, logging.DEBUG, "provider_response_diagnostic", exc_info=False, **fields)
    rendered = stream.getvalue()
    assert "private" not in rendered and "PRIVATE" not in rendered
    assert "<redacted-url>" in rendered
    assert "t" * 128 not in rendered and "…" in rendered
    assert safe_diagnostic_fields({"upstream_type": {"message": "PRIVATE"}}) == {}


class DangerousValue:
    def __str__(self) -> str:
        raise AssertionError("untrusted str called")

    def __repr__(self) -> str:
        raise AssertionError("untrusted repr called")


def test_field_selection_omits_unknown_nested_values_and_owner_fields() -> None:
    dangerous = DangerousValue()
    fields = safe_diagnostic_fields(
        {
            "provider": dangerous,
            "stage": dangerous,
            "model": dangerous,
            "attempt": dangerous,
            "event": dangerous,
            "reason": dangerous,
            "error_type": dangerous,
            "elapsed_ms": dangerous,
            "exc_info": dangerous,
            "logger": dangerous,
            "level": dangerous,
            "body": dangerous,
            "prompt": ["PRIVATE_CONTENT"],
            "response": {"message": "PRIVATE_CONTENT"},
            "stdout": dangerous,
            "stderr": dangerous,
            "unknown": dangerous,
            "upstream_code": dangerous,
            "upstream_status": {"code": dangerous},
            "upstream_message": [dangerous],
            "received": True,
            "response_bytes": -1,
            "category": "UNRECOGNIZED",
            "http_status": "200",
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
        {"unknown_reason": 1},
        {"missing_url": -1},
        {"missing_url": True},
        {"missing_url": 1.5},
        {"missing_url": DangerousValue()},
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
            "upstream_message": "界",
            "upstream_message_chars": 999,
            "upstream_message_bytes": 999,
            "upstream_message_truncated": True,
            "rejection_counts": counts,
            "response_bytes": 42,
            "http_status": 200,
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
        ErrorCode.PROTOCOL_ERROR,
        "public",
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
        "received": 0,
        "accepted": 0,
        "rejected": 0,
        "rejection_counts": {},
        "rewritten": 0,
        "processed": 0,
        "unprocessed": 0,
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
        "received": 4,
        "accepted": 1,
        "rejected": 1,
        "rejection_counts": {"missing_url": 1},
        "rewritten": 1,
        "processed": 2,
        "unprocessed": 2,
    }
    snapshot = counts.as_fields()
    counts.accept()
    assert snapshot["accepted"] == 1
    assert rejected.accepted == 0
    assert empty.rejection_counts == {}


def test_candidate_counts_record_rewrite_before_rejection_or_unfinished_work() -> None:
    counts = CandidateCounts(2)
    counts.record_rewrite()
    counts.reject("empty_abstract")
    counts.record_rewrite()
    assert counts.as_fields() == {
        "received": 2,
        "accepted": 0,
        "rejected": 1,
        "rejection_counts": {"empty_abstract": 1},
        "rewritten": 2,
        "processed": 1,
        "unprocessed": 1,
    }
    counts.reject("judge_execution_failed")
    with pytest.raises(ValueError, match="already been processed"):
        counts.record_rewrite()


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
        log_event(
            logger,
            logging.DEBUG,
            "provider_response_diagnostic",
            exc_info=False,
            **best_effort_diagnostics(broken),
        )
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
        logger,
        logging.DEBUG,
        "provider_response_diagnostic",
        exc_info=False,
        **safe_diagnostic_fields({"upstream_code": 10**200}),
    )
    rendered = stream.getvalue()
    assert "upstream_code=1" + "0" * 126 + "…" in rendered
    assert "0" * 127 not in rendered


@pytest.mark.parametrize(
    "reason",
    [
        "duplicate",
        "missing_title",
        "missing_source_id",
        "invalid_identifier",
        "identifier_conflict",
        "invalid_date",
        "invalid_landing_url",
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


# These cases exercise each adapter through the real shared HTTP boundary.
_NEW_WEB_PROVIDERS = ("doubao", "youcom", "keenable", "open_websearch")
_NEW_WEB_FIXTURES = Path(__file__).parents[1] / "fixtures" / "providers"
_NEW_WEB_QUERY = "合成检索哨兵"
_NEW_WEB_BODY = "synthetic body sentinel"
_NEW_WEB_CREDENTIAL = "synthetic credential sentinel"
_NEW_WEB_URL = "https://example.com/page?marker=synthetic_url_sentinel"
_NEW_WEB_MARKERS = (
    _NEW_WEB_QUERY,
    _NEW_WEB_BODY,
    _NEW_WEB_CREDENTIAL,
    "synthetic_url_sentinel",
    "synthetic_error_sentinel",
    "synthetic_message_sentinel",
    "synthetic_detail_sentinel",
    "synthetic_hint_sentinel",
    "synthetic_fix_sentinel",
    "synthetic_code_sentinel",
    "synthetic_engine_sentinel",
)
NewWebAdapter = DoubaoAdapter | YouComAdapter | KeenableAdapter | OpenWebSearchAdapter


def _new_web_adapter(provider: str, executor: HttpJsonExecutor) -> NewWebAdapter:
    if provider == "open_websearch":
        return OpenWebSearchAdapter(
            name=provider,
            api_url="https://provider.example.com",
            http_executor=executor,
            engines=["bing", "duckduckgo", "bing"],
        )
    factories: dict[str, type[DoubaoAdapter] | type[YouComAdapter] | type[KeenableAdapter]] = {
        "doubao": DoubaoAdapter,
        "youcom": YouComAdapter,
        "keenable": KeenableAdapter,
    }
    return factories[provider](
        name=provider,
        api_url="https://provider.example.com",
        secret=SecretValue(_NEW_WEB_CREDENTIAL),
        http_executor=executor,
    )


@asynccontextmanager
async def _new_web_http(
    caplog: pytest.LogCaptureFixture,
    provider: str,
    respond: Callable[[httpx.Request, int], httpx.Response],
) -> AsyncIterator[tuple[NewWebAdapter, list[httpx.Request], list[float]]]:
    caplog.set_level(logging.DEBUG, logger="agent_search_gateway")
    requests: list[httpx.Request] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return respond(request, len(requests))

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(3, 0.01, 0.02, 5), provider_name=provider, sleep=sleep
        )
        yield _new_web_adapter(provider, executor), requests, delays


async def _new_web_call(
    adapter: NewWebAdapter, stage: str
) -> list[KeywordSearchHit] | URLFetchCandidate:
    if stage == "search":
        return await adapter.search(_NEW_WEB_QUERY)
    return await adapter.fetch(normalize_url(_NEW_WEB_URL))


def _new_web_search_payload(provider: str, records: list[object]) -> object:
    if provider == "doubao":
        return {"Result": {"WebResults": records}}
    if provider == "youcom":
        return {"results": {"web": records}}
    if provider == "keenable":
        return {"results": records}
    return {"status": "ok", "data": {"results": records}}


def _new_web_record(provider: str) -> dict[str, object]:
    if provider == "doubao":
        return {"Url": _NEW_WEB_URL, "Title": _NEW_WEB_BODY, "Summary": _NEW_WEB_BODY}
    return {
        "url": _NEW_WEB_URL,
        "title": _NEW_WEB_BODY,
        "snippet": _NEW_WEB_BODY,
        "snippets": [_NEW_WEB_BODY],
        "description": _NEW_WEB_BODY,
    }


def _new_web_success(provider: str, stage: str, *, body: str = _NEW_WEB_BODY) -> object:
    if stage == "search":
        return _new_web_search_payload(provider, [_new_web_record(provider)])
    if provider == "doubao":
        return {
            "Result": {"Data": {"StatusCode": 200, "ContentText": body, "Title": _NEW_WEB_BODY}}
        }
    if provider == "youcom":
        return [{"url": _NEW_WEB_URL, "markdown": body, "title": _NEW_WEB_BODY}]
    if provider == "keenable":
        return {"url": _NEW_WEB_URL, "content": body, "title": _NEW_WEB_BODY}
    return {"status": "ok", "data": {"content": body, "title": _NEW_WEB_BODY}}


def _new_web_error(provider: str) -> dict[str, object]:
    fields: dict[str, object] = {
        "error": "synthetic_error_sentinel",
        "message": " ".join(_NEW_WEB_MARKERS),
        "detail": "synthetic_detail_sentinel",
        "hint": "synthetic_hint_sentinel",
        "fix": "synthetic_fix_sentinel",
        "code": "synthetic_code_sentinel",
        "retryable": True,
    }
    if provider == "doubao":
        return {"ResponseMetadata": {"Error": {"Code": "synthetic_code_sentinel", **fields}}}
    if provider in {"youcom", "open_websearch"}:
        return {
            "status": "error",
            "error": fields,
            **{k: v for k, v in fields.items() if k != "error"},
        }
    return fields


def _new_web_events(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, object]]:
    return [
        cast(dict[str, object], vars(record)["gateway_fields"])
        for record in caplog.records
        if getattr(record, "gateway_event", None) == event
    ]


def _assert_new_web_safe(
    caplog: pytest.LogCaptureFixture, failure: ExecutionFailure | None = None
) -> str:
    formatter = KeyValueFormatter(SecretRedactor())
    rendered = "\n".join(
        formatter.format(record)
        for record in caplog.records
        if getattr(record, "gateway_event", None)
    )
    for marker in _NEW_WEB_MARKERS:
        assert marker not in rendered
        if failure is not None:
            assert marker not in failure.message
    return rendered


def _assert_new_web_attempts(
    caplog: pytest.LogCaptureFixture,
    provider: str,
    stage: str,
    *,
    attempts: int,
    retries: int,
    terminal_category: str | None,
) -> None:
    started = _new_web_events(caplog, "http_attempt_started")
    retrying = _new_web_events(caplog, "http_retrying")
    failed = _new_web_events(caplog, "http_failed")
    assert [event["attempt"] for event in started] == list(range(1, attempts + 1))
    assert [event["attempt"] for event in retrying] == list(range(1, retries + 1))
    assert len(failed) == int(terminal_category is not None)
    if terminal_category is not None:
        assert failed[0]["category"] == terminal_category
        assert failed[0]["attempt"] == attempts
    for event in (
        *started,
        *retrying,
        *failed,
        *_new_web_events(caplog, "provider_response_diagnostic"),
    ):
        assert event["provider"] == provider
        assert event["stage"] == stage


@pytest.mark.parametrize("provider", _NEW_WEB_PROVIDERS)
@pytest.mark.parametrize("stage", ["search", "fetch"])
@pytest.mark.parametrize("status", [401, 402, 403, 404, 422])
async def test_new_web_terminal_http_status_is_single_and_safe(
    caplog: pytest.LogCaptureFixture, provider: str, stage: str, status: int
) -> None:
    async with _new_web_http(
        caplog,
        provider,
        lambda _request, _attempt: httpx.Response(status, json=_new_web_error(provider)),
    ) as (adapter, requests, delays):
        with pytest.raises(HttpStatusFailure) as caught:
            await _new_web_call(adapter, stage)
    assert len(requests) == 1 and delays == []
    assert caught.value.status_code == status
    assert caught.value.diagnostics["http_status"] == status
    _assert_new_web_attempts(
        caplog, provider, stage, attempts=1, retries=0, terminal_category="status"
    )
    assert _new_web_events(caplog, "http_failed")[0]["http_status"] == status
    _assert_new_web_safe(caplog, caught.value)


@pytest.mark.parametrize("provider", _NEW_WEB_PROVIDERS)
@pytest.mark.parametrize("stage", ["search", "fetch"])
@pytest.mark.parametrize("status", [408, 429, 500])
async def test_new_web_retryable_http_status_recovers_with_shared_budget(
    caplog: pytest.LogCaptureFixture, provider: str, stage: str, status: int
) -> None:
    def respond(_request: httpx.Request, attempt: int) -> httpx.Response:
        if attempt == 1:
            return httpx.Response(status, json=_new_web_error(provider))
        return httpx.Response(200, json=_new_web_success(provider, stage))

    async with _new_web_http(caplog, provider, respond) as (adapter, requests, delays):
        result = await _new_web_call(adapter, stage)
    assert len(requests) == 2 and delays == [0.01]
    assert result
    _assert_new_web_attempts(caplog, provider, stage, attempts=2, retries=1, terminal_category=None)
    assert [
        event["http_status"] for event in _new_web_events(caplog, "provider_response_diagnostic")
    ] == [status, 200]
    assert _new_web_events(caplog, "http_retrying")[0]["category"] == "status"
    if provider == "open_websearch" and stage == "search":
        assert [json.loads(request.content) for request in requests] == [
            {"query": _NEW_WEB_QUERY, "engines": ["bing", "duckduckgo", "bing"], "limit": 50}
        ] * 2
    _assert_new_web_safe(caplog)


@pytest.mark.parametrize("provider", _NEW_WEB_PROVIDERS)
@pytest.mark.parametrize("stage", ["search", "fetch"])
@pytest.mark.parametrize("failure_kind", ["429", "500", "timeout", "connect"])
async def test_new_web_retries_stop_at_shared_attempt_budget(
    caplog: pytest.LogCaptureFixture, provider: str, stage: str, failure_kind: str
) -> None:
    def respond(request: httpx.Request, _attempt: int) -> httpx.Response:
        if failure_kind == "timeout":
            raise httpx.ReadTimeout(" ".join(_NEW_WEB_MARKERS), request=request)
        if failure_kind == "connect":
            raise httpx.ConnectError(" ".join(_NEW_WEB_MARKERS), request=request)
        return httpx.Response(int(failure_kind), json=_new_web_error(provider))

    async with _new_web_http(caplog, provider, respond) as (adapter, requests, delays):
        with pytest.raises(ExecutionFailure) as caught:
            await _new_web_call(adapter, stage)
    assert len(requests) == 3 and delays == [0.01, 0.02]
    category = "status" if failure_kind in {"429", "500"} else "transport"
    _assert_new_web_attempts(
        caplog, provider, stage, attempts=3, retries=2, terminal_category=category
    )
    assert all(event["category"] == category for event in _new_web_events(caplog, "http_retrying"))
    if category == "status":
        assert isinstance(caught.value, HttpStatusFailure)
        assert caught.value.diagnostics["http_status"] == int(failure_kind)
    else:
        assert _new_web_events(caplog, "provider_response_diagnostic") == []
    _assert_new_web_safe(caplog, caught.value)


@pytest.mark.parametrize(
    "stage,code",
    [
        ("search", "700429"),
        ("search", "10500"),
        ("fetch", "700429"),
        ("fetch", "10500"),
        ("fetch", "10502"),
    ],
)
@pytest.mark.parametrize("recovers", [True, False], ids=["recovers", "exhausts"])
async def test_new_web_doubao_business_retry_shares_http_attempt_budget(
    caplog: pytest.LogCaptureFixture, stage: str, code: str, recovers: bool
) -> None:
    def respond(_request: httpx.Request, attempt: int) -> httpx.Response:
        # A transport-level status and a 200 business failure consume the same budget.
        if attempt == 1:
            return httpx.Response(429, json=_new_web_error("doubao"))
        if recovers and attempt == 3:
            return httpx.Response(200, json=_new_web_success("doubao", stage))
        return httpx.Response(
            200,
            json={
                "ResponseMetadata": {
                    "Error": {
                        "Code": code,
                        "CodeN": int(code),
                        "Message": " ".join(_NEW_WEB_MARKERS),
                    }
                }
            },
        )

    failure: ExecutionFailure | None = None
    async with _new_web_http(caplog, "doubao", respond) as (adapter, requests, delays):
        if recovers:
            assert await _new_web_call(adapter, stage)
        else:
            with pytest.raises(ProviderResponseFailure) as caught:
                await _new_web_call(adapter, stage)
            failure = caught.value
            assert caught.value.retryable is True
            assert caught.value.diagnostics["semantic_result"] == "error"
    assert len(requests) == 3 and len(delays) == 2
    assert delays[0] == 0.01
    assert 0.016 <= delays[1] <= 0.02
    _assert_new_web_attempts(
        caplog,
        "doubao",
        stage,
        attempts=3,
        retries=2,
        terminal_category=None if recovers else "provider_header",
    )
    assert [event["category"] for event in _new_web_events(caplog, "http_retrying")] == [
        "status",
        "provider_header",
    ]
    diagnostics = _new_web_events(caplog, "provider_response_diagnostic")
    assert len(diagnostics) == 3
    assert diagnostics[1]["category"] == "provider_response"
    assert diagnostics[1]["semantic_result"] == "error"
    _assert_new_web_safe(caplog, failure)


@pytest.mark.parametrize("provider", _NEW_WEB_PROVIDERS)
@pytest.mark.parametrize("stage", ["search", "fetch"])
async def test_new_web_200_business_error_is_terminal_and_drops_untrusted_prose(
    caplog: pytest.LogCaptureFixture, provider: str, stage: str
) -> None:
    async with _new_web_http(
        caplog,
        provider,
        lambda _request, _attempt: httpx.Response(200, json=_new_web_error(provider)),
    ) as (adapter, requests, delays):
        with pytest.raises(ProviderResponseFailure) as caught:
            await _new_web_call(adapter, stage)
    assert len(requests) == 1 and delays == []
    assert caught.value.retryable is False
    assert caught.value.diagnostics["semantic_result"] == "error"
    assert caught.value.diagnostics["http_status"] == 200
    _assert_new_web_attempts(
        caplog, provider, stage, attempts=1, retries=0, terminal_category="provider_header"
    )
    diagnostics = _new_web_events(caplog, "provider_response_diagnostic")
    assert len(diagnostics) == 1
    assert diagnostics[0]["category"] == "provider_response"
    assert diagnostics[0]["semantic_result"] == "error"
    assert "upstream_message" not in diagnostics[0]
    rendered = _assert_new_web_safe(caplog, caught.value)
    assert "upstream_code=unknown" in rendered


@pytest.mark.parametrize("provider", _NEW_WEB_PROVIDERS)
@pytest.mark.parametrize("stage", ["search", "fetch"])
@pytest.mark.parametrize("body_kind", ["fixture", "empty"])
async def test_new_web_non_json_reaches_shared_decode_failure_without_retry(
    caplog: pytest.LogCaptureFixture, provider: str, stage: str, body_kind: str
) -> None:
    body = (
        (_NEW_WEB_FIXTURES / provider / "invalid_json.txt").read_bytes()
        if body_kind == "fixture"
        else b""
    )
    async with _new_web_http(
        caplog, provider, lambda _request, _attempt: httpx.Response(200, content=body)
    ) as (adapter, requests, delays):
        with pytest.raises(ProtocolFailure) as caught:
            await _new_web_call(adapter, stage)
    assert len(requests) == 1 and delays == []
    assert isinstance(caught.value.__cause__, json.JSONDecodeError)
    _assert_new_web_attempts(
        caplog, provider, stage, attempts=1, retries=0, terminal_category="decode"
    )
    diagnostics = _new_web_events(caplog, "provider_response_diagnostic")
    assert len(diagnostics) == 1
    assert diagnostics[0]["category"] == "decode"
    assert diagnostics[0]["http_status"] == 200
    rendered = _assert_new_web_safe(caplog, caught.value)
    if body.strip():
        assert body.decode().strip() not in rendered


@pytest.mark.parametrize("provider", _NEW_WEB_PROVIDERS)
@pytest.mark.parametrize("stage", ["search", "fetch"])
async def test_new_web_invalid_schema_has_diagnostic_without_http_failure_or_retry(
    caplog: pytest.LogCaptureFixture, provider: str, stage: str
) -> None:
    payload = {"unexpected": " ".join(_NEW_WEB_MARKERS)}
    async with _new_web_http(
        caplog, provider, lambda _request, _attempt: httpx.Response(200, json=payload)
    ) as (adapter, requests, delays):
        with pytest.raises(ExecutionFailure) as caught:
            await _new_web_call(adapter, stage)
    assert len(requests) == 1 and delays == []
    assert not isinstance(caught.value, ProviderResponseFailure)
    _assert_new_web_attempts(caplog, provider, stage, attempts=1, retries=0, terminal_category=None)
    diagnostics = _new_web_events(caplog, "provider_response_diagnostic")
    assert len(diagnostics) == 1
    assert diagnostics[0]["category"] == "invalid_envelope"
    _assert_new_web_safe(caplog, caught.value)


@pytest.mark.parametrize("provider", _NEW_WEB_PROVIDERS)
async def test_new_web_empty_fetch_does_not_promote_metadata_to_content(
    caplog: pytest.LogCaptureFixture, provider: str
) -> None:
    payload = _new_web_success(provider, "fetch", body=" \t\n")
    async with _new_web_http(
        caplog, provider, lambda _request, _attempt: httpx.Response(200, json=payload)
    ) as (adapter, requests, delays):
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.fetch(normalize_url(_NEW_WEB_URL))
    assert len(requests) == 1 and delays == []
    assert caught.value.diagnostics["semantic_result"] == "empty"
    _assert_new_web_attempts(
        caplog, provider, "fetch", attempts=1, retries=0, terminal_category=None
    )
    diagnostics = _new_web_events(caplog, "provider_response_diagnostic")
    assert len(diagnostics) == 1
    assert diagnostics[0]["semantic_result"] == "empty"
    _assert_new_web_safe(caplog, caught.value)


@pytest.mark.parametrize("provider", _NEW_WEB_PROVIDERS)
@pytest.mark.parametrize(
    "case,received,accepted,reasons",
    [
        ("mixed", 2, 1, {"missing_url": 1}),
        ("invalid", 2, 0, {"missing_url": 1, "invalid_record": 1}),
        ("empty", 0, 0, {}),
    ],
)
async def test_new_web_search_counts_partition_only_response_candidates(
    caplog: pytest.LogCaptureFixture,
    provider: str,
    case: str,
    received: int,
    accepted: int,
    reasons: dict[str, int],
) -> None:
    records: list[object] = (
        [_new_web_record(provider), {}]
        if case == "mixed"
        else [{}, 42]
        if case == "invalid"
        else []
    )
    async with _new_web_http(
        caplog,
        provider,
        lambda _request, _attempt: httpx.Response(
            200, json=_new_web_search_payload(provider, records)
        ),
    ) as (adapter, requests, delays):
        hits = await adapter.search(_NEW_WEB_QUERY)
    assert hits == (
        [KeywordSearchHit(_NEW_WEB_URL, _NEW_WEB_BODY, _NEW_WEB_BODY)] if accepted else []
    )
    assert len(requests) == 1 and delays == []
    assert _new_web_events(caplog, "provider_candidates_summary") == [
        {
            "provider": provider,
            "stage": "search",
            "phase": "adapter",
            "received": received,
            "accepted": accepted,
            "rejected": received - accepted,
            "rejection_counts": reasons,
            "processed": received,
            "unprocessed": 0,
            "rewritten": 0,
        }
    ]
    diagnostics = _new_web_events(caplog, "provider_response_diagnostic")
    assert len(diagnostics) == 1
    assert diagnostics[0]["semantic_result"] == "success"
    _assert_new_web_attempts(
        caplog, provider, "search", attempts=1, retries=0, terminal_category=None
    )
    _assert_new_web_safe(caplog)


@pytest.mark.parametrize("provider", _NEW_WEB_PROVIDERS)
@pytest.mark.parametrize("stage", ["search", "fetch"])
async def test_new_web_cancellation_keeps_identity_and_never_retries(
    caplog: pytest.LogCaptureFixture, provider: str, stage: str
) -> None:
    cancellation = asyncio.CancelledError("synthetic cancellation")

    def respond(_request: httpx.Request, _attempt: int) -> httpx.Response:
        raise cancellation

    async with _new_web_http(caplog, provider, respond) as (adapter, requests, delays):
        with pytest.raises(asyncio.CancelledError) as caught:
            await _new_web_call(adapter, stage)
    assert caught.value is cancellation
    assert len(requests) == 1 and delays == []
    _assert_new_web_attempts(caplog, provider, stage, attempts=1, retries=0, terminal_category=None)
    assert _new_web_events(caplog, "provider_response_diagnostic") == []


@pytest.mark.parametrize("has_results", [True, False], ids=["valid-results", "empty-results"])
async def test_new_web_partial_engine_failures_preserve_results_and_safe_local_counts(
    caplog: pytest.LogCaptureFixture, has_results: bool
) -> None:
    provider = "open_websearch"
    records: list[object] = [_new_web_record(provider)] if has_results else []
    payload = {
        "status": "ok",
        "data": {
            "results": records,
            "partialFailures": [
                {
                    "engine": "synthetic_engine_sentinel",
                    "code": "synthetic_code_sentinel",
                    "message": " ".join(_NEW_WEB_MARKERS),
                },
                {
                    "engine": "synthetic_engine_sentinel",
                    "error": _new_web_error(provider),
                },
            ],
        },
    }
    async with _new_web_http(
        caplog, provider, lambda _request, _attempt: httpx.Response(200, json=payload)
    ) as (adapter, requests, delays):
        hits = await adapter.search(_NEW_WEB_QUERY)
    assert hits == (
        [KeywordSearchHit(_NEW_WEB_URL, _NEW_WEB_BODY, _NEW_WEB_BODY)] if has_results else []
    )
    assert len(requests) == 1 and delays == []
    _assert_new_web_attempts(
        caplog, provider, "search", attempts=1, retries=0, terminal_category=None
    )
    diagnostics = _new_web_events(caplog, "provider_response_diagnostic")
    assert len(diagnostics) == 1
    assert diagnostics[0]["category"] == "provider_response"
    assert diagnostics[0]["semantic_result"] == "success"
    code, message = diagnostics[0]["upstream_code"], diagnostics[0]["upstream_message"]
    assert isinstance(code, DiagnosticText) and code.text == "partial_engine_failures"
    assert isinstance(message, DiagnosticText) and message.text == "partial_engine_failures count=2"
    assert _new_web_events(caplog, "provider_candidates_summary") == [
        {
            "provider": provider,
            "stage": "search",
            "phase": "adapter",
            "received": len(records),
            "accepted": len(records),
            "rejected": 0,
            "rejection_counts": {},
            "processed": len(records),
            "unprocessed": 0,
            "rewritten": 0,
        }
    ]
    _assert_new_web_safe(caplog)
