"""Agent configuration stays independent of LLM search and fetch inheritance."""

import copy

import pytest

from agent_search_gateway.config import resolve_config
from agent_search_gateway.providers.registry import ProviderRegistry
from tests.unit.test_config_llm import _INVALID_EXPLICIT_LIMITS, _config, _environment


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
        None,
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


@pytest.mark.parametrize(
    ("limits", "llm_bytes", "agent_bytes"),
    [
        ({"agent_prompt_bytes": 1024}, 131072, 1024),
        ({"llm_request_body_bytes": 2048, "agent_prompt_bytes": 1024}, 2048, 1024),
        ({"llm_request_body_bytes": 1, "agent_prompt_bytes": 1}, 1, 1),
    ],
)
def test_input_limits_customize_agent_budget_independently(
    limits: dict[str, object], llm_bytes: int, agent_bytes: int
) -> None:
    data = _agent_data()
    data["input_limits"] = limits
    original = copy.deepcopy(data)

    resolved = resolve_config(data, ProviderRegistry(), _environment())

    assert resolved.input_limits.llm_request_body_bytes == llm_bytes
    assert resolved.input_limits.agent_prompt_bytes == agent_bytes
    assert resolved.agent.providers[0].command == "agy"
    assert resolved.agent.search_invocations[0].model is None
    assert resolved.llm.search_invocations[0].extra_body == {"entry": {"id": 1}}
    assert data == original


def test_agent_limits_are_optional_without_credentials() -> None:
    config = resolve_config(_agent_data(), ProviderRegistry(), _environment())
    provider = config.agent.providers[0]
    assert provider.max_concurrency is None
    assert provider.rate_policy.windows == ()
    assert provider.rate_policy.min_interval_seconds is None
    assert not hasattr(provider, "credentials")


@pytest.mark.parametrize("capacity", [False, True])
@pytest.mark.parametrize("window", [False, True])
@pytest.mark.parametrize("interval", [False, True])
def test_agent_limits_are_independently_optional(
    capacity: bool, window: bool, interval: bool
) -> None:
    fields: dict[str, object] = {"rate_limits": []}
    if capacity:
        fields["max_concurrency"] = 7
    if window:
        fields["rate_limits"] = [{"requests": 2, "period_seconds": 3}]
    if interval:
        fields["min_interval_seconds"] = 0.25
    provider = resolve_config(
        _agent_data(**fields), ProviderRegistry(), _environment()
    ).agent.providers[0]
    assert provider.max_concurrency == (7 if capacity else None)
    assert [(rule.requests, rule.period_seconds) for rule in provider.rate_policy.windows] == (
        [(2, 3.0)] if window else []
    )
    assert provider.rate_policy.min_interval_seconds == (0.25 if interval else None)


@pytest.mark.parametrize("level", ["category", "alias", "unused"])
def test_agent_legacy_default_max_concurrency_is_rejected(level: str) -> None:
    from agent_search_gateway.errors import ConfigFailure

    data = _agent_data()
    group = data["agent_providers"]
    assert isinstance(group, dict)
    if level == "category":
        group["default_max_concurrency"] = 1
        path = "agent_providers.default_max_concurrency"
    else:
        group["agent" if level == "alias" else "unused"] = {
            "harness": "antigravity",
            "default_max_concurrency": 1,
        }
        alias = "agent" if level == "alias" else "unused"
        path = f"agent_providers.{alias}.default_max_concurrency"
    with pytest.raises(ConfigFailure) as caught:
        resolve_config(data, ProviderRegistry(), _environment())
    assert path in str(caught.value)
    assert "removed" in str(caught.value)


@pytest.mark.parametrize("referenced", [False, True])
@pytest.mark.parametrize(
    "key",
    [
        "credentials",
        "credential_strategy",
        "api_url",
        "api_key_env",
        "api_key",
        "contact_email_env",
        "contact_email",
        "secret",
    ],
)
def test_agent_rejects_http_credentials_and_credential_strategy(key: str, referenced: bool) -> None:
    from agent_search_gateway.errors import ConfigFailure

    data = _agent_data()
    group = data["agent_providers"]
    assert isinstance(group, dict)
    alias = "agent" if referenced else "unused"
    group[alias] = {"harness": "antigravity", key: "[REDACTED_SECRET]"}
    with pytest.raises(ConfigFailure) as caught:
        resolve_config(data, ProviderRegistry(), _environment())
    assert f"agent_providers.{alias}.{key}" in str(caught.value)
    assert "[REDACTED_SECRET]" not in str(caught.value)


@pytest.mark.parametrize(("key", "value", "field"), _INVALID_EXPLICIT_LIMITS)
def test_agent_invalid_explicit_limits_report_alias_field_path(
    key: str, value: object, field: str
) -> None:
    from agent_search_gateway.errors import ConfigFailure, ErrorCode

    with pytest.raises(ConfigFailure) as caught:
        resolve_config(_agent_data(**{key: value}), ProviderRegistry(), _environment())
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert f"agent_providers.agent.{field}" in str(caught.value)


@pytest.mark.parametrize("level", ["search", "invocation"])
def test_agent_legacy_default_is_rejected_in_search_tables(level: str) -> None:
    from agent_search_gateway.errors import ConfigFailure

    data = _agent_data()
    if level == "search":
        data["search_agent"] = {"providers": [{"provider": "agent"}], "default_max_concurrency": 1}
        path = "search_agent.default_max_concurrency"
    else:
        data["search_agent"] = {"providers": [{"provider": "agent", "default_max_concurrency": 1}]}
        path = "search_agent.providers[0].default_max_concurrency"
    with pytest.raises(ConfigFailure) as caught:
        resolve_config(data, ProviderRegistry(), _environment())
    assert path in str(caught.value)
    assert "removed" in str(caught.value)


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
        ("timeout_seconds", True),
        ("timeout_seconds", None),
        ("timeout_seconds", "3600"),
        ("timeout_seconds", -0.5),
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", float("inf")),
        ("timeout_seconds", float("-inf")),
        ("timeout_seconds", 0),
        ("timeout_seconds", -1),
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


def test_agent_timeout_has_no_gateway_upper_bound() -> None:
    config = resolve_config(_agent_data(timeout_seconds=3600), ProviderRegistry(), _environment())
    assert config.agent.providers[0].timeout_seconds == 3600


@pytest.mark.parametrize("timeout", [0.5, 300, 540, 540.1, 3600, 7200])
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
