"""Shared response-diagnostic assertions for web fetch provider contract tests."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import cast

import httpx
import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import (
    DiagnosticText,
    KeyValueFormatter,
    SecretRedactor,
    SecretValue,
)
from agent_search_gateway.providers.contracts import URLFetchCandidate, URLFetchProvider
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.url_normalization import normalize_url

FetchAdapterFactory = Callable[[HttpJsonExecutor], URLFetchProvider]


@asynccontextmanager
async def _diagnostic_adapter(
    caplog: pytest.LogCaptureFixture,
    provider_name: str,
    make_adapter: FetchAdapterFactory,
    response: httpx.Response,
) -> AsyncIterator[URLFetchProvider]:
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        yield make_adapter(
            HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name=provider_name)
        )


def assert_safe_response_diagnostic(
    caplog: pytest.LogCaptureFixture, semantic: str
) -> tuple[dict[str, object], str]:
    """Require one diagnostic and keep every private sentinel out of formatted logs."""
    records = [record for record in caplog.records if getattr(record, "gateway_event", None)]
    diagnostics = [
        cast(dict[str, object], vars(record)["gateway_fields"])
        for record in records
        if getattr(record, "gateway_event", None) == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["semantic_result"] == semantic
    rendered = "\n".join(
        KeyValueFormatter(SecretRedactor([SecretValue("PRIVATE_AUTH")])).format(record)
        for record in records
    )
    assert "PRIVATE_" not in rendered
    return diagnostics[0], rendered


async def assert_text_fetch_diagnostics(
    caplog: pytest.LogCaptureFixture,
    provider_name: str,
    make_adapter: FetchAdapterFactory,
    body: str,
    status: int,
    semantic: str,
) -> None:
    response = httpx.Response(
        status,
        text=body,
        headers={
            "content-type": "text/markdown",
            "x-brd-status-code": "502",
            "x-brd-error-code": "captcha",
        },
    )
    async with _diagnostic_adapter(caplog, provider_name, make_adapter, response) as adapter:
        target = normalize_url("https://example.com/page?key=PRIVATE_QUERY")
        if semantic == "success":
            assert await adapter.fetch(target) == URLFetchCandidate(body, body)
        else:
            with pytest.raises(ExecutionFailure) as caught:
                await adapter.fetch(target)
            expected = "page body is empty" if semantic == "empty" else "HTTP status 403"
            assert caught.value.message == f"{provider_name}/fetch: {expected}"
            assert caught.value.diagnostics["semantic_result"] == semantic
    diagnostic, _ = assert_safe_response_diagnostic(caplog, semantic)
    assert diagnostic["response_kind"] == "text"
    assert diagnostic["http_status"] == status
    assert diagnostic["response_bytes"] == len(body.encode())
    assert diagnostic["content_type"] == "text/markdown"
    assert "upstream_message" not in diagnostic
    assert "upstream_code" not in diagnostic


async def assert_json_fetch_diagnostics(
    caplog: pytest.LogCaptureFixture,
    provider_name: str,
    make_adapter: FetchAdapterFactory,
    payload: object,
    message: str | None,
    semantic: str,
    *,
    check_provider_response_category: bool = False,
) -> None:
    response = httpx.Response(200, json=payload)
    async with _diagnostic_adapter(caplog, provider_name, make_adapter, response) as adapter:
        target = normalize_url("https://example.com/page?key=PRIVATE_QUERY")
        if message is None:
            assert (await adapter.fetch(target)).content == "PRIVATE_BODY"
        else:
            with pytest.raises(ExecutionFailure) as caught:
                await adapter.fetch(target)
            assert caught.value.message == f"{provider_name}/fetch: {message}"
            if semantic == "error":
                selected = caught.value.diagnostics["upstream_message"]
                assert isinstance(selected, DiagnosticText)
                assert selected.text == "target blocked"
    diagnostic, rendered = assert_safe_response_diagnostic(caplog, semantic)
    if semantic == "error":
        if check_provider_response_category:
            assert diagnostic["category"] == "provider_response"
        assert 'upstream_message="target blocked"' in rendered
    else:
        assert "target blocked" not in rendered
