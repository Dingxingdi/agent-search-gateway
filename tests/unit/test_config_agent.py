"""Agent configuration stays independent of LLM search and fetch inheritance."""

import copy

import pytest

from agent_search_gateway.config import resolve_config
from agent_search_gateway.providers.registry import ProviderRegistry
from tests.unit.test_config_llm import _config, _environment


def test_resolve_config_builds_independent_agent_invocations() -> None:
    data = _config()
    data["agent_providers"] = {
        "primary": {"harness": "antigravity"},
        "second": {
            "harness": "antigravity",
            "command": "/opt/Agent Tools/agy",
            "max_concurrency": 2,
            "timeout_seconds": 540,
            "pass_env": ["HARNESS_SETTING"],
        },
    }
    data["search_agent"] = {
        "providers": [
            {"provider": "primary"},
            {"provider": "second", "model": " pinned "},
            {"provider": "primary"},
            {"provider": "primary", "model": "explicit"},
        ]
    }
    original = copy.deepcopy(data)
    resolved = resolve_config(data, ProviderRegistry(), _environment())
    assert hasattr(resolved, "agent"), "resolve_config must expose independent Agent configuration"
    assert [(inv.provider, inv.model) for inv in resolved.agent.search_invocations] == [
        ("primary", None),
        ("second", " pinned "),
        ("primary", None),
        ("primary", "explicit"),
    ]
    first, second = resolved.agent.providers
    assert (first.command, first.max_concurrency, first.timeout_seconds, first.pass_env) == (
        "agy",
        1,
        300.0,
        (),
    )
    assert (second.command, second.max_concurrency, second.timeout_seconds, second.pass_env) == (
        "/opt/Agent Tools/agy",
        2,
        540.0,
        ("HARNESS_SETTING",),
    )
    assert resolved.llm.search_invocations[1].model == "global-model"
    assert resolved.llm.search_invocations[0].extra_body == {"entry": {"id": 1}}
    assert data == original


@pytest.mark.parametrize("agent_enabled", [False, True])
@pytest.mark.parametrize("llm_enabled", [False, True])
def test_search_namespaces_are_independently_optional(
    agent_enabled: bool,
    llm_enabled: bool,
) -> None:
    data = _config()
    if not llm_enabled:
        data.pop("search_llm")
    if agent_enabled:
        data["agent_providers"] = {"primary": {"harness": "antigravity"}}
        data["search_agent"] = {"providers": [{"provider": "primary"}]}
    resolved = resolve_config(data, ProviderRegistry(), _environment())
    assert bool(resolved.agent.search_invocations) is agent_enabled
    assert bool(resolved.llm.search_invocations) is llm_enabled
    assert resolved.llm.judge.model == "judge-model"
    assert resolved.llm.safety.model == "fetch-model"
    assert resolved.llm.content_clean.model == "fetch-model"
    assert resolved.llm.focus_summary.model == "fetch-model"
    if agent_enabled:
        assert resolved.agent.search_invocations[0].model is None


def test_agent_config_empty_list_ignores_unreferenced_provider() -> None:
    data = _config()
    data["agent_providers"] = {"unreferenced": {"harness": "unsupported"}}
    data["search_agent"] = {"providers": []}
    resolved = resolve_config(data, ProviderRegistry(), _environment())
    assert resolved.agent.search_invocations == ()
    assert resolved.agent.providers == ()


def _agent_data(**provider_values: object) -> dict[str, object]:
    data = _config()
    data["agent_providers"] = {"agent": {"harness": "antigravity", **provider_values}}
    data["search_agent"] = {"providers": [{"provider": "agent"}]}
    return data


def test_agent_config_rejects_unknown_harness() -> None:
    from agent_search_gateway.errors import ConfigFailure, ErrorCode

    with pytest.raises(ConfigFailure) as caught:
        resolve_config(_agent_data(harness="unknown"), ProviderRegistry(), _environment())
    assert caught.value.code is ErrorCode.CONFIG_ERROR


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("harness", ""),
        ("harness", 1),
        ("command", ""),
        ("command", 1),
        ("command", "agy --flag"),
        ("command", "/opt/agy --flag"),
        ("command", "agy | tee"),
        ("command", "agy;echo secret"),
        ("command", "/opt/agy\n"),
        ("command", "$(agy)"),
        ("command", "`agy`"),
        ("command", "/opt/$HOME/agy"),
        ("command", "/opt/agy > out"),
        ("max_concurrency", 0),
        ("max_concurrency", -1),
        ("max_concurrency", True),
        ("max_concurrency", 1.5),
        ("timeout_seconds", False),
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", float("inf")),
        ("timeout_seconds", float("-inf")),
        ("timeout_seconds", 0),
        ("timeout_seconds", -1),
        ("timeout_seconds", 540.1),
        ("pass_env", "NAME"),
        ("pass_env", [1]),
        ("pass_env", [""]),
        ("pass_env", ["X=secret"]),
        ("pass_env", ["1BAD"]),
        ("pass_env", ["A-B"]),
        ("extra_body", {}),
        ("api_key", "secret-value"),
    ],
)
def test_agent_config_rejects_invalid_provider_fields(key: str, value: object) -> None:
    from agent_search_gateway.errors import ConfigFailure

    with pytest.raises(ConfigFailure) as caught:
        resolve_config(_agent_data(**{key: value}), ProviderRegistry(), _environment())
    assert "secret-value" not in str(caught.value)


@pytest.mark.parametrize(
    "entry",
    [
        None,
        "agent",
        {},
        {"provider": ""},
        {"provider": 1},
        {"provider": "unknown"},
        *({"provider": "agent", "model": value} for value in ("", " ", None, True, 1, ["bad"])),
        {"provider": "agent", "extra_body": {}},
    ],
)
def test_agent_config_rejects_invalid_invocation(entry: object) -> None:
    from agent_search_gateway.errors import ConfigFailure

    data = _agent_data()
    data["search_agent"] = {"providers": [entry]}
    with pytest.raises(ConfigFailure):
        resolve_config(data, ProviderRegistry(), _environment())


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("search_agent", None),
        ("search_agent", []),
        ("search_agent", {"providers": {}}),
        ("search_agent", {"providers": None}),
        ("search_agent", {"unknown": True}),
        ("agent_providers", []),
        ("agent_providers", {"agent": None}),
        ("agent_providers", {"agent": {}}),
    ],
)
def test_agent_config_rejects_invalid_namespace(key: str, value: object) -> None:
    from agent_search_gateway.errors import ConfigFailure

    data = _agent_data()
    data[key] = value
    with pytest.raises(ConfigFailure):
        resolve_config(data, ProviderRegistry(), _environment())


@pytest.mark.parametrize("timeout", [0.5, 300, 540])
@pytest.mark.parametrize("concurrency", [1, 2])
def test_agent_valid_numeric_boundaries(timeout: float, concurrency: int) -> None:
    data = _agent_data(timeout_seconds=timeout, max_concurrency=concurrency, pass_env=["_OK", "A2"])
    config = resolve_config(data, ProviderRegistry(), _environment())
    assert config.agent.providers[0].timeout_seconds == timeout
    assert config.agent.providers[0].max_concurrency == concurrency


def test_agent_search_does_not_remove_fetch_requirements_or_global_fallback() -> None:
    from agent_search_gateway.errors import ConfigFailure

    data = _agent_data()
    data.pop("fetch_llm")
    config = resolve_config(data, ProviderRegistry(), _environment())
    assert config.llm.safety.model == "global-model"
    data.pop("global_default_llm")
    with pytest.raises(ConfigFailure):
        resolve_config(data, ProviderRegistry(), _environment())
