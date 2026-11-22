import asyncio
import logging
import os
import shutil
import ssl
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import httpx
import pytest

from agent_search_gateway.config import ResolvedConfig, resolve_config
from agent_search_gateway.errors import ConfigFailure, ErrorCode, ExecutionFailure
from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.academic.arxiv import ArxivProvider
from agent_search_gateway.providers.academic.defaults import (
    build_default_academic_registry,
    build_default_oa_resolver_registry,
)
from agent_search_gateway.providers.academic.unpaywall import UnpaywallResolver
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.llm.openai_chat import OpenAIChatCompletionsClient
from agent_search_gateway.providers.web.firecrawl import FirecrawlAdapter
from agent_search_gateway.providers.web.zhihu import ZhihuAdapter
from agent_search_gateway.runtime import Runtime
from tests.runtime.test_runtime_assembly import _config, _environment


def _proxy_config(*direct: str, disabled_direct: bool = False) -> ResolvedConfig:
    raw = _config()
    web = raw["web_providers"]
    llm = raw["llm_providers"]
    assert isinstance(web, dict) and isinstance(llm, dict)
    web["brave"]["use_proxy"] = "brave" not in direct
    web["tavily"]["use_proxy"] = "tavily" not in direct
    web["firecrawl"]["use_proxy"] = not disabled_direct
    llm["primary"]["use_proxy"] = "primary" not in direct
    llm["unreferenced"] = {"use_proxy": not disabled_direct}
    raw["academic_providers"] = {
        "arxiv": {
            "enabled": "arxiv" in direct,
            "use_proxy": "arxiv" not in direct and not disabled_direct,
        }
    }
    raw["oa_resolvers"] = {
        "unpaywall": {
            "enabled": "unpaywall" in direct,
            "use_proxy": "unpaywall" not in direct and not disabled_direct,
            "contact_email_env": "TEST_CONTACT",
        }
    }
    return resolve_config(
        raw,
        build_default_registry(),
        {**_environment(), "TEST_CONTACT": "test@example.org"},
        academic_registry=build_default_academic_registry(),
        oa_resolver_registry=build_default_oa_resolver_registry(),
    )


def _mock_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))


@pytest.fixture(autouse=True)
def _clear_ca_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)


async def _direct_contexts(tmp_path: Path, *direct: str) -> list[ssl.SSLContext]:
    calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> httpx.AsyncClient:
        calls.append(kwargs)
        return _mock_client()

    runtime = Runtime.build(
        _proxy_config(*direct), RuntimePaths.from_home(tmp_path), http_client_factory=factory
    )
    await runtime.aclose()
    contexts = []
    for kwargs in calls:
        if kwargs:
            assert kwargs.keys() == {"trust_env", "verify"}
            assert kwargs["trust_env"] is False
            context = kwargs["verify"]
            assert isinstance(context, ssl.SSLContext)
            assert context.verify_mode == ssl.CERT_REQUIRED
            assert context.check_hostname
            contexts.append(context)
    assert len(contexts) == len(direct)
    return contexts


@pytest.mark.parametrize("direct", ["brave", "arxiv", "unpaywall", "primary"])
async def test_families_direct_factory_uses_verified_ssl_context(
    tmp_path: Path, direct: str
) -> None:
    await _direct_contexts(tmp_path, direct)


async def test_direct_factory_shares_verified_ssl_context(tmp_path: Path) -> None:
    first, second = await _direct_contexts(tmp_path, "tavily", "brave")
    assert first is second


@pytest.mark.parametrize("with_directory", [False, True])
async def test_ssl_cert_file_loads_only_selected_ca(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_directory: bool
) -> None:
    ca = httpx.create_ssl_context(trust_env=False).get_ca_certs(binary_form=True)[0]
    bundle = tmp_path / "selected-ca"
    bundle.write_text(ssl.DER_cert_to_PEM_cert(ca), encoding="ascii")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    if with_directory:
        monkeypatch.setenv("SSL_CERT_DIR", os.pathsep.join((str(tmp_path), str(bundle))))

    (context,) = await _direct_contexts(tmp_path, "brave")

    assert context.get_ca_certs(binary_form=True) == [ca]


@pytest.mark.parametrize("parts", [("ca",), ("ca", "more"), ("", "ca", "", "more", ""), ("", "")])
async def test_ssl_cert_directory_passed_as_capath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parts: tuple[str, ...]
) -> None:
    (tmp_path / "ca").mkdir()
    (tmp_path / "more").mkdir()
    directories = os.pathsep.join(str(tmp_path / part) if part else "" for part in parts)
    monkeypatch.setenv("SSL_CERT_DIR", directories)
    loaded_directories: dict[ssl.SSLContext, list[str]] = {}
    load_locations = ssl.SSLContext.load_verify_locations

    def record_locations(
        context: ssl.SSLContext,
        cafile: str | None = None,
        capath: str | None = None,
        cadata: str | bytes | None = None,
    ) -> None:
        if capath is not None:
            loaded_directories.setdefault(context, []).append(capath)
        load_locations(context, cafile, capath, cadata)

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", record_locations)
    (context,) = await _direct_contexts(tmp_path, "brave")

    assert loaded_directories[context] == [directories]
    assert context.get_ca_certs() == []


@pytest.mark.parametrize("direct", ["brave", "arxiv", "unpaywall", "primary"])
@pytest.mark.parametrize(
    "invalid",
    ["missing-file", "broken-file", "missing-dir", "not-dir", "list-missing", "list-file"],
)
def test_invalid_ssl_ca_fails_before_any_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str, direct: str
) -> None:
    path = tmp_path / "private-ca-path"
    if invalid in {"broken-file", "not-dir", "list-file"}:
        path.write_text("invalid-certificate-sentinel", encoding="ascii")
    if invalid.startswith("list-"):
        monkeypatch.setenv("SSL_CERT_DIR", os.pathsep.join((str(tmp_path), str(path))))
    else:
        variable = "SSL_CERT_FILE" if invalid.endswith("file") else "SSL_CERT_DIR"
        monkeypatch.setenv(variable, str(path))
    monkeypatch.setenv("HTTPS_PROXY", "http://user:proxy-sentinel@proxy.invalid")
    factory = Mock(side_effect=_mock_client)

    with pytest.raises(ConfigFailure) as caught:
        Runtime.build(
            _proxy_config(direct), RuntimePaths.from_home(tmp_path), http_client_factory=factory
        )

    assert caught.value.code == ErrorCode.CONFIG_ERROR
    for marker in (str(path), "private-ca-path", "invalid-certificate-sentinel", "proxy-sentinel"):
        assert marker not in str(caught.value)
    factory.assert_not_called()


def test_direct_factory_rejects_zero_argument_factory_without_fallback(tmp_path: Path) -> None:
    calls = 0

    def factory() -> httpx.AsyncClient:
        nonlocal calls
        calls += 1
        return _mock_client()

    with pytest.raises(ConfigFailure) as caught:
        Runtime.build(
            _proxy_config("tavily"), RuntimePaths.from_home(tmp_path), http_client_factory=factory
        )

    assert caught.value.code == ErrorCode.CONFIG_ERROR
    assert calls == 0


@pytest.mark.parametrize("disabled_direct", [False, True])
async def test_proxy_factory_needs_no_arguments_or_prepared_ssl_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disabled_direct: bool
) -> None:
    create_context = Mock(side_effect=AssertionError("unexpected direct context"))
    monkeypatch.setattr(httpx, "create_ssl_context", create_context)
    factory = Mock(side_effect=_mock_client)

    runtime = Runtime.build(
        _proxy_config(disabled_direct=disabled_direct),
        RuntimePaths.from_home(tmp_path),
        http_client_factory=factory,
    )
    await runtime.aclose()

    assert factory.called
    assert all(not call.args and not call.kwargs for call in factory.call_args_list)
    create_context.assert_not_called()


def _mixed_config() -> ResolvedConfig:
    raw = _config()
    raw["web_providers"] = {
        "zhihu": {
            "enable_search": True,
            "api_key_env": "ENV_A",
            "use_proxy": False,
        },
        "firecrawl": {
            "enable_search": True,
            "enable_fetch": True,
            "api_key_env": "ENV_B",
            "api_url": "https://firecrawl.example.test",
            "use_proxy": True,
        },
        "brave": {"enable_search": False, "use_proxy": False},
    }
    raw["academic_providers"] = {
        "arxiv": {"enabled": True, "use_proxy": True},
        "openalex": {"enabled": False, "use_proxy": False},
    }
    raw["oa_resolvers"] = {
        "unpaywall": {"enabled": True, "contact_email_env": "TEST_CONTACT", "use_proxy": False},
    }
    llm = raw["llm_providers"]
    assert isinstance(llm, dict)
    llm["primary"]["use_proxy"] = False
    llm["secondary"]["use_proxy"] = True
    llm["unreferenced"] = {"use_proxy": False}
    raw["retry"] = {"max_attempts": 2, "base_delay_seconds": 0.001, "max_delay_seconds": 0.001}
    return resolve_config(
        raw,
        build_default_registry(),
        {**_environment(), "TEST_CONTACT": "test@example.org"},
        academic_registry=build_default_academic_registry(),
        oa_resolver_registry=build_default_oa_resolver_registry(),
    )


def _mixed_executors(runtime: Runtime) -> dict[str, HttpJsonExecutor]:
    zhihu, firecrawl = runtime.web_search_providers
    (arxiv,) = runtime.academic_search_providers
    assert isinstance(zhihu, ZhihuAdapter)
    assert isinstance(firecrawl, FirecrawlAdapter)
    assert isinstance(arxiv, ArxivProvider)
    assert isinstance(runtime.oa_resolver, UnpaywallResolver)
    executors = {
        "web:zhihu": cast(HttpJsonExecutor, zhihu._http),
        "web:firecrawl": cast(HttpJsonExecutor, firecrawl._http),
        "academic:arxiv": cast(HttpJsonExecutor, arxiv._executor),
        "oa:unpaywall": cast(HttpJsonExecutor, runtime.oa_resolver._executor),
    }
    for name, client in runtime.llm_clients.items():
        assert isinstance(client, OpenAIChatCompletionsClient)
        executors[f"llm:{name}"] = client._executor
    return executors


class _RecordedClient(httpx.AsyncClient):
    def __init__(self, kwargs: dict[str, object]) -> None:
        self.factory_kwargs = kwargs
        self.close_calls = 0
        self.request_count = 0
        self.fail = False
        super().__init__(
            transport=httpx.MockTransport(self._respond),
            trust_env=cast(bool, kwargs.get("trust_env", True)),
            verify=cast(ssl.SSLContext | bool, kwargs.get("verify", True)),
        )

    def _respond(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        if self.fail:
            raise httpx.ConnectError("controlled failure", request=request)
        return httpx.Response(200, json={"ok": True})

    async def aclose(self) -> None:
        self.close_calls += 1
        await super().aclose()


def _recorded_runtime(
    tmp_path: Path, config: ResolvedConfig
) -> tuple[Runtime, list[_RecordedClient]]:
    created: list[_RecordedClient] = []

    def factory(**kwargs: object) -> httpx.AsyncClient:
        client = _RecordedClient(kwargs)
        created.append(client)
        return client

    runtime = Runtime.build(config, RuntimePaths.from_home(tmp_path), http_client_factory=factory)
    return runtime, created


async def test_families_mixed_modes_bind_each_client_to_its_provider(tmp_path: Path) -> None:
    runtime, created = _recorded_runtime(tmp_path, _mixed_config())
    try:
        executors = _mixed_executors(runtime)
        expected = {
            "web:zhihu": False,
            "web:firecrawl": True,
            "academic:arxiv": True,
            "oa:unpaywall": False,
            "llm:primary": False,
            "llm:secondary": True,
        }
        assert executors.keys() == expected.keys()
        assert {executor._client for executor in executors.values()} == set(created)
        assert len(created) == len(expected)
        contexts = []
        for name, executor in executors.items():
            client = cast(_RecordedClient, executor._client)
            if expected[name]:
                assert client.factory_kwargs == {}
            else:
                assert client.factory_kwargs.keys() == {"trust_env", "verify"}
                assert client.factory_kwargs["trust_env"] is False
                contexts.append(client.factory_kwargs["verify"])
            assert await executor.request_json(
                "GET", "https://provider.example.test", stage="test"
            ) == {"ok": True}
            assert client.request_count == 1
        assert isinstance(contexts[0], ssl.SSLContext)
        assert all(context is contexts[0] for context in contexts)
        assert len(runtime.web_fetch_providers) == 1
        assert cast(object, runtime.web_fetch_providers[0]) is runtime.web_search_providers[1]
        assert executors["web:firecrawl"] is runtime._web_http_executors[1]
    finally:
        await runtime.aclose()


async def test_isolation_connect_error_keeps_clients_modes_and_retry_policy(tmp_path: Path) -> None:
    config = _mixed_config()
    runtime, created = _recorded_runtime(tmp_path, config)
    try:
        executors = _mixed_executors(runtime)
        bindings = {name: executor._client for name, executor in executors.items()}
        modes = [dict(client.factory_kwargs) for client in created]
        failing = cast(_RecordedClient, bindings["web:zhihu"])
        failing.fail = True

        with pytest.raises(ExecutionFailure) as caught:
            await executors["web:zhihu"].request_json(
                "GET", "https://provider.example.test", stage="test"
            )

        assert caught.value.code == ErrorCode.ALL_PROVIDERS_FAILED
        assert failing.request_count == config.retry.max_attempts
        assert len(created) == len(bindings)
        assert [client.factory_kwargs for client in created] == modes
        assert {
            name: executor._client for name, executor in _mixed_executors(runtime).items()
        } == bindings
        for name, executor in executors.items():
            assert executor._retry_policy is config.retry
            if name != "web:zhihu":
                assert await executor.request_json(
                    "GET", "https://provider.example.test", stage="test"
                ) == {"ok": True}
                assert cast(_RecordedClient, bindings[name]).request_count == 1
    finally:
        await runtime.aclose()


async def test_close_mixed_runtime_twice_closes_each_client_once(tmp_path: Path) -> None:
    runtime, created = _recorded_runtime(tmp_path, _mixed_config())
    await runtime.aclose()
    await runtime.aclose()

    assert len(created) == 6
    assert all(client.close_calls == 1 and client.is_closed for client in created)


async def test_logging_reports_namespaced_boolean_modes_without_proxy_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(variable, "http://proxy-user:proxy-password@proxy.example.test:8080")
    with caplog.at_level(logging.DEBUG, logger="agent_search_gateway.runtime"):
        runtime, _ = _recorded_runtime(tmp_path, _mixed_config())
        await runtime.aclose()

    records = [
        record
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "runtime_built"
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    fields = cast(dict[str, object], records[0].__dict__["gateway_fields"])
    expected = {
        "web_use_proxy": "zhihu:false,firecrawl:true",
        "academic_use_proxy": "arxiv:true",
        "oa_use_proxy": "unpaywall:false",
        "llm_use_proxy": "primary:false,secondary:true",
    }
    assert {key: fields[key] for key in expected} == expected
    logged = KeyValueFormatter(SecretRedactor(())).format(records[0])
    for namespace, modes in expected.items():
        assert f"{namespace}={modes}" in logged
    logged += caplog.text + repr(fields)
    for marker in ("proxy-user", "proxy-password", "proxy.example.test", "http://"):
        assert marker not in logged


@asynccontextmanager
async def _routing_server(
    body: bytes, tls: ssl.SSLContext | None = None
) -> AsyncIterator[tuple[str, list[str]]]:
    request_lines: list[str] = []
    tasks: list[asyncio.Task[None]] = []
    writers: set[asyncio.StreamWriter] = set()

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(3):
                headers = await reader.readuntil(b"\r\n\r\n")
                request_lines.append(headers.split(b"\r\n", 1)[0].decode("ascii"))
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode("ascii")
                    + b"Connection: close\r\n\r\n"
                    + body
                )
                await writer.drain()
        finally:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=3)

    def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.add(writer)
        tasks.append(asyncio.create_task(respond(reader, writer)))

    server = await asyncio.wait_for(
        asyncio.start_server(connected, "127.0.0.1", 0, ssl=tls), timeout=3
    )
    try:
        port = server.sockets[0].getsockname()[1]
        scheme = "https" if tls else "http"
        yield f"{scheme}://127.0.0.1:{port}", request_lines
    finally:
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=3)
        for writer in writers:
            writer.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3)
        await asyncio.wait_for(
            asyncio.gather(*(writer.wait_closed() for writer in writers)), timeout=3
        )
        assert all(
            outcome is None or isinstance(outcome, asyncio.CancelledError) for outcome in outcomes
        )


def _localhost_certificate(tmp_path: Path, *, with_ip_san: bool) -> tuple[Path, ssl.SSLContext]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("Local TLS certificate generation requires the OpenSSL executable")
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    cert = trusted / "localhost.pem"
    key = tmp_path / "localhost.key"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            *(["-addext", "subjectAltName=IP:127.0.0.1"] if with_ip_san else []),
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,digitalSignature,keyCertSign",
            "-out",
            str(cert),
            "-keyout",
            str(key),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run([openssl, "rehash", str(trusted)], check=True, capture_output=True)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    return trusted, server_context


async def test_ssl_cert_directory_trusts_certificate_from_second_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    trusted, server_context = _localhost_certificate(tmp_path, with_ip_san=True)
    monkeypatch.setenv("SSL_CERT_DIR", os.pathsep.join((str(empty), str(trusted))))
    (context,) = await _direct_contexts(tmp_path, "brave")
    async with (
        _routing_server(b"verified", tls=server_context) as (url, _),
        httpx.AsyncClient(verify=context, trust_env=False) as client,
    ):
        response = await client.get(url)
        assert response.text == "verified"


async def test_ssl_cert_directory_list_preserves_httpx_common_name_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted, server_context = _localhost_certificate(tmp_path, with_ip_san=False)
    monkeypatch.setenv("SSL_CERT_DIR", str(trusted))
    baseline = httpx.create_ssl_context(verify=True, trust_env=True)
    accepted = []
    async with _routing_server(b"verified", tls=server_context) as (url, _):
        localhost = httpx.URL(url).copy_with(host="localhost")
        for directories in (str(trusted), os.pathsep.join((str(trusted), str(trusted)))):
            monkeypatch.setenv("SSL_CERT_DIR", directories)
            (context,) = await _direct_contexts(tmp_path, "brave")
            async with httpx.AsyncClient(verify=context, trust_env=False) as client:
                try:
                    response = await client.get(localhost)
                except httpx.ConnectError as exc:
                    assert "hostname mismatch" in str(exc).lower()
                    accepted.append(False)
                else:
                    assert response.text == "verified"
                    accepted.append(True)

    assert accepted == [baseline.hostname_checks_common_name] * 2


@pytest.mark.parametrize("proxy_variable", ["HTTP_PROXY", "ALL_PROXY"])
@pytest.mark.parametrize(
    ("use_proxy", "no_proxy", "expected"),
    [
        pytest.param(True, False, "PROXY", id="environment-proxy"),
        pytest.param(False, False, "TARGET", id="direct"),
        pytest.param(True, True, "TARGET", id="no-proxy"),
    ],
)
async def test_provider_http_routing_uses_environment_or_direct_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    proxy_variable: str,
    use_proxy: bool,
    no_proxy: bool,
    expected: str,
) -> None:
    variables = tuple(
        name
        for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "REQUEST_METHOD")
        for name in (variable, variable.lower())
    )
    original_environment = {name: os.environ.get(name) for name in variables}
    async with (
        _routing_server(b"TARGET") as (target_url, target_requests),
        _routing_server(b"PROXY") as (proxy_url, proxy_requests),
    ):
        with monkeypatch.context() as environment:
            for variable in variables:
                environment.delenv(variable, raising=False)
            environment.setenv(proxy_variable, proxy_url)
            if no_proxy:
                environment.setenv("NO_PROXY", "127.0.0.1")
            raw = _config()
            raw["web_providers"] = {
                "brave": {
                    "enable_search": True,
                    "api_key_env": "ENV_B",
                    "api_url": target_url,
                    "use_proxy": use_proxy,
                }
            }
            config = resolve_config(raw, build_default_registry(), _environment())
            runtime = Runtime.build(config, RuntimePaths.from_home(tmp_path))
            try:
                (executor,) = runtime._web_http_executors
                url = f"{target_url}/routing?source=provider"
                async with asyncio.timeout(3):
                    response = await executor._client.get(url)
                assert response.status_code == 200
                assert response.text == expected
                if expected == "PROXY":
                    assert proxy_requests == [f"GET {url} HTTP/1.1"]
                    assert target_requests == []
                else:
                    assert target_requests == ["GET /routing?source=provider HTTP/1.1"]
                    assert proxy_requests == []
            finally:
                await asyncio.wait_for(runtime.aclose(), timeout=3)
    assert all(os.environ.get(name) == value for name, value in original_environment.items())
