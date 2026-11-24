"""Offline checks for the Baidu live smoke using the production HTTP and adapter stack."""

import asyncio
import json
import logging
from collections.abc import Callable

import httpx
import pytest

from tests.integration.test_live_baiduqianfan import _run

_Handler = Callable[[httpx.Request], httpx.Response]
_ENV = {"BAIDUQIANFAN_API_KEY": "test-key"}


def _client(handler: _Handler) -> Callable[[], httpx.AsyncClient]:
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _valid(stage: str, items: list[dict[str, str]]) -> dict[str, object]:
    if stage == "web_search":
        return {"references": items}
    return {"web_page_result": {"web_pages": items}}


@pytest.mark.parametrize("bad_stage", ["web_search", "agentic_search"])
@pytest.mark.parametrize(
    ("status", "payload"),
    [(403, {}), (200, {"code": "InvalidArgument"}), (200, {"unexpected": []})],
    ids=["forbidden", "business-error", "invalid-envelope"],
)
async def test_baidu_smoke_requires_both_branches_to_be_verified(
    bad_stage: str, status: int, payload: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        stage = request.url.path.rsplit("/", 1)[-1]
        if stage == bad_stage:
            return httpx.Response(status, json=payload)
        return httpx.Response(200, json=_valid(stage, [{"url": "https://example.test/hit"}]))

    with pytest.raises(pytest.fail.Exception, match="both Baidu search branches"):
        await _run(_ENV, _client(respond))
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(reports) == 2
    for item in reports:
        assert item["attempts"] == 1
        if item["stage"] == bad_stage:
            assert item["http_status"] == status
            assert item["category"] in {"http_status", "provider_or_schema"}
            assert item["hits"] is None
        else:
            assert item["category"] == "verified"
            assert item["http_status"] == 200
            assert item["hits"] == 1


async def test_baidu_smoke_accepts_verified_empty_results(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        stage = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=_valid(stage, []))

    await _run(_ENV, _client(respond))
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [
        (
            item["stage"],
            item["category"],
            item["attempts"],
            item["http_status"],
            item["received"],
            item["hits"],
        )
        for item in reports
    ] == [
        ("web_search", "verified", 1, 200, 0, 0),
        ("agentic_search", "verified", 1, 200, 0, 0),
    ]


@pytest.mark.parametrize("stage", ["web_search", "agentic_search"])
@pytest.mark.parametrize("include_valid", [False, True], ids=["all-rejected", "mixed"])
async def test_baidu_smoke_checks_nonempty_branch_candidates(
    stage: str, include_valid: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [{"renamed_url": "https://example.test/malformed"}]
    if include_valid:
        items.append({"url": "https://example.test/valid"})

    def respond(request: httpx.Request) -> httpx.Response:
        requested = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=_valid(requested, items if requested == stage else []))

    if include_valid:
        await _run(_ENV, _client(respond))
    else:
        with pytest.raises(pytest.fail.Exception, match="both Baidu search branches"):
            await _run(_ENV, _client(respond))

    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    report = next(row for row in reports if row["stage"] == stage)
    assert report["category"] == ("verified" if include_valid else "provider_or_schema")
    assert (report["received"], report["hits"]) == (len(items), int(include_valid))


@pytest.mark.parametrize("both_fail", [False, True])
async def test_baidu_smoke_reports_only_safe_metrics(
    both_fail: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    key = "fake-key"
    body = "private-body"
    exception = "private-exception"
    signed_url = "https://example.test/hit?signature=private-signature"

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/web_search"):
            if both_fail:
                raise ValueError(exception)
            return httpx.Response(200, json=_valid("web_search", [{"url": signed_url}]))
        return httpx.Response(200, json={"code": "InvalidArgument", "message": body})

    with pytest.raises(pytest.fail.Exception) as failure:
        await _run({"BAIDUQIANFAN_API_KEY": key}, _client(respond))
    capture = capsys.readouterr()
    for sentinel in (key, body, exception, signed_url, "private-signature"):
        assert sentinel not in capture.out + capture.err + str(failure.value)
    assert str(failure.value) == "Could not verify both Baidu search branches"
    reports = [json.loads(line) for line in capture.out.splitlines()]
    assert len(reports) == 2
    for item in reports:
        assert set(item) == {
            "stage",
            "category",
            "attempts",
            "http_status",
            "received",
            "hits",
            "known_code",
            "elapsed_ms",
        }
        assert type(item["elapsed_ms"]) is int and item["elapsed_ms"] >= 0
    if not both_fail:
        assert reports[0]["hits"] == 1
        assert reports[1]["known_code"] == "InvalidArgument"


async def test_baidu_smoke_skips_missing_credentials_without_network() -> None:
    def forbidden_client() -> httpx.AsyncClient:
        pytest.fail("missing credentials constructed a client")

    with pytest.raises(pytest.skip.Exception, match="BAIDUQIANFAN_API_KEY"):
        await _run({}, forbidden_client)


async def test_baidu_smoke_restores_logger_after_branch_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = logging.getLogger("agent_search_gateway.providers.web.baiduqianfan")
    monkeypatch.setattr(logger, "disabled", True)
    before = (logger.level, tuple(logger.handlers), logger.propagate, logger.disabled)

    debug_enabled = []

    def fail_branch(request: httpx.Request) -> httpx.Response:
        debug_enabled.append(logger.isEnabledFor(logging.DEBUG))
        raise ValueError("unavailable branch")

    with pytest.raises(pytest.fail.Exception, match="both Baidu search branches"):
        await _run(_ENV, _client(fail_branch))
    assert debug_enabled == [True, True]
    assert (logger.level, tuple(logger.handlers), logger.propagate, logger.disabled) == before


async def test_baidu_smoke_bounds_long_retry_after(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("tests.integration.test_live_baiduqianfan._BUDGET_SECONDS", 0.1)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/web_search"):
            return httpx.Response(429, headers={"Retry-After": "3600"}, json={})
        return httpx.Response(200, json=_valid("agentic_search", []))

    # A regression must also terminate the offline test promptly.
    with pytest.raises(pytest.fail.Exception, match="both Baidu search branches"):
        await asyncio.wait_for(_run(_ENV, _client(respond)), timeout=3)
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(row["stage"], row["attempts"], row["http_status"]) for row in reports] == [
        ("web_search", 1, 429),
        ("agentic_search", 1, 200),
    ]
