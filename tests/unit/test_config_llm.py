import copy
from collections.abc import Callable
from dataclasses import FrozenInstanceError

import pytest

from agent_search_gateway.config import resolve_config, resolve_llm_config
from agent_search_gateway.errors import ConfigFailure, ErrorCode
from agent_search_gateway.providers.registry import ProviderRegistry


def _config() -> dict[str, object]:
    return {
        "llm_providers": {
            "primary": {
                "protocol": "openai",
                "api_endpoint": "chat_completions",
                "credentials": [
                    {"api_url": "https://primary.example.test", "api_key_env": "LLM_A_ENV"}
                ],
            },
            "secondary": {
                "protocol": "openai",
                "api_endpoint": "chat_completions",
                "credentials": [
                    {
                        "api_url": "https://secondary.example.test",
                        "api_key_env": "LLM_B_ENV",
                        "max_concurrency": 4,
                    }
                ],
            },
        },
        "global_default_llm": {
            "provider": "primary",
            "model": "global-model",
            "extra_body": {"scope": {"name": "global"}},
        },
        "search_llm": {
            "providers": [
                {
                    "provider": "primary",
                    "model": "search-one",
                    "extra_body": {"entry": {"id": 1}},
                },
                {"provider": "secondary"},
            ]
        },
        "fetch_llm": {
            "provider": "secondary",
            "model": "fetch-model",
            "extra_body": {"scope": {"name": "fetch"}},
            "judge": {
                "model": "judge-model",
                "extra_body": {"scope": {"name": "judge"}},
            },
            "safety": {},
            "content_clean": {"provider": "primary"},
            "focus_summary": {},
        },
    }


def _environment() -> dict[str, str]:
    return {"LLM_A_ENV": "x", "LLM_B_ENV": "y"}


def test_resolve_llm_config_preserves_independent_search_entries_and_fetch_inheritance() -> None:
    data = _config()
    original = copy.deepcopy(data)
    resolved = resolve_llm_config(data, _environment())

    assert [
        (provider.name, provider.credentials[0].max_concurrency) for provider in resolved.providers
    ] == [("primary", None), ("secondary", 4)]
    assert [invocation.provider for invocation in resolved.search_invocations] == [
        "primary",
        "secondary",
    ]
    assert [invocation.model for invocation in resolved.search_invocations] == [
        "search-one",
        "global-model",
    ]
    assert resolved.search_invocations[0].extra_body == {"entry": {"id": 1}}
    assert resolved.search_invocations[1].extra_body == {"scope": {"name": "global"}}

    assert resolved.judge.provider == "secondary"
    assert resolved.judge.model == "judge-model"
    assert resolved.judge.extra_body == {"scope": {"name": "judge"}}
    assert resolved.safety.model == "fetch-model"
    assert resolved.safety.extra_body == {"scope": {"name": "fetch"}}
    assert resolved.content_clean.provider == "primary"
    assert resolved.content_clean.model == "fetch-model"
    assert resolved.focus_summary.provider == "secondary"

    assert data == original
    global_table = data["global_default_llm"]
    assert isinstance(global_table, dict)
    global_table["extra_body"] = {"mutated": True}
    assert resolved.search_invocations[1].extra_body == {"scope": {"name": "global"}}
    assert resolved.search_invocations[1].extra_body is not resolved.focus_summary.extra_body


def test_referenced_llm_alias_resolves_ordered_credentials() -> None:
    data = {
        "llm_providers": {
            "primary": {
                "protocol": "openai",
                "api_endpoint": "chat_completions",
                "credentials": [
                    {"api_url": "https://first.example.test", "api_key_env": "LLM_A_ENV"},
                    {
                        "api_url": "https://second.example.test",
                        "api_key_env": "LLM_B_ENV",
                        "max_concurrency": 4,
                        "rate_limits": [{"requests": 3, "period_seconds": 2}],
                        "min_interval_seconds": 0.5,
                    },
                ],
            },
        },
        "global_default_llm": {"provider": "primary", "model": "global-model"},
        "search_llm": {"providers": [{"provider": "primary", "model": "search-model"}]},
    }
    original = copy.deepcopy(data)
    resolved = resolve_llm_config(data, _environment())
    assert len(resolved.providers) == 1
    provider = resolved.providers[0]
    assert (provider.name, provider.protocol, provider.api_endpoint) == (
        "primary",
        "openai",
        "chat_completions",
    )
    first, second = provider.credentials
    assert [member.options["api_url"] for member in provider.credentials] == [
        "https://first.example.test",
        "https://second.example.test",
    ]
    assert [member.api_key_env for member in provider.credentials] == ["LLM_A_ENV", "LLM_B_ENV"]
    assert first.api_key is not None and second.api_key is not None
    assert [first.api_key.reveal(), second.api_key.reveal()] == ["x", "y"]
    assert first.api_key is not second.api_key
    assert first.max_concurrency is None
    assert first.rate_policy.windows == ()
    assert first.rate_policy.min_interval_seconds is None
    assert second.max_concurrency == 4
    assert [(window.requests, window.period_seconds) for window in second.rate_policy.windows] == [
        (3, 2.0)
    ]
    assert second.rate_policy.min_interval_seconds == 0.5
    assert resolved.search_invocations[0].model == "search-model"
    assert resolved.judge.provider == provider.name
    assert resolved.judge.model == "global-model"
    assert data == original


def _primary(data: dict[str, object]) -> dict[str, object]:
    tables = data["llm_providers"]
    assert isinstance(tables, dict)
    table = tables["primary"]
    assert isinstance(table, dict)
    return table


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, "round_robin"),
        ({"credential_strategy": "round_robin"}, "round_robin"),
        ({"credential_strategy": "fill_first"}, "fill_first"),
    ],
)
def test_llm_credential_strategy_defaults_and_valid_values(
    fields: dict[str, object], expected: str
) -> None:
    data = _config()
    _primary(data).update(fields)
    assert resolve_llm_config(data, _environment()).providers[0].credential_strategy == expected


@pytest.mark.parametrize("key", ["credentials_strategy", "max_concurency"])
def test_referenced_llm_alias_rejects_unknown_options(key: str) -> None:
    data = _config()
    _primary(data)[key] = "fill_first"

    with pytest.raises(ConfigFailure, match=r"llm_providers.primary.*unknown") as caught:
        resolve_llm_config(data, _environment())

    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert "fill_first" not in str(caught.value)


@pytest.mark.parametrize("value", ["fill_out", "unknown", "", None, True, 1, [], {}])
def test_llm_credential_strategy_rejects_invalid_values(value: object) -> None:
    data = _config()
    _primary(data)["credential_strategy"] = value
    with pytest.raises(ConfigFailure, match=r"llm_providers.primary.credential_strategy"):
        resolve_llm_config(data, _environment())


@pytest.mark.parametrize(
    "key",
    [
        "api_url",
        "api_key_env",
        "api_key",
        "contact_email_env",
        "contact_email",
        "secret",
        "max_concurrency",
        "rate_limits",
        "min_interval_seconds",
        "default_max_concurrency",
    ],
)
@pytest.mark.parametrize("referenced", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
def test_llm_legacy_alias_fields_are_rejected(key: str, referenced: bool, mixed: bool) -> None:
    data = _config()
    table = _primary(data) if referenced else {}
    if not referenced:
        tables = data["llm_providers"]
        assert isinstance(tables, dict)
        tables["unused"] = table
    if not mixed:
        table.pop("credentials", None)
    table[key] = "[REDACTED_SECRET]"
    with pytest.raises(ConfigFailure) as caught:
        resolve_llm_config(data, _environment())
    alias = "primary" if referenced else "unused"
    assert f"llm_providers.{alias}.{key}" in str(caught.value)
    assert "removed" in str(caught.value)
    assert "[REDACTED_SECRET]" not in str(caught.value)


def test_llm_legacy_category_default_is_rejected() -> None:
    data = _config()
    tables = data["llm_providers"]
    assert isinstance(tables, dict)
    tables["default_max_concurrency"] = 2
    with pytest.raises(ConfigFailure, match=r"llm_providers.default_max_concurrency.*removed"):
        resolve_llm_config(data, _environment())


@pytest.mark.parametrize("value", [None, [], {}, "bad", True, [None], [{}]])
def test_referenced_llm_credentials_require_nonempty_tables(value: object) -> None:
    data = _config()
    _primary(data)["credentials"] = value
    with pytest.raises(ConfigFailure, match=r"llm_providers.primary.credentials"):
        resolve_llm_config(data, _environment())


@pytest.mark.parametrize(
    "key",
    [
        "model",
        "protocol",
        "api_endpoint",
        "use_proxy",
        "credentials",
        "credential_strategy",
        "default_max_concurrency",
        "api_key",
        "secret",
        "contact_email_env",
        "unknown",
    ],
)
def test_llm_credentials_reject_unknown_fields(key: str) -> None:
    data = _config()
    members = _primary(data)["credentials"]
    assert isinstance(members, list)
    members[0][key] = "[REDACTED_SECRET]"
    with pytest.raises(ConfigFailure, match=r"llm_providers.primary.credentials\[0\]") as caught:
        resolve_llm_config(data, _environment())
    if key == "default_max_concurrency":
        assert "removed" in str(caught.value)


@pytest.mark.parametrize(
    "path",
    [
        ("global_default_llm",),
        ("search_llm",),
        ("search_llm", "providers", 0),
        ("fetch_llm",),
        *(("fetch_llm", stage) for stage in ("judge", "safety", "content_clean", "focus_summary")),
    ],
)
def test_llm_legacy_default_is_rejected_in_invocation_tables(path: tuple[str | int, ...]) -> None:
    data = _config()
    table: object = data
    for key in path:
        if isinstance(key, int):
            assert isinstance(table, list)
            table = table[key]
        else:
            assert isinstance(table, dict)
            table = table[key]
    assert isinstance(table, dict)
    table["default_max_concurrency"] = 2
    with pytest.raises(ConfigFailure) as caught:
        resolve_llm_config(data, _environment())
    assert "default_max_concurrency" in str(caught.value)
    assert "removed" in str(caught.value)


def test_llm_extra_body_and_unused_fields_keep_their_existing_boundary() -> None:
    data = _config()
    global_table = data["global_default_llm"]
    assert isinstance(global_table, dict)
    global_table["extra_body"] = {"default_max_concurrency": 3, "credentials": "vendor-payload"}
    tables = data["llm_providers"]
    assert isinstance(tables, dict)
    tables["unused"] = {
        "protocol": None,
        "credentials": [{"api_key_env": "MISSING_ENV"}],
        "credential_strategy": "invalid",
        "use_proxy": None,
        "unknown": True,
    }
    resolved = resolve_llm_config(data, _environment())
    assert resolved.search_invocations[1].extra_body == {
        "default_max_concurrency": 3,
        "credentials": "vendor-payload",
    }
    assert [provider.name for provider in resolved.providers] == ["primary", "secondary"]


_INVALID_INTEGER_LIMITS: tuple[object, ...] = (
    None,
    0,
    -1,
    True,
    False,
    1.5,
    "2",
    [],
    {},
    float("nan"),
    float("inf"),
)
_INVALID_SECONDS_LIMITS: tuple[object, ...] = (
    None,
    0,
    -1,
    True,
    False,
    "2",
    [],
    {},
    float("nan"),
    float("inf"),
    float("-inf"),
    10**400,
)
_INVALID_WINDOW_CONTAINERS: tuple[object, ...] = (None, {}, "bad", True, [None], [[]])
_INVALID_EXPLICIT_LIMITS: list[tuple[str, object, str]] = [
    *(("max_concurrency", value, "max_concurrency") for value in _INVALID_INTEGER_LIMITS),
    *(("min_interval_seconds", value, "min_interval_seconds") for value in _INVALID_SECONDS_LIMITS),
    *(("rate_limits", value, "rate_limits") for value in _INVALID_WINDOW_CONTAINERS),
    ("rate_limits", [{}], "rate_limits[0].requests"),
    ("rate_limits", [{"requests": 1}], "rate_limits[0].period_seconds"),
    ("rate_limits", [{"requests": 1, "period_seconds": 1, "unknown": True}], "rate_limits[0]"),
    *(
        ("rate_limits", [{"requests": value, "period_seconds": 1}], "rate_limits[0].requests")
        for value in _INVALID_INTEGER_LIMITS
    ),
    *(
        ("rate_limits", [{"requests": 1, "period_seconds": value}], "rate_limits[0].period_seconds")
        for value in _INVALID_SECONDS_LIMITS
    ),
]


@pytest.mark.parametrize(("key", "value", "field"), _INVALID_EXPLICIT_LIMITS)
def test_llm_invalid_explicit_limits_report_credential_field_path(
    key: str, value: object, field: str
) -> None:
    data = _config()
    members = _primary(data)["credentials"]
    assert isinstance(members, list)
    members.append(
        {
            "api_url": "https://second.example.test/private-endpoint",
            "api_key_env": "LLM_B_ENV",
            key: value,
        }
    )
    with pytest.raises(ConfigFailure) as caught:
        resolve_llm_config(data, _environment())
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert f"llm_providers.primary.credentials[1].{field}" in str(caught.value)
    assert "private-endpoint" not in str(caught.value)


@pytest.mark.parametrize("capacity", [False, True])
@pytest.mark.parametrize("window", [False, True])
@pytest.mark.parametrize("interval", [False, True])
def test_llm_credential_limits_are_independently_optional(
    capacity: bool, window: bool, interval: bool
) -> None:
    data = _config()
    members = _primary(data)["credentials"]
    assert isinstance(members, list)
    member = members[0]
    member["rate_limits"] = []
    if capacity:
        member["max_concurrency"] = 7
    if window:
        member["rate_limits"] = [{"requests": 2, "period_seconds": 3}]
    if interval:
        member["min_interval_seconds"] = 0.25
    resolved = resolve_llm_config(data, _environment()).providers[0].credentials[0]
    assert resolved.max_concurrency == (7 if capacity else None)
    assert [(rule.requests, rule.period_seconds) for rule in resolved.rate_policy.windows] == (
        [(2, 3.0)] if window else []
    )
    assert resolved.rate_policy.min_interval_seconds == (0.25 if interval else None)


@pytest.mark.parametrize("key", ["api_url", "api_key_env"])
@pytest.mark.parametrize("value", [None, "", " ", True, 1, [], {}])
def test_llm_credentials_require_endpoint_and_auth_strings(key: str, value: object) -> None:
    data = _config()
    members = _primary(data)["credentials"]
    assert isinstance(members, list)
    members[0][key] = value
    with pytest.raises(ConfigFailure) as caught:
        resolve_llm_config(data, _environment())
    assert f"llm_providers.primary.credentials[0].{key}" in str(caught.value)


def test_referenced_llm_validates_each_credential_environment() -> None:
    data = _config()
    members = _primary(data)["credentials"]
    assert isinstance(members, list)
    members.append({"api_url": "https://second.example.test", "api_key_env": "MISSING_ENV"})
    with pytest.raises(ConfigFailure, match=r"llm_providers.primary.credentials\[1\].api_key_env"):
        resolve_llm_config(data, _environment())


Mutator = Callable[[dict[str, object]], None]


def _missing_provider(data: dict[str, object]) -> None:
    search = data["search_llm"]
    assert isinstance(search, dict)
    providers = search["providers"]
    assert isinstance(providers, list)
    providers.append({"provider": "missing"})


def _bad_protocol(data: dict[str, object]) -> None:
    providers = data["llm_providers"]
    assert isinstance(providers, dict)
    primary = providers["primary"]
    assert isinstance(primary, dict)
    primary["protocol"] = "anthropic"


def _bad_endpoint(data: dict[str, object]) -> None:
    providers = data["llm_providers"]
    assert isinstance(providers, dict)
    primary = providers["primary"]
    assert isinstance(primary, dict)
    primary["api_endpoint"] = "responses"


def _bad_concurrency(data: dict[str, object]) -> None:
    providers = data["llm_providers"]
    assert isinstance(providers, dict)
    primary = providers["primary"]
    assert isinstance(primary, dict)
    credentials = primary["credentials"]
    assert isinstance(credentials, list)
    credentials[0]["max_concurrency"] = 0


@pytest.mark.parametrize(
    "mutate",
    [_missing_provider, _bad_protocol, _bad_endpoint, _bad_concurrency],
)
def test_resolve_llm_config_rejects_invalid_referenced_provider(mutate: Mutator) -> None:
    data = _config()
    mutate(data)
    with pytest.raises(ConfigFailure) as caught:
        resolve_llm_config(data, _environment())
    assert caught.value.code is ErrorCode.CONFIG_ERROR


def test_resolve_llm_config_allows_empty_search_list() -> None:
    data = _config()
    search = data["search_llm"]
    assert isinstance(search, dict)
    search["providers"] = []
    resolved = resolve_llm_config(data, _environment())
    assert resolved.search_invocations == ()


@pytest.mark.parametrize(
    "value",
    [None, False, 1, "synthetic_input_limit_sentinel", [], {"unknown": 1024}],
)
def test_input_limits_reject_non_tables_and_unknown_keys(value: object) -> None:
    data = _config()
    data["input_limits"] = value

    with pytest.raises(ConfigFailure, match="input_limits") as caught:
        resolve_config(data, ProviderRegistry(), _environment())

    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert "synthetic_input_limit_sentinel" not in str(caught.value)


@pytest.mark.parametrize("key", ["llm_request_body_bytes", "agent_prompt_bytes"])
@pytest.mark.parametrize(
    "value", [True, False, 0, -1, 1.5, "synthetic_input_limit_sentinel", None, [], {}]
)
def test_input_limits_reject_invalid_byte_budgets(key: str, value: object) -> None:
    data = _config()
    data["input_limits"] = {key: value}

    with pytest.raises(
        ConfigFailure, match=rf"input_limits\.{key} must be a positive integer"
    ) as caught:
        resolve_config(data, ProviderRegistry(), _environment())

    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert "synthetic_input_limit_sentinel" not in str(caught.value)


def test_input_limits_default_to_128_kib_llm_and_96_kib_agent() -> None:
    resolved = resolve_config(_config(), ProviderRegistry(), _environment())

    assert hasattr(resolved, "input_limits"), "resolve_config must expose input byte budgets"
    assert resolved.input_limits.llm_request_body_bytes == 131072
    assert resolved.input_limits.agent_prompt_bytes == 98304


@pytest.mark.parametrize("limits", [{}, {"llm_request_body_bytes": 2048}])
def test_input_limits_preserve_legacy_resolved_config_construction(
    limits: dict[str, object],
) -> None:
    data = _config()
    data["input_limits"] = limits
    resolved = resolve_config(data, ProviderRegistry(), _environment())

    legacy = type(resolved)(resolved.web, resolved.llm, resolved.retry)

    assert legacy.input_limits.llm_request_body_bytes == 131072
    assert legacy.input_limits.agent_prompt_bytes == 98304
    for key in ("llm_request_body_bytes", "agent_prompt_bytes"):
        with pytest.raises(FrozenInstanceError):
            setattr(legacy.input_limits, key, 1)


def test_input_limits_customize_llm_budget_without_changing_agent_default() -> None:
    data = _config()
    data["input_limits"] = {"llm_request_body_bytes": 2048}
    original = copy.deepcopy(data)

    resolved = resolve_config(data, ProviderRegistry(), _environment())

    assert resolved.input_limits.llm_request_body_bytes == 2048
    assert resolved.input_limits.agent_prompt_bytes == 98304
    assert resolved.llm.search_invocations[0].extra_body == {"entry": {"id": 1}}
    assert resolved.llm.judge.extra_body == {"scope": {"name": "judge"}}
    assert data == original


@pytest.mark.parametrize(
    ("proxy_fields", "expected"),
    [({}, True), ({"use_proxy": True}, True), ({"use_proxy": False}, False)],
)
def test_llm_provider_use_proxy_defaults_and_boolean_values(
    proxy_fields: dict[str, object], expected: bool
) -> None:
    data = _config()
    tables = data["llm_providers"]
    assert isinstance(tables, dict)
    for name in ("primary", "secondary"):
        table = tables[name]
        assert isinstance(table, dict)
        table.update(proxy_fields)
    resolved = resolve_llm_config(data, _environment())
    assert [provider.use_proxy for provider in resolved.providers] == [expected, expected]
    invocations = (
        *resolved.search_invocations,
        resolved.judge,
        resolved.safety,
        resolved.content_clean,
        resolved.focus_summary,
    )
    assert all("use_proxy" not in invocation.extra_body for invocation in invocations)
    assert resolved.search_invocations[0].extra_body == {"entry": {"id": 1}}
    assert resolved.judge.extra_body == {"scope": {"name": "judge"}}


@pytest.mark.parametrize("name", ["primary", "secondary"])
@pytest.mark.parametrize("value", ["false", "do-not-echo-proxy", 0, 1, None, [], {}, ()])
def test_llm_provider_use_proxy_is_strict_boolean(name: str, value: object) -> None:
    data = _config()
    tables = data["llm_providers"]
    assert isinstance(tables, dict)
    table = tables[name]
    assert isinstance(table, dict)
    table["use_proxy"] = value
    with pytest.raises(ConfigFailure) as caught:
        resolve_llm_config(data, _environment())
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert f"llm_providers.{name}.use_proxy" in str(caught.value)
    assert "do-not-echo-proxy" not in str(caught.value)


@pytest.mark.parametrize("value", ["false", 0, 1, None, [], {}])
def test_unreferenced_llm_provider_use_proxy_remains_ignored(value: object) -> None:
    data = _config()
    tables = data["llm_providers"]
    assert isinstance(tables, dict)
    tables["unused"] = {"use_proxy": value}
    resolved = resolve_llm_config(data, _environment())
    assert [provider.name for provider in resolved.providers] == ["primary", "secondary"]


def test_llm_provider_use_proxy_and_credentials_are_frozen() -> None:
    from agent_search_gateway.config import LLMProviderConfig, ResolvedCredential
    from agent_search_gateway.observability import SecretValue

    credential = ResolvedCredential(
        options={"api_url": "https://provider.example.test"},
        api_key=SecretValue("[REDACTED_SECRET]"),
        max_concurrency=7,
    )
    provider = LLMProviderConfig("primary", "openai", "chat_completions", (credential,))
    assert provider.credentials == (credential,)
    assert provider.credentials[0].max_concurrency == 7
    assert provider.use_proxy is True
    assert provider.credential_strategy == "round_robin"
    assert "https://provider.example.test" not in repr(provider)
    with pytest.raises(FrozenInstanceError):
        provider.use_proxy = False  # type: ignore[misc]
    with pytest.raises(TypeError):
        provider.credentials[0].options["api_url"] = "https://changed.example.test"  # type: ignore[index]
