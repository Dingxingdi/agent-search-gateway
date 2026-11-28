"""Offline validation of the LangSearch smoke with only the HTTP transport replaced."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from agent_search_gateway import cli
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.runtime import HttpClientFactory
from tests.integration.test_live_langsearch import _run


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(tmp_path / "config.toml", tmp_path / "daemon.sock", tmp_path / "results")


@pytest.mark.parametrize("env", [{}, {"LANGSEARCH_API_KEY": "  "}], ids=["missing", "blank"])
async def test_requires_key_before_opening_clients(
    env: dict[str, str], paths: RuntimePaths
) -> None:
    def forbidden(**kwargs: object) -> httpx.AsyncClient:
        pytest.fail("constructed a client before checking credentials")

    with pytest.raises(pytest.skip.Exception, match="LANGSEARCH_API_KEY"):
        await _run(env, paths, forbidden)


_KEY_ENV = "LANGSEARCH_API_KEY"
_ENV = {_KEY_ENV: "offline-langsearch-credential"}
_HIT = {"url": "https://example.test/article", "name": "Title", "snippet": "Abstract"}


def _envelope(items: list[object]) -> httpx.Response:
    return httpx.Response(200, json={"code": 200, "data": {"webPages": {"value": items}}})


def _client_factory(
    transport: httpx.MockTransport, clients: list[httpx.AsyncClient] | None = None
) -> HttpClientFactory:
    def factory(**kwargs: Any) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=transport, **kwargs)
        if clients is not None:
            clients.append(client)
        return client

    return factory


async def test_proves_retrieval_through_cli(
    paths: RuntimePaths, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[httpx.Request] = []
    clients: list[httpx.AsyncClient] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _envelope([{**_HIT, "text": "Ignored", "content": "Ignored"}])

    factory = _client_factory(httpx.MockTransport(respond), clients)

    with patch.object(cli, "run_command", wraps=cli.run_command) as command:
        await _run(_ENV, paths, factory)
    command.assert_awaited_once()
    assert command.await_args is not None
    args, cli_paths = command.await_args.args
    assert (args.command, args.provider, cli_paths) == ("keyword-search", "langsearch", paths)
    assert [(r.method, str(r.url), json.loads(r.content)) for r in calls] == [
        ("POST", "https://api.langsearch.com/v1/web-search", {"query": args.query, "count": 10})
    ]
    (result,) = paths.results_dir.glob("keyword-*.jsonl")
    assert json.loads(result.read_text()) == {"url": _HIT["url"], "abstract": "Abstract"}
    report = json.loads(capsys.readouterr().out)
    assert report["category"] == "verified"
    assert report["queries"] == report["attempts"] == report["admitted"] == 1
    assert report["http_status"] == 200 and report["empty_bodies"]
    assert report["cleanup"] and clients and all(client.is_closed for client in clients)
    assert not paths.socket_file.exists()


@pytest.mark.parametrize("second_has_hit", [False, True], ids=["still-empty", "alternate-hit"])
async def test_only_genuine_empty_retrieval_gets_one_alternate(
    paths: RuntimePaths, second_has_hit: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    queries: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        queries.append(json.loads(request.content)["query"])
        return _envelope([_HIT] if second_has_hit and len(queries) == 2 else [])

    factory = _client_factory(httpx.MockTransport(respond))

    if second_has_hit:
        await _run(_ENV, paths, factory)
    else:
        with pytest.raises(pytest.fail.Exception, match="Could not verify LangSearch retrieval"):
            await _run(_ENV, paths, factory)
    report = json.loads(capsys.readouterr().out)
    assert report["category"] == ("verified" if second_has_hit else "empty_results")
    assert len(queries) == len(set(queries)) == report["attempts"] == report["queries"] == 2
    assert report["cleanup"]


@pytest.mark.parametrize(
    ("response", "category"),
    [
        (httpx.Response(401, text=_ENV[_KEY_ENV]), "http_status"),
        (httpx.Response(429, text=_ENV[_KEY_ENV]), "http_status"),
        (httpx.Response(200, content=b"invalid JSON"), "provider_or_schema"),
        (httpx.Response(200, json={"code": 403, "message": _ENV[_KEY_ENV]}), "provider_or_schema"),
        (_envelope([{"renamed_url": _HIT["url"]}]), "unusable_results"),
        (_envelope([{"url": "ftp://example.test", "name": "Title"}]), "unusable_results"),
        (_envelope([{"url": _HIT["url"]}]), "unusable_results"),
    ],
    ids=["unauthorized", "quota", "bad-json", "business", "all-bad", "invalid-url", "no-abstract"],
)
async def test_failures_report_safe_evidence_without_another_query(
    paths: RuntimePaths,
    response: httpx.Response,
    category: str,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["query"])
        return response

    factory = _client_factory(httpx.MockTransport(respond))

    with caplog.at_level(logging.DEBUG), pytest.raises(pytest.fail.Exception) as failure:
        await _run(_ENV, paths, factory)
    captured = capsys.readouterr()
    assert _ENV[_KEY_ENV] not in captured.out + captured.err + str(failure.value) + caplog.text
    report = json.loads(captured.out)
    assert report["category"] == category
    assert report["queries"] == len(set(calls)) == 1
    assert 1 <= report["attempts"] == len(calls) <= 2
    assert report["http_status"] == response.status_code
    assert report["admitted"] == 0 and report["cleanup"]


@pytest.mark.parametrize("first_status", [429, 503])
async def test_retry_transport_failure_does_not_reuse_previous_http_status(
    paths: RuntimePaths, first_status: int, capsys: pytest.CaptureFixture[str]
) -> None:
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(first_status)
        raise httpx.ConnectError("connection failed", request=request)

    factory = _client_factory(httpx.MockTransport(respond))

    with pytest.raises(pytest.fail.Exception, match="Could not verify LangSearch retrieval"):
        await _run(_ENV, paths, factory)
    report = json.loads(capsys.readouterr().out)
    assert report["category"] == "transport_or_workflow"
    assert report["http_status"] is None and report["received"] is None
    assert report["attempts"] == attempts == 2 and report["queries"] == 1
    assert report["admitted"] == 0 and report["cleanup"]


async def test_budget_cancels_slow_request_and_closes_daemon(
    paths: RuntimePaths, capsys: pytest.CaptureFixture[str]
) -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()
    clients: list[httpx.AsyncClient] = []

    async def slow(request: httpx.Request) -> httpx.Response:
        entered.set()
        try:
            await asyncio.sleep(0.6)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return _envelope([_HIT])

    factory = _client_factory(httpx.MockTransport(slow), clients)

    with pytest.raises(pytest.fail.Exception, match="Could not verify LangSearch retrieval"):
        await asyncio.wait_for(_run(_ENV, paths, factory, budget=0.1), timeout=3)
    assert entered.is_set() and cancelled.is_set()
    assert clients and all(c.is_closed for c in clients) and not paths.socket_file.exists()
    report = json.loads(capsys.readouterr().out)
    assert report["category"] == "timeout" and report["cleanup"] and report["attempts"] == 1


@pytest.mark.parametrize(
    "destination",
    ["https://llm.invalid/v1/chat/completions", "https://api.langsearch.com/v1/fetch"],
)
async def test_blocks_unexpected_destinations_before_transport(
    paths: RuntimePaths, destination: str, capsys: pytest.CaptureFixture[str]
) -> None:
    sends = 0

    async def redirect(request: httpx.Request) -> None:
        request.url = httpx.URL(destination)

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return _envelope([_HIT])

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(respond), event_hooks={"request": [redirect]}, **kwargs
        )

    with pytest.raises(pytest.fail.Exception):
        await _run(_ENV, paths, factory)
    report = json.loads(capsys.readouterr().out)
    assert sends == report["attempts"] == 0
    assert report["category"] == "unexpected_request" and report["cleanup"]


async def test_budget_includes_upstream_retry_after(
    paths: RuntimePaths, capsys: pytest.CaptureFixture[str]
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3600"})

    factory = _client_factory(httpx.MockTransport(respond))

    with pytest.raises(pytest.fail.Exception):
        await asyncio.wait_for(_run(_ENV, paths, factory, budget=0.1), timeout=3)
    report = json.loads(capsys.readouterr().out)
    assert report["category"] == "timeout" and report["http_status"] == 429
    assert report["attempts"] == report["queries"] == 1 and report["cleanup"]


async def test_rejects_unplanned_payload_before_sending(
    paths: RuntimePaths, capsys: pytest.CaptureFixture[str]
) -> None:
    class ModifiedClient(httpx.AsyncClient):
        def build_request(self, *args: Any, **kwargs: Any) -> httpx.Request:
            kwargs["json"] = {**kwargs["json"], "contents": True}
            return super().build_request(*args, **kwargs)

    sends = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        sends += 1
        return _envelope([_HIT])

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return ModifiedClient(transport=httpx.MockTransport(respond), **kwargs)

    with pytest.raises(pytest.fail.Exception):
        await _run(_ENV, paths, factory)
    assert sends == 0 and json.loads(capsys.readouterr().out)["category"] == "unexpected_request"
