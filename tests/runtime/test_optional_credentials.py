from pathlib import Path

import pytest

from agent_search_gateway.config import resolve_config
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.contracts import ProviderCapabilities
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.providers.registry import WebProviderRegistration
from agent_search_gateway.runtime import Runtime
from tests.runtime.test_runtime_assembly import _config, _CountingAsyncClient, _environment
from tests.support.fakes import FakeKeywordSearchProvider


@pytest.mark.parametrize("with_key", [False, True])
async def test_runtime_injects_optional_secret_only_when_present(
    tmp_path: Path, with_key: bool
) -> None:
    captured: dict[str, object] = {}

    def create_provider(**kwargs: object) -> FakeKeywordSearchProvider:
        captured.update(kwargs)
        return FakeKeywordSearchProvider("optional", [])

    registry = build_default_registry()
    registry.register(
        WebProviderRegistration(
            name="optional",
            capabilities=ProviderCapabilities(search=True, fetch=False),
            factory=create_provider,
            allowed_config_keys=frozenset(),
            requires_api_key=False,
            supports_optional_api_key=True,
        )
    )
    options: dict[str, object] = {"enable_search": True}
    if with_key:
        options["api_key_env"] = "ENV_A"
    raw = _config()
    raw["web_providers"] = {"optional": options}
    resolved = resolve_config(raw, registry, _environment())
    runtime = Runtime.build(
        resolved,
        RuntimePaths.from_home(tmp_path),
        registry=registry,
        http_client_factory=_CountingAsyncClient,
    )
    try:
        if with_key:
            assert resolved.web.providers[0].secret is not None
            assert captured["secret"] is resolved.web.providers[0].secret
        else:
            assert "secret" not in captured
    finally:
        await runtime.aclose()
