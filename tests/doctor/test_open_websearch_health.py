import asyncio
import gzip
import json
from collections.abc import AsyncIterator
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from agent_search_gateway import doctor
from agent_search_gateway.config import load_toml
from agent_search_gateway.doctor import DoctorCheck, DoctorStatus, _probe_open_websearch_health
from agent_search_gateway.paths import RuntimePaths
from tests.doctor._support import environment_name, write_valid_config


def _member_health(check: DoctorCheck) -> DoctorCheck:
    return DoctorCheck(
        check.status, f"web_providers.open_websearch.credentials[0]: {check.message}"
    )


@pytest.mark.parametrize(
    ("search", "fetch", "options", "expected_url"),
    [
        (True, False, {}, "http://127.0.0.1:3210"),
        (False, True, {"api_url": "https://example.test/base/"}, "https://example.test/base/"),
        (True, True, {}, "http://127.0.0.1:3210"),
        (True, False, {"api_url": None}, None),
    ],
)
async def test_doctor_probes_enabled_open_websearch_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    search: bool,
    fetch: bool,
    options: dict[str, object],
    expected_url: str | None,
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    write_valid_config(paths.config_file)
    # TOML has no null literal. Supply parsed data so explicit None reaches the real resolver.
    data = load_toml(paths.config_file)
    data["web_providers"] = {
        "open_websearch": {"enable_search": search, "enable_fetch": fetch, "credentials": [options]}
    }
    monkeypatch.setattr(doctor, "load_toml", lambda path: data)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": "ok", "data": {"daemon": "running"}})
    )
    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=transport))
    probe = AsyncMock(wraps=_probe_open_websearch_health)
    monkeypatch.setattr(doctor, "_probe_open_websearch_health", probe)

    report = await doctor.run_doctor(paths, environ={environment_name(): "present"})

    if expected_url is None:
        probe.assert_not_awaited()
        assert report.exit_code == 1
        assert report.checks[0].status is DoctorStatus.FAIL
        assert "web_providers.open_websearch.credentials[0]" in report.checks[0].message
        return
    probe.assert_awaited_once_with(expected_url)
    expected = (
        DoctorCheck(DoctorStatus.OK, "open-webSearch daemon healthy")
        if expected_url is not None
        else DoctorCheck(DoctorStatus.FAIL, "open-webSearch daemon unhealthy (invalid_health_url)")
    )
    assert report.checks[-2] == _member_health(expected)


@pytest.mark.parametrize("healthy", [False, True])
async def test_doctor_health_failure_is_not_hidden_by_missing_gateway_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, healthy: bool
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    write_valid_config(paths.config_file)
    with paths.config_file.open("a") as target:
        target.write(
            "[web_providers.open_websearch]\nenable_search = true\n"
            "[[web_providers.open_websearch.credentials]]\n"
        )
    status = DoctorStatus.OK if healthy else DoctorStatus.FAIL
    check = DoctorCheck(status, "open-webSearch health result")
    monkeypatch.setattr(doctor, "_probe_open_websearch_health", AsyncMock(return_value=check))

    report = await doctor.run_doctor(paths, environ={environment_name(): "present"})

    assert report.checks[-2:] == (
        _member_health(check),
        DoctorCheck(DoctorStatus.INFO, "daemon not running"),
    )
    assert report.exit_code == (0 if healthy else 1)


@pytest.mark.parametrize(
    ("code", "headers", "body", "reason"),
    [
        (200, {"Content-Encoding": "gzip"}, b"invalid gzip", "invalid_health_response"),
        (503, {"Content-Encoding": "gzip"}, b"invalid gzip", "503"),
        (302, {"Location": "https://example.test:invalid"}, b"", "302"),
    ],
)
async def test_doctor_reports_malformed_health_response_and_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    headers: dict[str, str],
    body: bytes,
    reason: str,
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    write_valid_config(paths.config_file)
    with paths.config_file.open("a") as target:
        target.write(
            "[web_providers.open_websearch]\nenable_search = true\n"
            "[[web_providers.open_websearch.credentials]]\n"
        )

    response = httpx.Response(code, headers=headers, stream=httpx.ByteStream(body))
    handler = Mock(return_value=response)
    make_client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
    clients: list[httpx.AsyncClient] = []

    def create_client(**kwargs: Any) -> httpx.AsyncClient:
        client = make_client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", create_client)
    report = await doctor.run_doctor(paths, environ={environment_name(): "present"})

    assert report.checks[-2:] == (
        _member_health(
            DoctorCheck(DoctorStatus.FAIL, f"open-webSearch daemon unhealthy ({reason})")
        ),
        DoctorCheck(DoctorStatus.INFO, "daemon not running"),
    )
    assert report.exit_code == 1
    handler.assert_called_once()
    assert (response.request.method, str(response.request.url)) == (
        "GET",
        "http://127.0.0.1:3210/health",
    )
    (client,) = clients
    assert response.is_closed
    assert client.is_closed


@pytest.mark.parametrize(
    ("code", "payload", "expected"),
    [
        (200, {"status": "ok", "data": {"daemon": "running"}}, DoctorStatus.OK),
        (201, {"status": "ok", "data": {"daemon": "running"}}, DoctorStatus.FAIL),
        (200, {"status": "error", "data": {"daemon": "running"}}, DoctorStatus.FAIL),
        (200, {"status": "ok", "data": {"daemon": "stopped"}}, DoctorStatus.FAIL),
        (200, {"status": "ok", "data": []}, DoctorStatus.FAIL),
        (200, [], DoctorStatus.FAIL),
    ],
)
async def test_health_accepts_exact_success_envelope(
    monkeypatch: pytest.MonkeyPatch, code: int, payload: object, expected: DoctorStatus
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(code, json=payload)

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=transport))
    result = await _probe_open_websearch_health("http://example.test")

    assert [(r.method, str(r.url)) for r in requests] == [("GET", "http://example.test/health")]
    assert result.status is expected
    if expected is DoctorStatus.OK:
        assert result == DoctorCheck(DoctorStatus.OK, "open-webSearch daemon healthy")


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("http://example.test", "http://example.test/health"),
        ("http://example.test/", "http://example.test/health"),
        (" http://example.test ", "http://example.test/health"),
        (" https://example.test/prefix/  ", "https://example.test/prefix/health"),
        ("https://example.test:8443/service", "https://example.test:8443/service/health"),
        ("https://example.test/service/", "https://example.test/service/health"),
        ("https://example.test/app%2Fapi/?q=x#y", "https://example.test/app%2Fapi/health"),
    ],
)
async def test_health_preserves_path_prefix_and_strips_query_fragment(
    monkeypatch: pytest.MonkeyPatch, base: str, expected: str
) -> None:
    urls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"status": "ok", "data": {"daemon": "running"}})

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=transport))
    await _probe_open_websearch_health(base)

    assert urls == [expected]


@pytest.mark.parametrize(
    "base",
    [
        None,
        42,
        "",
        "  ",
        "ftp://example.test",
        "http:///service",
        "http://:80",
        "http://exa mple.test",
        "http://example.test\t",
        "http://exa\tmple.test",
        "http://exa\x7fmple.test",
        "http://example.test:no",
        "http://example.test:99999",
        "http://[invalid",
        "http://[::1]invalid",
        "http://999.999.999.999",
        "http://a@example.test",
        "http://a:b@example.test",
        "http://@example.test",
    ],
)
async def test_health_rejects_invalid_url_and_userinfo_without_client(
    monkeypatch: pytest.MonkeyPatch, base: object
) -> None:
    def unexpected_client(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid configuration constructed an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_client)
    result = await _probe_open_websearch_health(base)

    assert result == DoctorCheck(
        DoctorStatus.FAIL, "open-webSearch daemon unhealthy (invalid_health_url)"
    )


async def test_health_sends_no_credentials_body_or_redirected_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) > 1:
            pytest.fail("probe followed a redirect")
        return httpx.Response(302, headers={"Location": "https://other.example.test/next"})

    transport = httpx.MockTransport(redirect)
    factory = Mock(wraps=partial(httpx.AsyncClient, transport=transport))
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    result = await _probe_open_websearch_health("https://example.test?q=x#y")

    assert result.status is DoctorStatus.FAIL
    (request,) = requests
    assert request.method == "GET"
    assert str(request.url) == "https://example.test/health"
    assert not {"authorization", "proxy-authorization", "cookie"}.intersection(request.headers)
    assert request.content == b""
    factory.assert_called_once_with(
        follow_redirects=False,
        trust_env=False,
        timeout=2.0,
        event_hooks={"response": [doctor._check_health_status]},
    )


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"fake-body",
        b"\xffbad",
        b"[]",
        b'{"data":{"daemon":"running"}}',
        b'{"status":"ok"}',
        b'{"status":"ok","data":{}}',
        b'{"status":"fake","data":{"daemon":"running"}}',
        b'{"status":true,"data":{"daemon":"running"}}',
        b'{"status":"ok","data":[]}',
        b'{"status":"ok","data":{"daemon":"fake"}}',
        b'{"status":"ok","data":{"daemon":true}}',
    ],
)
async def test_health_requires_exact_success_envelope(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=transport))

    result = await _probe_open_websearch_health("https://example.test")

    assert result == DoctorCheck(
        DoctorStatus.FAIL, "open-webSearch daemon unhealthy (invalid_health_response)"
    )


@pytest.mark.parametrize("extra", [0, 1], ids=["at-limit", "over-limit"])
async def test_doctor_limits_decoded_health_body_and_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: int
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    write_valid_config(paths.config_file)
    with paths.config_file.open("a") as target:
        target.write(
            "[web_providers.open_websearch]\nenable_search = true\n"
            "[[web_providers.open_websearch.credentials]]\n"
        )

    envelope = b'{"status":"ok","data":{"daemon":"running"}}'
    compressed = gzip.compress(envelope.ljust(64 * 1024 + extra))
    consumed_tail = False

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal consumed_tail
            yield compressed[:10]
            yield compressed[10:]
            consumed_tail = True
            yield b""

    response = httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=Chunks())
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    report = await doctor.run_doctor(paths, environ={environment_name(): "present"})

    expected = (
        DoctorCheck(DoctorStatus.FAIL, "open-webSearch daemon unhealthy (invalid_health_response)")
        if extra
        else DoctorCheck(DoctorStatus.OK, "open-webSearch daemon healthy")
    )
    assert report.checks[-2:] == (
        _member_health(expected),
        DoctorCheck(DoctorStatus.INFO, "daemon not running"),
    )
    assert consumed_tail is (extra == 0)
    assert response.is_closed and client.is_closed


async def test_health_rejects_success_if_parsing_crosses_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(200, json={"status": "ok", "data": {"daemon": "running"}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    loop = asyncio.get_running_loop()
    start = loop.time()
    clock = Mock(return_value=start)
    monkeypatch.setattr(loop, "time", clock)
    parse = json.loads

    def slow_parse(*args: Any, **kwargs: Any) -> Any:
        clock.return_value = start + 3
        return parse(*args, **kwargs)

    monkeypatch.setattr(json, "loads", slow_parse)
    result = await _probe_open_websearch_health("https://example.test")
    assert result == DoctorCheck(DoctorStatus.FAIL, "open-webSearch daemon unhealthy (timeout)")
    assert response.is_closed and client.is_closed


async def test_health_handles_recursive_json_decode_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(200, content=b"[" * 2000 + b"]" * 2000)
    transport = httpx.MockTransport(lambda request: response)
    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=transport))

    result = await _probe_open_websearch_health("https://example.test")

    assert result == DoctorCheck(
        DoctorStatus.FAIL, "open-webSearch daemon unhealthy (invalid_health_response)"
    )


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (httpx.ConnectError("fake-detail"), "connection_failed"),
        (httpx.TimeoutException("fake-detail"), "timeout"),
        (httpx.RemoteProtocolError("fake-detail"), "connection_failed"),
        *((code, str(code)) for code in (201, 204, 301, 302, 307, 401, 404, 429, 500)),
    ],
)
async def test_health_classifies_transport_timeout_and_http_failures_safely(
    monkeypatch: pytest.MonkeyPatch, outcome: httpx.TransportError | int, reason: str
) -> None:
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert attempts == 1
        if isinstance(outcome, httpx.TransportError):
            raise outcome
        return httpx.Response(outcome, text="fake-body", headers={"Location": "/fake-next"})

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=transport))

    result = await _probe_open_websearch_health("https://example.test/fake-path")

    assert attempts == 1
    assert result == DoctorCheck(DoctorStatus.FAIL, f"open-webSearch daemon unhealthy ({reason})")


@pytest.fixture
def blocked_health_request(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[httpx.AsyncClient, asyncio.Event, list[httpx.Request]]:
    entered = asyncio.Event()
    requests: list[httpx.Request] = []

    async def wait_for_response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("blocked health response unexpectedly resumed")

    client = httpx.AsyncClient(transport=httpx.MockTransport(wait_for_response), trust_env=False)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    return client, entered, requests


async def test_health_has_total_deadline_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    blocked_health_request: tuple[httpx.AsyncClient, asyncio.Event, list[httpx.Request]],
) -> None:
    client, _, requests = blocked_health_request
    monkeypatch.setattr("agent_search_gateway.doctor._OPEN_WEBSEARCH_HEALTH_TIMEOUT", 0.02)

    async with asyncio.timeout(0.5) as watchdog:
        result = await _probe_open_websearch_health("https://example.test")

    assert not watchdog.expired()
    assert result == DoctorCheck(DoctorStatus.FAIL, "open-webSearch daemon unhealthy (timeout)")
    assert len(requests) == 1
    assert client.is_closed


async def test_health_cancellation_propagates_and_closes_client(
    blocked_health_request: tuple[httpx.AsyncClient, asyncio.Event, list[httpx.Request]],
) -> None:
    client, entered, requests = blocked_health_request
    async with asyncio.timeout(0.5):
        task = asyncio.create_task(_probe_open_websearch_health("https://example.test"))
        await entered.wait()
        task.cancel("cancel-health")
        with pytest.raises(asyncio.CancelledError, match="cancel-health"):
            await task

    assert len(requests) == 1
    assert client.is_closed


async def test_doctor_checks_each_open_websearch_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    write_valid_config(paths.config_file)
    with paths.config_file.open("a") as target:
        target.write(
            "[web_providers.open_websearch]\nenable_search = true\nenable_fetch = true\n"
            '[[web_providers.open_websearch.credentials]]\napi_url = "https://healthy.example.test/base"\n'
            '[[web_providers.open_websearch.credentials]]\napi_url = "https://failed.example.test/base"\n'
        )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "healthy.example.test":
            return httpx.Response(200, json={"status": "ok", "data": {"daemon": "running"}})
        return httpx.Response(
            503, text="synthetic-private-body", headers={"X-Private": "synthetic-header"}
        )

    monkeypatch.setattr(
        httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(respond))
    )
    report = await doctor.run_doctor(paths, environ={environment_name(): "synthetic-key"})
    assert [str(request.url) for request in requests] == [
        "https://healthy.example.test/base/health",
        "https://failed.example.test/base/health",
    ]
    assert report.exit_code == 1
    assert report.checks[-3:-1] == (
        DoctorCheck(
            DoctorStatus.OK,
            "web_providers.open_websearch.credentials[0]: open-webSearch daemon healthy",
        ),
        DoctorCheck(
            DoctorStatus.FAIL,
            "web_providers.open_websearch.credentials[1]: open-webSearch daemon unhealthy (503)",
        ),
    )
    assert all(
        value not in repr(report)
        for value in (
            "synthetic-key",
            "synthetic-private-body",
            "synthetic-header",
            "https://failed.example.test",
        )
    )
