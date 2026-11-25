import asyncio
import logging
import ssl
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from agent_search_gateway import runtime
from agent_search_gateway.config import resolve_config
from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.errors import ConfigFailure
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.contracts import ProviderCapabilities
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.providers.registry import WebProviderRegistration


@pytest.mark.parametrize("use_proxy", [False, True], ids=["direct", "environment-proxy"])
async def test_http_client_factory_has_unlimited_active_connections(
    use_proxy: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:3128")
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    context = runtime._prepare_direct_ssl_context()
    factory = Mock(wraps=httpx.AsyncClient)
    async with runtime._create_http_client(
        factory, use_proxy=use_proxy, direct_ssl_context=context, label="example"
    ) as client:
        kwargs = factory.call_args.kwargs
        limits = kwargs["limits"]
        assert isinstance(limits, httpx.Limits)
        assert limits.max_connections is None
        assert limits.max_keepalive_connections == 20
        assert client.trust_env is use_proxy
        if not use_proxy:
            assert kwargs["trust_env"] is False
            assert kwargs["verify"] is context
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname

        # Inspect the selected real transport, including the environment-proxy mount.
        transport = client._transport_for_url(httpx.URL("https://gateway.example.test"))
        assert isinstance(transport, httpx.AsyncHTTPTransport)
        assert (transport is not client._transport) is use_proxy
        # httpcore represents an unlimited active connection count with sys.maxsize.
        assert transport._pool._max_connections == sys.maxsize
        assert transport._pool._max_keepalive_connections == 20


@pytest.mark.parametrize("use_proxy", [False, True])
def test_http_client_factory_never_retries_without_limits(use_proxy: bool) -> None:
    factory = Mock(side_effect=TypeError("constructor failure"))
    with pytest.raises((ConfigFailure, TypeError)):
        runtime._create_http_client(
            factory,
            use_proxy=use_proxy,
            direct_ssl_context=ssl.create_default_context(),
            label="example",
        )
    factory.assert_called_once()
    assert "limits" in factory.call_args.kwargs


async def test_resource_owner_closes_registered_clients_once() -> None:
    owner = runtime._RuntimeResources()
    first, second = (AsyncMock(spec=httpx.AsyncClient) for _ in range(2))
    assert owner.register(first) is first
    owner.register(second)
    owner.register(first)

    await owner.aclose()
    await owner.aclose()

    first.aclose.assert_awaited_once_with()
    second.aclose.assert_awaited_once_with()


async def test_resource_owner_continues_after_close_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    owner = runtime._RuntimeResources()
    clients = [AsyncMock(spec=httpx.AsyncClient) for _ in range(3)]
    clients[1].aclose.side_effect = RuntimeError("secret-token-in-close-error")
    for client in clients:
        owner.register(client)

    with caplog.at_level(logging.WARNING, logger=runtime.__name__):
        await owner.aclose()
        await owner.aclose()

    for client in clients:
        client.aclose.assert_awaited_once_with()
    assert len(caplog.records) == 1
    assert caplog.records[0].__dict__["gateway_event"] == "runtime_client_close_failed"
    assert caplog.records[0].__dict__["gateway_fields"] == {}
    assert "secret-token" not in caplog.text
    assert not caplog.records[0].exc_info


async def test_resource_owner_preserves_primary_error_if_logging_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    owner = runtime._RuntimeResources()
    clients = [AsyncMock(spec=httpx.AsyncClient) for _ in range(3)]
    clients[1].aclose.side_effect = RuntimeError("close failure")
    for client in clients:
        owner.register(client)
    failing_emit = Mock(side_effect=OSError("log failure"))
    monkeypatch.setattr(caplog.handler, "emit", failing_emit)

    original = ValueError("startup failure")
    with pytest.raises(ValueError) as caught:
        try:
            raise original
        finally:
            await owner.aclose()

    failing_emit.assert_called_once()
    assert caught.value is original
    for client in clients:
        client.aclose.assert_awaited_once_with()


@pytest.mark.parametrize(
    "failure",
    [
        "second-adapter",
        "second-client",
        "llm-client",
        "close-failure",
        "cancel-build",
        "bind-failure",
        "success",
    ],
)
async def test_partial_build_and_startup_error_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    registry = build_default_registry()
    clients = []
    primary = ValueError("synthetic constructor failure")

    def adapter(**kwargs: object) -> object:
        if len(clients) == 2 and failure in {"second-adapter", "close-failure"}:
            raise primary
        if len(clients) == 1 and failure == "cancel-build":
            asyncio.current_task().cancel()
        return Mock(name="adapter")

    def make_client(**kwargs: object) -> httpx.AsyncClient:
        if (len(clients), failure) in {(1, "second-client"), (2, "llm-client")}:
            raise primary
        client = AsyncMock(spec=httpx.AsyncClient)
        if failure == "close-failure":
            client.aclose.side_effect = RuntimeError("synthetic close failure")
        clients.append(client)
        return client

    registry.register(
        WebProviderRegistration(
            "mock", ProviderCapabilities(True, False), adapter, frozenset(), requires_api_key=False
        )
    )
    config = resolve_config(
        {
            "web_providers": {"mock": {"enable_search": True, "credentials": [{}, {}]}},
            "llm_providers": {
                "local": {
                    "protocol": "openai",
                    "api_endpoint": "chat_completions",
                    "credentials": [{"api_url": "https://example.test", "api_key_env": "ENV_D"}],
                }
            },
            "global_default_llm": {"provider": "local", "model": "test"},
        },
        registry,
        {"ENV_D": "example"},
    )
    paths = RuntimePaths.from_home(tmp_path)

    async def build() -> runtime.Runtime:
        return await runtime.Runtime.build(
            config, paths, registry=registry, http_client_factory=make_client
        )

    if failure in {"bind-failure", "success"}:
        built = await build()
        if failure == "bind-failure":
            daemon = ForegroundDaemon(paths, runtime_factory=lambda: built)
            monkeypatch.setattr(
                asyncio, "start_unix_server", AsyncMock(side_effect=OSError("synthetic bind error"))
            )
            with pytest.raises(ConfigFailure, match="Failed to bind"):
                await daemon.start()
        await built.aclose()
        await built.aclose()
        assert len(clients) == 3
    elif failure == "cancel-build":
        with pytest.raises(asyncio.CancelledError):
            await asyncio.create_task(build())
        assert len(clients) == 1
    else:
        with pytest.raises(ValueError) as exc:
            await build()
        assert exc.value is primary
        assert len(clients) == (1 if failure == "second-client" else 2)
    for client in clients:
        client.aclose.assert_awaited_once_with()


async def test_resource_owner_joins_close_during_repeated_cancellation() -> None:
    owner = runtime._RuntimeResources()
    started, resume = asyncio.Event(), asyncio.Event()
    finished = []

    async def close() -> None:
        started.set()
        await resume.wait()
        finished.append(True)

    client = AsyncMock(spec=httpx.AsyncClient)
    client.aclose.side_effect = close
    owner.register(client)
    task = asyncio.create_task(owner.aclose())
    await started.wait()
    other = asyncio.create_task(owner.aclose())
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    assert not other.done()
    resume.set()
    await other
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == [True]
    client.aclose.assert_awaited_once()
