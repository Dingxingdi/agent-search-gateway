import logging
import random
import shlex
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.http import HttpJsonExecutor, ProviderResponseFailure
from agent_search_gateway.providers.web.brightdata import BrightDataAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingHttpExecutor
from tests.support.logging import structured_test_logger


def _events(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, object]]:
    return [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == event
    ]


async def test_brightdata_reports_successful_candidate_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"organic": [{"link": "https://example.com", "description": "PRIVATE_BODY"}]},
            )
        )
    ) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="brightdata")
        assert await _brightdata_adapter(executor).search("query") == [
            KeywordSearchHit("https://example.com", "", "PRIVATE_BODY")
        ]
    summaries = _events(caplog, "provider_candidates_summary")
    assert summaries == [
        {
            "provider": "brightdata",
            "stage": "search",
            "phase": "adapter",
            "received": 1,
            "accepted": 1,
            "rejected": 0,
            "rejection_counts": {},
            "processed": 1,
            "unprocessed": 0,
            "rewritten": 0,
        }
    ]
    diagnostics = _events(caplog, "provider_response_diagnostic")
    assert len(diagnostics) == 1
    assert diagnostics[0]["semantic_result"] == "success"
    assert diagnostics[0]["response_kind"] == "object"
    assert "PRIVATE_BODY" not in caplog.text


async def test_brightdata_reports_mixed_candidate_rejections(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    results = [
        {"link": "https://example.com"},
        {},
        {"link": None},
        {"link": "  "},
        None,
        {"link": 1},
        {"link": "https://example.com/bad", "description": []},
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"organic": results})
        )
    ) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="brightdata")
        assert await _brightdata_adapter(executor).search("query") == [
            KeywordSearchHit("https://example.com", "", "")
        ]
    summaries = _events(caplog, "provider_candidates_summary")
    assert len(summaries) == 1
    assert summaries[0]["received"] == 7
    assert summaries[0]["accepted"] == 1
    assert summaries[0]["rejected"] == 6
    assert summaries[0]["rejection_counts"] == {"missing_url": 3, "invalid_record": 3}
    assert summaries[0]["processed"] == 7
    assert summaries[0]["unprocessed"] == 0


async def test_brightdata_reports_all_invalid_candidates(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"organic": [None, {}, {"link": ""}]})
        )
    ) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="brightdata")
        assert await _brightdata_adapter(executor).search("query") == []
    summaries = _events(caplog, "provider_candidates_summary")
    assert len(summaries) == 1
    assert summaries[0]["received"] == summaries[0]["rejected"] == 3
    assert summaries[0]["accepted"] == summaries[0]["unprocessed"] == 0
    assert summaries[0]["rejection_counts"] == {"missing_url": 2, "invalid_record": 1}


async def test_brightdata_reports_empty_candidate_summary(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"organic": []}))
    ) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="brightdata")
        assert await _brightdata_adapter(executor).search("query") == []
    summaries = _events(caplog, "provider_candidates_summary")
    assert len(summaries) == 1
    assert summaries[0]["received"] == summaries[0]["accepted"] == summaries[0]["rejected"] == 0
    assert summaries[0]["rejection_counts"] == {}


async def test_brightdata_json_error_diagnostics_use_only_known_fields() -> None:
    logger, stream = structured_test_logger("tests.brightdata.json-error")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "error": {
                    "code": "quota_limit",
                    "message": "Vendor quota exhausted",
                    "detail": "PRIVATE_ERROR_DETAIL",
                },
                "organic": [{"message": "PRIVATE_CANDIDATE_BODY"}],
                "message": "PRIVATE_ROOT_MESSAGE",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(3, 0, 0, 5), provider_name="brightdata", logger=logger
        )
        with pytest.raises(ExecutionFailure) as caught:
            await _brightdata_adapter(executor).search("query")
    assert type(caught.value) is ExecutionFailure
    assert caught.value.message == "brightdata/search: provider reported failure"
    assert set(caught.value.diagnostics) == {"upstream_code", "upstream_message"}
    assert len(requests) == 1
    diagnostic_lines = [
        line
        for line in stream.getvalue().splitlines()
        if "event=provider_response_diagnostic " in line
    ]
    assert len(diagnostic_lines) == 1
    fields = dict(token.split("=", 1) for token in shlex.split(diagnostic_lines[0])[1:])
    assert fields["http_status"] == "200"
    assert fields["category"] == "provider_response"
    assert fields["semantic_result"] == "error"
    assert fields["upstream_code"] == "quota_limit"
    assert fields["upstream_message"] == "Vendor quota exhausted"
    assert "PRIVATE_" not in stream.getvalue() + str(caught.value)


async def test_brightdata_invalid_organic_reports_invalid_envelope(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"organic": {"message": "PRIVATE_CANDIDATE"}, "message": "PRIVATE_ROOT"}
            )
        )
    ) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="brightdata")
        with pytest.raises(ExecutionFailure, match="organic must be an array"):
            await _brightdata_adapter(executor).search("query")
    diagnostics = _events(caplog, "provider_response_diagnostic")
    assert len(diagnostics) == 1
    assert diagnostics[0]["category"] == "invalid_envelope"
    assert diagnostics[0]["semantic_result"] == "unknown"
    assert not any(key.startswith("upstream_") for key in diagnostics[0])
    assert not _events(caplog, "provider_candidates_summary")


async def test_brightdata_search_uses_serp_zone_and_maps_organic_results() -> None:
    executor = RecordingHttpExecutor(
        json_responses=[
            {
                "organic": [
                    {
                        "link": "https://example.com/a",
                        "title": "A",
                        "description": "Snippet A",
                        "rank": 1,
                    }
                ],
                "ignored": "metadata",
            }
        ]
    )
    adapter = BrightDataAdapter(
        name="brightdata",
        api_url="https://api.brightdata.test/",
        search_zone="serp-zone",
        fetch_zone="fetch-zone",
        secret=SecretValue("secret"),
        http_executor=executor,
    )

    assert await adapter.search("hello world & docs") == [
        KeywordSearchHit("https://example.com/a", "A", "Snippet A")
    ]

    request = executor.requests[0]
    assert request.method == "POST"
    assert request.url == "https://api.brightdata.test/request"
    assert request.stage == "search"
    assert request.headers == {"Authorization": "Bearer secret"}
    assert isinstance(request.json_body, dict)
    assert request.json_body["zone"] == "serp-zone"
    assert "fetch-zone" not in str(request.json_body)
    assert request.json_body["format"] == "raw"
    assert set(request.json_body) == {"zone", "url", "format"}
    google_url = str(request.json_body["url"])
    params = parse_qs(urlparse(google_url).query)
    assert urlparse(google_url)._replace(query="").geturl() == "https://www.google.com/search"
    assert params == {"q": ["hello world & docs"], "brd_json": ["1"]}


async def test_brightdata_fetch_uses_fetch_zone_and_returns_markdown_text() -> None:
    executor = RecordingHttpExecutor(text_responses=["# fetched\nbody"])
    adapter = BrightDataAdapter(
        name="brightdata",
        api_url="https://api.brightdata.test",
        search_zone="serp-zone",
        fetch_zone="fetch-zone",
        secret=SecretValue("secret"),
        http_executor=executor,
    )

    target = normalize_url("https://example.com/path?a=1&b=2")
    assert await adapter.fetch(target) == URLFetchCandidate("# fetched\nbody", "# fetched\nbody")

    request = executor.requests[0]
    assert request.method == "POST"
    assert request.url == "https://api.brightdata.test/request"
    assert request.stage == "fetch"
    assert request.headers == {"Authorization": "Bearer secret"}
    assert request.json_body == {
        "zone": "fetch-zone",
        "url": str(target),
        "format": "raw",
        "data_format": "markdown",
    }
    assert "serp-zone" not in str(request.json_body)


async def test_brightdata_search_and_fetch_keep_zones_independent() -> None:
    executor = RecordingHttpExecutor(
        json_responses=[{"organic": []}],
        text_responses=["body"],
    )
    adapter = BrightDataAdapter(
        name="brightdata",
        api_url="https://api.brightdata.test",
        search_zone="serp-zone",
        fetch_zone="fetch-zone",
        secret=SecretValue("secret"),
        http_executor=executor,
    )

    await adapter.search("query")
    await adapter.fetch(normalize_url("https://example.com"))

    search_request, fetch_request = executor.requests
    assert isinstance(search_request.json_body, dict)
    assert isinstance(fetch_request.json_body, dict)
    assert search_request.json_body["zone"] == "serp-zone"
    assert fetch_request.json_body["zone"] == "fetch-zone"


@pytest.mark.parametrize("field", ["api_url", "search_zone", "fetch_zone"])
@pytest.mark.parametrize("value", [None, "", "   ", 1])
def test_brightdata_requires_non_empty_configuration(field: str, value: object) -> None:
    values: dict[str, object] = {
        "api_url": "https://api.brightdata.test",
        "search_zone": "serp-zone",
        "fetch_zone": "fetch-zone",
    }
    values[field] = value
    with pytest.raises(TypeError):
        BrightDataAdapter(
            name="brightdata",
            api_url=values["api_url"],  # type: ignore[arg-type]
            search_zone=values["search_zone"],  # type: ignore[arg-type]
            fetch_zone=values["fetch_zone"],  # type: ignore[arg-type]
            secret=SecretValue("secret"),
            http_executor=RecordingHttpExecutor(),
        )


@pytest.mark.parametrize("body", ["", " \n\t"])
async def test_brightdata_fetch_rejects_empty_body(body: str) -> None:
    adapter = BrightDataAdapter(
        name="brightdata",
        api_url="https://api.brightdata.test",
        search_zone="serp-zone",
        fetch_zone="fetch-zone",
        secret=SecretValue("secret"),
        http_executor=RecordingHttpExecutor(text_responses=[body]),
    )
    with pytest.raises(ExecutionFailure, match="page body is empty"):
        await adapter.fetch(normalize_url("https://example.com"))


_CAPTCHA_HEADERS = {
    "x-brd-error-code": "captcha",
    "x-brd-error": "redirect location was rejected",
    "x-brd-status-code": "502",
}


@pytest.mark.parametrize(
    "explanation",
    [
        "unexpected prose",
        "x" * 161,
        "https://example.test/private?q=EXPLANATION_MARKER",
        "token=[REDACTED_SECRET]",
        "redirect location was rejected\nEXPLANATION_MARKER",
    ],
)
async def test_brightdata_untrusted_explanation_keeps_only_safe_reason(explanation: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={**_CAPTCHA_HEADERS, "x-brd-error": explanation},
            )
        )
    ) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(1, 0.25, 2.0, 30.0),
            provider_name="brightdata",
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await _brightdata_adapter(executor).search("query")
    assert caught.value.retryable is True
    assert caught.value.safe_reason == "provider_reported_failure"
    assert explanation not in str(caught.value)


@pytest.mark.parametrize("payload", [{"organic": {}}, {"error": "BODY_DETAIL", "organic": []}])
async def test_brightdata_payload_failures_do_not_gain_header_retries(
    payload: dict[str, object],
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="brightdata",
        )
        with pytest.raises(ExecutionFailure) as caught:
            await _brightdata_adapter(executor).search("query")
    assert type(caught.value) is ExecutionFailure
    assert len(requests) == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_brightdata_auth_status_without_headers_preserves_http_failure(status: int) -> None:
    from agent_search_gateway.providers.http import HttpStatusFailure

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="brightdata",
        )
        with pytest.raises(HttpStatusFailure) as caught:
            await _brightdata_adapter(executor).search("query")
    assert caught.value.status_code == status
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("factor", "cap", "expected"),
    [(0.9, 2.0, [0.225, 0.45]), (0.8, 0.3, [0.2, 0.24]), (1.0, 0.3, [0.25, 0.3])],
)
async def test_brightdata_header_retry_uses_adjusted_delay(
    monkeypatch: pytest.MonkeyPatch, factor: float, cap: float, expected: list[float]
) -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []
    draws: list[tuple[float, float]] = []

    def uniform(low: float, high: float) -> float:
        draws.append((low, high))
        return factor

    monkeypatch.setattr(random, "uniform", uniform)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) < 3:
            return httpx.Response(200, headers=_CAPTCHA_HEADERS)
        return httpx.Response(200, json={"organic": []})

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, cap, 30.0),
            provider_name="brightdata",
            sleep=sleep,
        )
        assert await _brightdata_adapter(executor).search("query") == []

    assert len(requests) == 3
    assert sleeps == pytest.approx(expected)
    assert draws == [(0.8, 1.0), (0.8, 1.0)]


async def test_brightdata_mixed_failures_keep_one_budget_and_only_header_jitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []
    draws: list[tuple[float, float]] = []

    def uniform(low: float, high: float) -> float:
        draws.append((low, high))
        return 0.9

    monkeypatch.setattr(random, "uniform", uniform)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 2:
            return httpx.Response(500)
        return httpx.Response(200, headers=_CAPTCHA_HEADERS)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="brightdata",
            sleep=sleep,
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await _brightdata_adapter(executor).search("query")

    assert len(requests) == 3
    assert sleeps == pytest.approx([0.225, 0.5])
    assert draws == [(0.8, 1.0)]
    assert caught.value.diagnostics["brightdata_error_code"] == "captcha"
    assert caught.value.diagnostics["brightdata_status"] == 502


@pytest.mark.parametrize("kind", ["status", "transport"])
async def test_brightdata_generic_failures_preserve_exact_backoff(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []
    draws: list[tuple[float, float]] = []

    def uniform(low: float, high: float) -> float:
        draws.append((low, high))
        return 0.9

    monkeypatch.setattr(random, "uniform", uniform)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) < 3:
            if kind == "transport":
                raise httpx.ReadTimeout("fixture timeout", request=request)
            return httpx.Response(500)
        return httpx.Response(200, json={"organic": []})

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="brightdata",
            sleep=sleep,
        )
        assert await _brightdata_adapter(executor).search("query") == []

    assert len(requests) == 3
    assert sleeps == [0.25, 0.5]
    assert draws == []


@pytest.mark.parametrize(
    "outcome",
    ["recovery", "exhausted", "terminal", "invalid-code", "one-attempt"],
)
async def test_brightdata_header_events_expose_safe_metadata(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []
    draws: list[tuple[float, float]] = []
    credential = SecretValue("credential-value")
    logger, stream = structured_test_logger("tests.brightdata.safe-events", secrets=[credential])
    code = "captcha"
    safe_code = "captcha"
    if outcome == "terminal":
        code, safe_code = "other_code", "unknown"
    elif outcome == "invalid-code":
        code, safe_code = "CODE_MARKER" + "x" * 200, "invalid"
    explanation = (
        "https://example.test/TARGET_PATH_MARKER?q=HEADER_QUERY_MARKER "
        "token=[REDACTED_SECRET]\r\nHEADER_CONTROL_MARKER"
    )

    def uniform(low: float, high: float) -> float:
        draws.append((low, high))
        return 0.9

    monkeypatch.setattr(random, "uniform", uniform)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if outcome == "recovery" and len(requests) == 3:
            return httpx.Response(
                200,
                json={
                    "organic": [
                        {
                            "link": "https://example.test/RESULT_PATH_MARKER",
                            "description": "BODY_MARKER",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            text="BODY_MARKER",
            headers={
                "x-brd-error-code": code,
                "x-brd-status-code": "502",
                "x-brd-error": explanation,
            },
        )

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    message = ""
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(1 if outcome == "one-attempt" else 3, 0.25, 2.0, 30.0),
            provider_name="brightdata",
            logger=logger,
            sleep=sleep,
        )
        adapter = _brightdata_adapter(executor, secret=credential)
        query = "QUERY_MARKER https://example.test/QUERY_PATH_MARKER"
        if outcome == "recovery":
            assert len(await adapter.search(query)) == 1
        else:
            with pytest.raises(ProviderResponseFailure) as caught:
                await adapter.search(query)
            message = str(caught.value)

    expected_attempts = 3 if outcome in {"recovery", "exhausted"} else 1
    assert len(requests) == expected_attempts
    assert len(draws) == len(sleeps)
    logged = stream.getvalue()
    for marker in (
        "QUERY_MARKER",
        "QUERY_PATH_MARKER",
        "TARGET_PATH_MARKER",
        "HEADER_QUERY_MARKER",
        "HEADER_CONTROL_MARKER",
        "RESULT_PATH_MARKER",
        "BODY_MARKER",
        "CODE_MARKER",
        credential.reveal(),
        "[REDACTED_SECRET]",
        "traceback=",
        "Traceback",
    ):
        assert marker not in logged + message

    lines = logged.splitlines()
    retry_lines = [line for line in lines if "event=http_retrying" in line]
    failed_lines = [line for line in lines if "event=http_failed" in line]
    assert len(retry_lines) == len(sleeps) == (2 if expected_attempts == 3 else 0)
    assert len(failed_lines) == (0 if outcome == "recovery" else 1)
    for line in [*retry_lines, *failed_lines]:
        fields = dict(token.split("=", 1) for token in shlex.split(line)[1:])
        assert line.startswith("DEBUG ")
        assert fields["provider"] == "brightdata"
        assert fields["stage"] == "search"
        assert fields["category"] == "provider_header"
        assert fields["status"] == "200"
        assert fields["brightdata_status"] == "502"
        assert fields["brightdata_error_code"] == safe_code
        assert fields["reason"] == "provider_reported_failure"
        assert fields["retryable"] == (
            "false" if outcome in {"terminal", "invalid-code"} else "true"
        )
        assert int(fields["elapsed_ms"]) >= 0
        assert "transport_type" not in fields
    for attempt, (line, delay) in enumerate(zip(retry_lines, sleeps, strict=True), start=1):
        fields = dict(token.split("=", 1) for token in shlex.split(line)[1:])
        assert fields["attempt"] == str(attempt)
        assert fields["retry_scheduled"] == "true"
        assert fields["delay_ms"] == str(max(0, int(delay * 1000)))
    if failed_lines:
        fields = dict(token.split("=", 1) for token in shlex.split(failed_lines[0])[1:])
        assert fields["attempt"] == str(expected_attempts)
        assert fields["retry_scheduled"] == "false"
        assert fields["terminal_reason"] == (
            "non_retryable" if outcome in {"terminal", "invalid-code"} else "exhausted"
        )


def _brightdata_adapter(
    executor: HttpJsonExecutor, *, secret: SecretValue | None = None
) -> BrightDataAdapter:
    return BrightDataAdapter(
        name="brightdata",
        api_url="https://api.brightdata.test",
        search_zone="serp-zone",
        fetch_zone="fetch-zone",
        secret=secret if secret is not None else SecretValue("test-credential"),
        http_executor=executor,
    )


async def test_brightdata_unknown_header_code_remains_terminal_and_is_redacted() -> None:
    secret = SecretValue("UNKNOWN_CODE_SECRET")
    logger, stream = structured_test_logger("tests.brightdata.unknown-code", secrets=[secret])
    message = (
        "Vendor rejected "
        + secret.reveal()
        + " https://user:password@example.test/path?q=PRIVATE_QUERY#PRIVATE_FRAGMENT"
        + " token=PRIVATE_TOKEN"
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={
                "x-brd-error-code": secret.reveal(),
                "x-brd-error": message,
            },
        )

    async def no_retry(_delay: float) -> None:
        pytest.fail("Unknown header code must retain its terminal policy")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0, 0, 5),
            provider_name="brightdata",
            logger=logger,
            sleep=no_retry,
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await _brightdata_adapter(executor, secret=secret).search("query")
    assert len(requests) == 1
    assert caught.value.retryable is False
    assert caught.value.diagnostics["brightdata_error_code"] == "unknown"
    logged = stream.getvalue() + str(caught.value) + repr(caught.value.diagnostics)
    for marker in (
        secret.reveal(),
        "PRIVATE_QUERY",
        "PRIVATE_FRAGMENT",
        "PRIVATE_TOKEN",
        "password",
    ):
        assert marker not in logged
    diagnostic_lines = [
        line
        for line in stream.getvalue().splitlines()
        if "event=provider_response_diagnostic " in line
    ]
    assert len(diagnostic_lines) == 1
    fields = dict(token.split("=", 1) for token in shlex.split(diagnostic_lines[0])[1:])
    assert "upstream_code" in fields
    assert fields["upstream_message"].startswith("Vendor rejected")
    assert fields["brightdata_error_code"] == "unknown"


async def test_brightdata_duplicate_headers_keep_classification_without_raw_metadata() -> None:
    logger, stream = structured_test_logger("tests.brightdata.duplicate-headers")
    headers = [
        ("x-brd-error-code", "PRIVATE_CODE_A"),
        ("x-brd-error-code", "PRIVATE_CODE_B"),
        ("x-brd-status-code", "200"),
        ("x-brd-status-code", "502"),
        ("x-brd-error", "PRIVATE_MESSAGE_A"),
        ("x-brd-error", "PRIVATE_MESSAGE_B"),
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, headers=headers))
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(3, 0, 0, 5), provider_name="brightdata", logger=logger
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await _brightdata_adapter(executor).search("query")
    assert caught.value.retryable is False
    assert caught.value.diagnostics["brightdata_error_code"] == "invalid"
    assert not any(key.startswith("upstream_") for key in caught.value.diagnostics)
    assert "brightdata_status" not in caught.value.diagnostics
    assert stream.getvalue().count("event=provider_response_diagnostic ") == 1
    assert "PRIVATE_" not in stream.getvalue() + str(caught.value)
    assert "category=provider_response" in stream.getvalue()


async def test_brightdata_status_only_responses_preserve_inner_status_with_http_retries() -> None:
    logger, stream = structured_test_logger("tests.brightdata.status-only-diagnostics")
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, headers={"x-brd-status-code": "502"})
        return httpx.Response(200, headers={"x-brd-status-code": "200"}, json={"organic": []})

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2, 5),
            provider_name="brightdata",
            logger=logger,
            sleep=sleep,
        )
        assert await _brightdata_adapter(executor).search("query") == []
    diagnostics = [
        dict(token.split("=", 1) for token in shlex.split(line)[1:])
        for line in stream.getvalue().splitlines()
        if "event=provider_response_diagnostic " in line
    ]
    assert len(diagnostics) == len(requests) == 2
    assert diagnostics[0]["http_status"] == "503"
    assert diagnostics[0]["upstream_status"] == "502"
    assert diagnostics[1]["http_status"] == diagnostics[1]["upstream_status"] == "200"
    assert diagnostics[1]["semantic_result"] == "success"
    assert sleeps == [0.25]


@pytest.mark.parametrize(
    "message",
    [
        "PRIVATE_MESSAGE\nPRIVATE_CONTINUATION",
        "PRIVATE_MESSAGE\tPRIVATE_CONTINUATION",
        "PRIVATE_MESSAGE, PRIVATE_CONTINUATION",
    ],
)
async def test_brightdata_malformed_header_messages_keep_only_classification(message: str) -> None:
    logger, stream = structured_test_logger("tests.brightdata.malformed-message")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={**_CAPTCHA_HEADERS, "x-brd-error": message}
            )
        )
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0, 0, 5), provider_name="brightdata", logger=logger
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await _brightdata_adapter(executor).search("query")
    assert caught.value.retryable is True
    assert caught.value.safe_reason == "provider_reported_failure"
    assert "upstream_message" not in caught.value.diagnostics
    assert "PRIVATE_" not in stream.getvalue() + str(caught.value)
    assert stream.getvalue().count("event=provider_response_diagnostic ") == 1
    assert "category=provider_response" in stream.getvalue()


async def test_brightdata_header_diagnostics_override_body_error_in_one_event() -> None:
    logger, stream = structured_test_logger("tests.brightdata.header-body-priority")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers=_CAPTCHA_HEADERS,
                json={"error": {"code": "PRIVATE_BODY_CODE", "message": "PRIVATE_BODY_MESSAGE"}},
            )
        )
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0, 0, 5), provider_name="brightdata", logger=logger
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await _brightdata_adapter(executor).search("query")
    assert caught.value.retryable is True
    lines = [
        line
        for line in stream.getvalue().splitlines()
        if "event=provider_response_diagnostic " in line
    ]
    assert len(lines) == 1
    fields = dict(token.split("=", 1) for token in shlex.split(lines[0])[1:])
    assert fields["upstream_code"] == "captcha"
    assert fields["upstream_message"] == "redirect location was rejected"
    assert fields["http_status"] == "200"
    assert fields["upstream_status"] == "502"
    assert "PRIVATE_" not in stream.getvalue() + str(caught.value)


async def test_brightdata_long_header_message_redacts_before_truncation() -> None:
    secret = SecretValue("CROSS_BOUNDARY_SECRET_VALUE")
    message = "m" * 500 + secret.reveal() + "z" * 700
    logger, stream = structured_test_logger("tests.brightdata.long-header", secrets=[secret])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={**_CAPTCHA_HEADERS, "x-brd-error": message}
            )
        )
    ) as client:
        executor = HttpJsonExecutor(
            client, RetryPolicy(1, 0, 0, 5), provider_name="brightdata", logger=logger
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await _brightdata_adapter(executor, secret=secret).search("query")
    lines = [
        line
        for line in stream.getvalue().splitlines()
        if "event=provider_response_diagnostic " in line
    ]
    assert len(lines) == 1
    fields = dict(token.split("=", 1) for token in shlex.split(lines[0])[1:])
    assert fields["upstream_message"].startswith("m" * 500)
    assert len(fields["upstream_message"]) <= 512
    assert fields["upstream_message_truncated"] == "true"
    assert fields["upstream_message_chars"] == str(len(message))
    assert fields["upstream_message_bytes"] == str(len(message.encode()))
    assert caught.value.retryable is True
    assert caught.value.safe_reason == "provider_reported_failure"
    logged = stream.getvalue() + str(caught.value) + repr(caught.value.diagnostics)
    assert secret.reveal() not in logged
    assert "CROSS_BOUND" not in logged
    assert "z" * 20 not in logged


async def test_brightdata_response_diagnostic_preserves_outer_and_upstream_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []
    monkeypatch.setattr(random, "uniform", lambda _low, _high: 0.9)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers=_CAPTCHA_HEADERS)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    logger, stream = structured_test_logger("tests.brightdata.canonical-header")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="brightdata",
            logger=logger,
            sleep=sleep,
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await _brightdata_adapter(executor).search("query")

    lines = [
        dict(token.split("=", 1) for token in shlex.split(line)[1:])
        for line in stream.getvalue().splitlines()
        if "event=provider_response_diagnostic " in line
    ]
    assert len(lines) == len(requests) == 3
    for fields in lines:
        assert fields["status"] == fields["http_status"] == "200"
        assert fields["brightdata_status"] == fields["upstream_status"] == "502"
        assert fields["brightdata_error_code"] == fields["upstream_code"] == "captcha"
        assert fields["upstream_message"] == "redirect location was rejected"
        assert fields["response_bytes"] == "0"
        assert fields["category"] == "provider_response"
        assert fields["semantic_result"] == "error"
    assert caught.value.status_code == 200
    assert caught.value.safe_reason == "redirect location was rejected"
    assert sleeps == pytest.approx([0.225, 0.45])
    assert "category=decode" not in stream.getvalue()


async def test_brightdata_search_classifies_masked_captcha_before_json_decode() -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers=_CAPTCHA_HEADERS)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    logger, stream = structured_test_logger("tests.brightdata.response-diagnostic")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(1, 0.25, 2.0, 30.0),
            provider_name="brightdata",
            logger=logger,
            sleep=sleep,
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await _brightdata_adapter(executor).search("query")

    failure = caught.value
    assert failure.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert failure.status_code == 200
    assert failure.retryable is True
    for key, value in {
        "brightdata_error_code": "captcha",
        "brightdata_status": 502,
        "status": 200,
        "http_status": 200,
        "response_bytes": 0,
        "category": "provider_response",
        "semantic_result": "error",
    }.items():
        assert failure.diagnostics[key] == value
    assert failure.safe_reason == "redirect location was rejected"
    assert len(requests) == 1
    assert sleeps == []
    lines = stream.getvalue().splitlines()
    assert sum("event=http_attempt_completed " in line for line in lines) == 1
    diagnostics = [line for line in lines if "event=provider_response_diagnostic " in line]
    assert len(diagnostics) == 1
    assert "http_status=200" in diagnostics[0]
    assert "brightdata_status=502" in diagnostics[0]
    assert "brightdata_error_code=captcha" in diagnostics[0]
    assert "category=decode" not in stream.getvalue()


@pytest.mark.parametrize("code", ["captcha", "timeout", "expect_element", "expect_body"])
@pytest.mark.parametrize("inner_status", [None, "200", "502"])
async def test_brightdata_recoverable_headers_retry_and_preserve_mapping(
    code: str, inner_status: str | None
) -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []
    headers = {"X-Brd-Error-Code": code}
    if inner_status is not None:
        headers["X-Brd-Status-Code"] = inner_status

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) < 3:
            return httpx.Response(200, headers=headers, json={"organic": []})
        return httpx.Response(
            200,
            json={
                "organic": [
                    {"link": "https://example.com/a", "title": "A", "description": "Snippet A"}
                ],
            },
        )

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="brightdata",
            sleep=sleep,
        )
        assert await _brightdata_adapter(executor).search("query") == [
            KeywordSearchHit("https://example.com/a", "A", "Snippet A")
        ]

    assert len(requests) == 3
    assert len(sleeps) == 2


@pytest.mark.parametrize(
    ("outer_status", "inner_status"), [(408, 408), (429, 429), (503, 503), (503, 502)]
)
async def test_brightdata_status_only_http_failure_preserves_generic_retries(
    outer_status: int, inner_status: int
) -> None:
    """Status-only metadata must preserve the shared HTTP retry budget and delays."""
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) < 3:
            return httpx.Response(outer_status, headers={"x-brd-status-code": str(inner_status)})
        return httpx.Response(
            200, json={"organic": [{"link": "https://example.com/a", "title": "A"}]}
        )

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="brightdata",
            sleep=sleep,
        )
        assert await _brightdata_adapter(executor).search("query") == [
            KeywordSearchHit("https://example.com/a", "A", "")
        ]

    assert len(requests) == 3
    assert sleeps == [0.25, 0.5]


@pytest.mark.parametrize(
    ("outer_status", "headers", "safe_code", "inner_status"),
    [
        (503, {"x-brd-status-code": "401"}, "missing", 401),
        (503, {"x-brd-status-code": "403"}, "missing", 403),
        (503, {"x-brd-status-code": "404"}, "missing", 404),
        (503, {"x-brd-status-code": "bad"}, "missing", None),
        (
            503,
            {"x-brd-error-code": "", "x-brd-status-code": "503"},
            "invalid",
            503,
        ),
        (
            503,
            {"x-brd-error-code": "other_code", "x-brd-status-code": "503"},
            "unknown",
            503,
        ),
        (
            503,
            {"x-brd-error": "provider failure", "x-brd-status-code": "503"},
            "missing",
            503,
        ),
        (200, {"x-brd-error-code": "other_code"}, "unknown", None),
        (200, {"x-brd-error-code": "invalid_zone"}, "unknown", None),
        (200, {"x-brd-error-code": "auth_failed"}, "unknown", None),
        (500, {"x-brd-error-code": "other_code"}, "unknown", None),
        (200, {"x-brd-error": "explanation only"}, "missing", None),
        (401, _CAPTCHA_HEADERS, "captcha", 502),
        (403, _CAPTCHA_HEADERS, "captcha", 502),
        (200, {"x-brd-error-code": "captcha", "x-brd-status-code": "401"}, "captcha", 401),
        (200, {"x-brd-error-code": "captcha", "x-brd-status-code": "403"}, "captcha", 403),
        (200, {"x-brd-status-code": "502"}, "missing", 502),
        (200, {"x-brd-status-code": "302"}, "missing", 302),
        (200, {"x-brd-status-code": "100"}, "missing", 100),
        (200, {"x-brd-status-code": "599"}, "missing", 599),
    ],
)
async def test_brightdata_terminal_header_failures_do_not_retry(
    outer_status: int, headers: dict[str, str], safe_code: str, inner_status: int | None
) -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(outer_status, headers=headers, json={"organic": []})

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="brightdata",
            sleep=sleep,
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await _brightdata_adapter(executor).search("query")

    failure = caught.value
    assert failure.status_code == outer_status
    assert failure.retryable is False
    assert failure.diagnostics["brightdata_error_code"] == safe_code
    if inner_status is None:
        assert "brightdata_status" not in failure.diagnostics
    else:
        assert failure.diagnostics["brightdata_status"] == inner_status
    assert len(requests) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("headers", "safe_code"),
    [
        ([("x-brd-error-code", "captcha" + "x" * 64)], "invalid"),
        ([("x-brd-error-code", "captcha\t")], "invalid"),
        ([("x-brd-error-code", "captcha\x7f")], "invalid"),
        ([("x-brd-error-code", "")], "invalid"),
        ([("x-brd-error-code", "captcha, timeout")], "invalid"),
        ([("x-brd-error-code", "captcha"), ("x-brd-error-code", "captcha")], "invalid"),
        ([("x-brd-error-code", "CAPTCHA")], "unknown"),
        ([("x-brd-error-code", "captcha suffix")], "unknown"),
        ([("x-brd-status-code", "200"), ("x-brd-status-code", "502")], "missing"),
        *[
            ([("x-brd-error-code", "captcha"), ("x-brd-status-code", status)], "captcha")
            for status in ("", "bad", "099", "600", "0502", "502.0", "200, 502", "502\n")
        ],
        (
            [
                ("x-brd-error-code", "captcha"),
                ("x-brd-status-code", "200"),
                ("x-brd-status-code", "502"),
            ],
            "captcha",
        ),
    ],
)
async def test_brightdata_malformed_or_ambiguous_headers_are_terminal(
    headers: list[tuple[str, str]], safe_code: str
) -> None:
    logger, stream = structured_test_logger("tests.brightdata.malformed-headers")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers=headers, json={"organic": []})

    async def no_retry(_delay: float) -> None:
        pytest.fail("Invalid header classification must not schedule retries")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="brightdata",
            logger=logger,
            sleep=no_retry,
        )
        with pytest.raises(ProviderResponseFailure) as caught:
            await _brightdata_adapter(executor).search("query")

    assert caught.value.retryable is False
    assert caught.value.diagnostics["brightdata_error_code"] == safe_code
    assert "brightdata_status" not in caught.value.diagnostics
    assert "upstream_status" not in caught.value.diagnostics
    if safe_code == "invalid":
        assert "upstream_code" not in caught.value.diagnostics
        for name, value in headers:
            if name == "x-brd-error-code" and value:
                assert value not in stream.getvalue()
    assert stream.getvalue().count("event=provider_response_diagnostic ") == 1
    assert len(requests) == 1


@pytest.mark.parametrize("headers", [{}, {"x-brd-status-code": "200"}])
async def test_brightdata_benign_headers_preserve_success(headers: dict[str, str]) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, headers=headers, json={"organic": []})
        )
    ) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(1, 0.25, 2.0, 30.0),
            provider_name="brightdata",
        )
        assert await _brightdata_adapter(executor).search("query") == []


@pytest.mark.parametrize("body", ["", "not JSON"])
@pytest.mark.parametrize("headers", [{}, {"x-brd-status-code": "200"}])
async def test_brightdata_without_header_failure_preserves_decode_errors(
    body: str, headers: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    from agent_search_gateway.errors import ProtocolFailure

    caplog.set_level(logging.DEBUG)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers=headers, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="brightdata",
        )
        with pytest.raises(ProtocolFailure, match="response was not valid JSON"):
            await _brightdata_adapter(executor).search("query")
    assert len(requests) == 1
    diagnostics = _events(caplog, "provider_response_diagnostic")
    assert len(diagnostics) == 1
    assert diagnostics[0]["category"] == "decode"
    assert diagnostics[0]["semantic_result"] == "unknown"


async def test_brightdata_fetch_does_not_inherit_search_header_inspection() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"organic": []})
        return httpx.Response(200, headers=_CAPTCHA_HEADERS, text="# fetched\nbody")

    async def no_retry(_delay: float) -> None:
        pytest.fail("Fetch must not inherit Search header retries")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(3, 0.25, 2.0, 30.0),
            provider_name="brightdata",
            sleep=no_retry,
        )
        adapter = _brightdata_adapter(executor)
        assert await adapter.search("query") == []
        assert await adapter.fetch(normalize_url("https://example.com")) == URLFetchCandidate(
            "# fetched\nbody", "# fetched\nbody"
        )
    assert len(requests) == 2
