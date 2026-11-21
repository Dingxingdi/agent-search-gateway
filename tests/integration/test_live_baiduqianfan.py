"""Opt-in, Baidu-only live search check."""

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass

import httpx
import pytest

from agent_search_gateway.config import ResolvedRatePolicy, ResolvedRateWindow
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import DiagnosticText, SecretValue
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.web.baiduqianfan import BaiduQianfanAdapter
from agent_search_gateway.rate_limit import RateGate

pytestmark = pytest.mark.skipif(
    os.environ.get("WEB_SEARCH_RUN_INTEGRATION") != "1",
    reason="live provider integration tests are opt-in",
)

_STAGES = ("web_search", "agentic_search")
_KNOWN_CODES = {
    "InvalidArgument",
    "InternalError",
    "216003",
    "QUOTA_USER_DAILY_FREE",
    "BILLING_INSUFFICIENT_BALANCE",
    "RATE_LIMIT_SEARCH_QPS",
}
_BUDGET_SECONDS = 100.0


@dataclass
class _Branch:
    category: str = "unverified"
    known_code: str | None = None
    attempts: int = 0
    http_status: int | None = None
    received: int | None = None
    hits: int | None = None


class _Evidence(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.branches = {stage: _Branch() for stage in _STAGES}
        self.paths = {f"/v2/ai_search/{stage}": branch for stage, branch in self.branches.items()}

    async def request(self, request: httpx.Request) -> None:
        if branch := self.paths.get(request.url.path):
            branch.attempts += 1

    async def response(self, response: httpx.Response) -> None:
        if branch := self.paths.get(response.request.url.path):
            branch.http_status = response.status_code

    def emit(self, record: logging.LogRecord) -> None:
        fields = getattr(record, "gateway_fields", {})
        if fields.get("provider") != "baiduqianfan":
            return
        event = getattr(record, "gateway_event", None)
        if event == "provider_partial_failure" and fields.get("branch") in _STAGES:
            branch = self.branches[fields["branch"]]
            code = fields.get("upstream_code")
            if isinstance(code, DiagnosticText) and code.text in _KNOWN_CODES:
                branch.known_code = code.text
        if (
            event == "provider_candidates_summary"
            and fields.get("phase") == "adapter"
            and fields.get("stage") in _STAGES
        ):
            received, accepted = fields.get("received"), fields.get("accepted")
            if type(received) is int and type(accepted) is int and 0 <= accepted <= received:
                branch = self.branches[fields["stage"]]
                branch.received, branch.hits = received, accepted
                branch.category = "verified"


@contextmanager
def _collect_evidence(evidence: _Evidence) -> Iterator[None]:
    logger = logging.getLogger(BaiduQianfanAdapter.__module__)
    level, disabled, propagate = logger.level, logger.disabled, logger.propagate
    logger.disabled = False
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(evidence)
    try:
        yield
    finally:
        logger.removeHandler(evidence)
        logger.setLevel(level)
        logger.disabled, logger.propagate = disabled, propagate


async def _run(env: Mapping[str, str], client_factory: Callable[[], httpx.AsyncClient]) -> None:
    key = env.get("BAIDUQIANFAN_API_KEY")
    if not key or not key.strip():
        pytest.skip("BAIDUQIANFAN_API_KEY is not set")
    evidence = _Evidence()
    started = time.monotonic()
    failed = False
    try:
        async with client_factory() as client:
            client.event_hooks["request"].append(evidence.request)
            client.event_hooks["response"].append(evidence.response)
            executor = HttpJsonExecutor(
                client,
                RetryPolicy(3, 0.25, 2.0, 30.0),
                provider_name="baiduqianfan",
                rate_gate=RateGate(ResolvedRatePolicy((ResolvedRateWindow(3, 1.0),))),
            )
            adapter = BaiduQianfanAdapter(
                name="baiduqianfan",
                secret=SecretValue(key),
                http_executor=executor,
            )
            with _collect_evidence(evidence):
                # Retry-After can exceed the local backoff limit.
                async with asyncio.timeout(_BUDGET_SECONDS):
                    await adapter.search("Python 官方文档")
    except Exception:
        failed = True

    elapsed_ms = round((time.monotonic() - started) * 1000)
    for stage, branch in evidence.branches.items():
        if branch.category == "unverified" and branch.http_status is not None:
            branch.category = "http_status" if branch.http_status >= 400 else "provider_or_schema"
        print(
            json.dumps({"stage": stage, "elapsed_ms": elapsed_ms, **asdict(branch)}, sort_keys=True)
        )
    if failed or any(branch.category != "verified" for branch in evidence.branches.values()):
        pytest.fail("Could not verify both Baidu search branches", pytrace=False)


async def test_live_baidu_search() -> None:
    await _run(os.environ, httpx.AsyncClient)
