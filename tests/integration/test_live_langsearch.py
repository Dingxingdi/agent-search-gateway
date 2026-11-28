"""Opt-in LangSearch-only smoke through the production CLI and a private daemon."""

import asyncio
import io
import json
import os
from collections.abc import Mapping
from contextlib import aclosing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_search_gateway import cli
from agent_search_gateway.config import ResolvedConfig, resolve_config
from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.models import URLStatus
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.runtime import HttpClientFactory, Runtime
from agent_search_gateway.url_normalization import normalize_url
from tests.acceptance.test_gateway_workflows import _running_daemon

pytestmark = pytest.mark.skipif(
    os.environ.get("WEB_SEARCH_RUN_INTEGRATION") != "1",
    reason="live provider integration tests are opt-in",
)

_BASE = "https://api.langsearch.com"
_KEY_ENV = "LANGSEARCH_API_KEY"
_QUERIES = ("Python documentation asyncio", "NASA Mars exploration")


def _config(key: str) -> ResolvedConfig:
    return resolve_config(
        {
            "web_providers": {
                "langsearch": {
                    "enable_search": True,
                    "enable_fetch": False,
                    "credentials": [
                        {
                            "api_url": _BASE,
                            "api_key_env": _KEY_ENV,
                            "rate_limits": [{"requests": 5, "period_seconds": 1}],
                        }
                    ],
                }
            },
            "llm_providers": {
                "unused": {
                    "protocol": "openai",
                    "api_endpoint": "chat_completions",
                    "credentials": [{"api_url": "https://llm.invalid", "api_key_env": "UNUSED"}],
                }
            },
            "global_default_llm": {"provider": "unused", "model": "unused"},
            "retry": {
                "max_attempts": 2,
                "base_delay_seconds": 0.25,
                "max_delay_seconds": 2.0,
                "request_timeout_seconds": 15.0,
            },
        },
        build_default_registry(),
        {_KEY_ENV: key, "UNUSED": "inert"},
    )


@dataclass
class _Evidence:
    category: str = "unverified"
    queries: int = 0
    attempts: int = 0
    http_status: int | None = None
    received: int | None = None
    admitted: int = 0
    empty_bodies: bool = False
    cleanup: bool = False

    async def request(self, request: httpx.Request) -> None:
        if (
            request.method != "POST"
            or str(request.url) != f"{_BASE}/v1/web-search"
            or json.loads(request.content) != {"query": _QUERIES[self.queries - 1], "count": 10}
        ):
            self.category = "unexpected_request"
            raise ValueError("unexpected request")
        self.attempts += 1

    async def response(self, response: httpx.Response) -> None:
        self.http_status = response.status_code
        self.received = None
        if response.status_code == 200:
            await response.aread()
            try:
                envelope = response.json()
                items = envelope["data"]["webPages"]["value"]
                code = envelope["code"]
                if type(code) is int and code == 200 and isinstance(items, list):
                    self.received = len(items)
            except (KeyError, TypeError, ValueError):
                pass


def _admitted(path: Path, paths: RuntimePaths, runtime: Runtime) -> int:
    assert path.parent == paths.results_dir and path.match("keyword-*.jsonl")
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        row = json.loads(line)
        assert set(row) == {"url", "abstract"}
        record = runtime.store.get(normalize_url(row["url"]))
        assert record is not None and record.status is URLStatus.NEED_FETCH
        assert row["abstract"].strip() and record.abstract == row["abstract"]
        assert record.raw_content == record.content == ""
    return len(lines)


async def _cancel_on_shutdown(active: tuple[asyncio.Task[object], ...], grace: float) -> None:
    # The socket client timing out does not cancel the daemon's independent workflow.
    # Signal an expired grace period so this private daemon cancels and joins it.
    if active:
        raise TimeoutError


async def _run(
    env: Mapping[str, str],
    paths: RuntimePaths,
    client_factory: HttpClientFactory = httpx.AsyncClient,
    *,
    budget: float = 90.0,
) -> None:
    key = env.get(_KEY_ENV, "")
    if not key.strip():
        pytest.skip("LANGSEARCH_API_KEY is not set")

    evidence = _Evidence()
    clients: list[httpx.AsyncClient] = []

    def observed_client(**kwargs: Any) -> httpx.AsyncClient:
        client = client_factory(**kwargs)
        clients.append(client)
        client.event_hooks["request"].append(evidence.request)
        client.event_hooks["response"].append(evidence.response)
        return client

    try:
        runtime = await Runtime.build(_config(key), paths, http_client_factory=observed_client)
        daemon = ForegroundDaemon(
            paths, runtime_factory=lambda: runtime, shutdown_waiter=_cancel_on_shutdown
        )
        async with aclosing(runtime), _running_daemon(daemon), asyncio.timeout(budget):
            for query in _QUERIES:
                stdout, stderr = io.StringIO(), io.StringIO()
                args = cli.build_parser().parse_args(
                    ["keyword-search", query, "--provider", "langsearch"]
                )
                evidence.queries += 1
                evidence.http_status = evidence.received = None
                code = await cli.run_command(args, paths, stdout=stdout, stderr=stderr)
                if evidence.category == "unexpected_request":
                    break
                if code or stderr.getvalue():
                    if evidence.http_status is None:
                        evidence.category = "transport_or_workflow"
                    elif evidence.http_status != 200:
                        evidence.category = "http_status"
                    elif evidence.received:
                        evidence.category = "unusable_results"
                    else:
                        evidence.category = "provider_or_schema"
                    break
                assert evidence.http_status == 200 and evidence.received is not None
                evidence.admitted = _admitted(Path(stdout.getvalue().strip()), paths, runtime)
                if evidence.admitted:
                    assert evidence.admitted <= evidence.received
                    evidence.empty_bodies = True
                    evidence.category = "verified"
                    break
                if evidence.received:
                    evidence.category = "unusable_results"
                    break
                evidence.category = "empty_results"
    except TimeoutError:
        evidence.category = "timeout"
    except Exception:
        evidence.category = "workflow_failed"
    evidence.cleanup = not paths.socket_file.exists() and all(
        client.is_closed for client in clients
    )
    print(json.dumps(asdict(evidence), sort_keys=True))
    if evidence.category != "verified" or not evidence.cleanup:
        pytest.fail("Could not verify LangSearch retrieval", pytrace=False)


async def test_live_langsearch_search(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path / "config.toml", tmp_path / "daemon.sock", tmp_path / "results")
    await _run(os.environ, paths)
