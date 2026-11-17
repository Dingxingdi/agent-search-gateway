"""Opt-in, bounded live checks; output contains only fixed classifications and counts."""

import json
import os
import time
from dataclasses import dataclass, field

import httpx
import pytest

from agent_search_gateway.errors import ExecutionFailure, ProtocolFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import DiagnosticText, SecretValue
from agent_search_gateway.providers.http import HttpJsonExecutor, HttpStatusFailure
from agent_search_gateway.providers.web.doubao import DoubaoAdapter
from agent_search_gateway.providers.web.keenable import KeenableAdapter
from agent_search_gateway.providers.web.open_websearch import OpenWebSearchAdapter
from agent_search_gateway.providers.web.youcom import YouComAdapter
from agent_search_gateway.url_normalization import normalize_url

pytestmark = pytest.mark.skipif(
    os.environ.get("WEB_SEARCH_RUN_INTEGRATION") != "1",
    reason="live provider integration tests are opt-in",
)

_PROVIDERS = ("doubao", "youcom", "keenable", "open_websearch")
_POLICY = RetryPolicy(3, 0.25, 2.0, 30.0)
_ENGINES = ("bing", "duckduckgo")
_KEY_ENV = {
    "doubao": "DOUBAO_" + "API_KEY",
    "youcom": "YDC_" + "API_KEY",
    "keenable": "KEENABLE_" + "API_KEY",
}
_KNOWN_CODES = frozenset(
    {
        "10400",
        "10401",
        "10402",
        "10403",
        "10406",
        "10407",
        "10408",
        "10409",
        "10410",
        "10412",
        "10413",
        "10500",
        "10502",
        "700429",
        "unauthorized",
        "payment_required",
        "forbidden",
        "invalid_request",
        "spend_cap_exceeded",
        "invalid_json",
        "invalid_parameter",
        "missing_parameter",
        "unknown_parameter",
        "query_rejected",
        "search_rejected_upstream",
        "missing_api_key",
        "invalid_api_key",
        "invalid_authorization_header",
        "api_key_required",
        "insufficient_credits",
        "credentials_forbidden",
        "search_failed_upstream",
        "search_rate_limited",
        "rate_limited",
        "internal_error",
        "search_upstream_error",
        "search_overloaded",
        "content_not_extracted",
        "target_timeout",
        "engine_error",
        "browser_unavailable",
        "validation_failed",
        "fetch_failed",
        "partial_engine_failures",
        "unknown",
    }
)
_Adapter = DoubaoAdapter | YouComAdapter | KeenableAdapter | OpenWebSearchAdapter


@dataclass
class _Observation:
    attempts: int = 0
    status: int | None = None
    effective_list_matches: bool = False
    partial_failure_count: int | None = None
    hit_engines: list[str] = field(default_factory=list)

    async def request(self, request: httpx.Request) -> None:
        self.attempts += 1

    async def response(self, response: httpx.Response) -> None:
        self.status = response.status_code
        await response.aread()
        try:
            payload = response.json()
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if not isinstance(data, dict):
            return
        # This is the parsed/filtered request list, never a success list.
        self.effective_list_matches = data.get("engines") == list(_ENGINES)
        failures = data.get("partialFailures", [])
        if isinstance(failures, list):
            self.partial_failure_count = len(failures)
        results = data.get("results")
        if isinstance(results, list):
            self.hit_engines = [
                engine
                for engine in _ENGINES
                if any(isinstance(row, dict) and row.get("engine") == engine for row in results)
            ]


def _settings(provider: str) -> tuple[str, SecretValue | None]:
    if provider == "open_websearch":
        root = os.environ.get("OPEN_WEBSEARCH_API_URL")
        if not root:
            pytest.skip("OPEN_WEBSEARCH_API_URL is not set")
        return root, None
    variable = _KEY_ENV[provider]
    value = os.environ.get(variable)
    if not value:
        pytest.skip(f"{variable} is not set")
    roots = {
        "doubao": "https://open.feedcoopapi.com/search_api",
        "youcom": "https://ydc-index.io",
        "keenable": "https://api.keenable.ai",
    }
    return roots[provider], SecretValue(value)


def _adapter(
    provider: str, root: str, secret: SecretValue | None, executor: HttpJsonExecutor
) -> _Adapter:
    if provider == "open_websearch":
        return OpenWebSearchAdapter(
            name=provider, api_url=root, http_executor=executor, engines=list(_ENGINES)
        )
    if secret is None:
        raise ValueError("credential prerequisite")
    if provider == "doubao":
        return DoubaoAdapter(name=provider, api_url=root, secret=secret, http_executor=executor)
    if provider == "youcom":
        return YouComAdapter(name=provider, api_url=root, secret=secret, http_executor=executor)
    return KeenableAdapter(name=provider, api_url=root, secret=secret, http_executor=executor)


def _failure_fields(exc: Exception) -> tuple[str, str | None]:
    if isinstance(exc, HttpStatusFailure):
        category = "http_status"
    elif isinstance(exc, ProtocolFailure):
        category = "decode"
    elif isinstance(exc, httpx.TransportError) or isinstance(exc.__cause__, httpx.TransportError):
        category = "transport"
    elif isinstance(exc, ExecutionFailure):
        category = "provider_or_schema"
    else:
        category = "unexpected"
    code = exc.diagnostics.get("upstream_code") if isinstance(exc, ExecutionFailure) else None
    if isinstance(code, DiagnosticText):
        code = code.text
    return category, code if isinstance(code, str) and code in _KNOWN_CODES else None


async def _run(provider: str, stage: str) -> None:
    root, secret = _settings(provider)
    observed = _Observation()
    metrics: dict[str, object] = {
        "provider": provider,
        "stage": stage,
        "category": "success",
        "known_code": None,
        "shape_passed": False,
    }
    started = time.monotonic()
    failed = False
    try:
        async with httpx.AsyncClient(
            event_hooks={"request": [observed.request], "response": [observed.response]}
        ) as client:
            executor = HttpJsonExecutor(client, _POLICY, provider_name=provider)
            adapter = _adapter(provider, root, secret, executor)
            if stage == "search":
                hits = await adapter.search("Python programming language official documentation")
                metrics["hits"] = len(hits)
                valid = bool(hits) and all(
                    isinstance(hit.url, str)
                    and str(normalize_url(hit.url)).startswith(("http://", "https://"))
                    and isinstance(hit.title, str)
                    and isinstance(hit.snippet, str)
                    for hit in hits
                )
                if provider == "open_websearch":
                    valid = valid and observed.effective_list_matches
            else:
                candidate = await adapter.fetch(normalize_url("https://www.python.org/about/"))
                metrics["content_length"] = len(candidate.raw_content)
                valid = (
                    bool(candidate.raw_content.strip())
                    and candidate.content == candidate.raw_content
                )
            if not valid:
                metrics["category"] = "response_contract"
                failed = True
            else:
                metrics["shape_passed"] = True
    except Exception as exc:
        # Never render exception text, request objects, or raw payloads.
        metrics["category"], metrics["known_code"] = _failure_fields(exc)
        failed = True
    metrics.update(
        attempts=observed.attempts,
        http_status=observed.status,
        elapsed_ms=round((time.monotonic() - started) * 1000),
    )
    if provider == "open_websearch" and stage == "search":
        metrics.update(
            effective_list_matches=observed.effective_list_matches,
            partial_failure_count=observed.partial_failure_count,
            hit_engines=observed.hit_engines,
        )
    print(json.dumps(metrics, sort_keys=True))
    if failed:
        pytest.fail(f"{provider}/{stage}: {metrics['category']}", pytrace=False)


@pytest.mark.parametrize("provider", _PROVIDERS, ids=_PROVIDERS)
async def test_live_search_returns_valid_urls(provider: str) -> None:
    await _run(provider, "search")


@pytest.mark.parametrize("provider", _PROVIDERS, ids=_PROVIDERS)
async def test_live_fetch_returns_nonempty_content(provider: str) -> None:
    await _run(provider, "fetch")
