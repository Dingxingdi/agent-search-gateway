from collections import UserString

import pytest

from agent_search_gateway.config import resolve_web_provider_config
from agent_search_gateway.errors import ConfigFailure, ErrorCode
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import ProviderCapabilities
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.providers.registry import ProviderRegistry, WebProviderRegistration


@pytest.mark.parametrize("capacity", [None, 2])
@pytest.mark.parametrize("raw_windows", [None, [], [{"requests": 4, "period_seconds": 1.5}]])
@pytest.mark.parametrize("interval", [None, 0.25])
def test_credential_limits_are_independently_optional(
    capacity: int | None,
    raw_windows: list[dict[str, object]] | None,
    interval: float | None,
) -> None:
    from agent_search_gateway.config import _resolve_credential_limits

    table: dict[str, object] = {}
    if capacity is not None:
        table["max_concurrency"] = capacity
    if raw_windows is not None:
        table["rate_limits"] = raw_windows
    if interval is not None:
        table["min_interval_seconds"] = interval

    actual_capacity, policy = _resolve_credential_limits(table, "web_providers.dual.credentials[1]")
    assert actual_capacity == capacity
    assert tuple((w.requests, w.period_seconds) for w in policy.windows) == (
        ((4, 1.5),) if raw_windows else ()
    )
    assert policy.min_interval_seconds == interval


@pytest.mark.parametrize(
    ("field", "value", "error_field"),
    [
        *(("max_concurrency", v, "max_concurrency") for v in (None, 0, -1, True, 1.5, "2")),
        *(
            ("min_interval_seconds", v, "min_interval_seconds")
            for v in (None, 0, -1, True, float("inf"), float("nan"), "synthetic-secret")
        ),
        ("rate_limits", None, "rate_limits"),
        ("rate_limits", {}, "rate_limits"),
        ("rate_limits", (), "rate_limits"),
        ("rate_limits", "https://private.invalid", "rate_limits"),
        ("rate_limits", [False], "rate_limits[0]"),
        ("rate_limits", [{"period_seconds": 1}], "rate_limits[0].requests"),
        ("rate_limits", [{"requests": 1}], "rate_limits[0].period_seconds"),
        ("rate_limits", [{"synthetic-secret": 1}], "rate_limits[0]"),
        *(
            ("rate_limits", [{"requests": v, "period_seconds": 1}], "rate_limits[0].requests")
            for v in (0, -1, True, 1.5)
        ),
        *(
            ("rate_limits", [{"requests": 1, "period_seconds": v}], "rate_limits[0].period_seconds")
            for v in (0, -1, True, float("inf"), float("nan"))
        ),
    ],
)
def test_invalid_explicit_limits_report_field_path(
    field: str, value: object, error_field: str
) -> None:
    from agent_search_gateway.config import _resolve_credential_limits

    label = "web_providers.dual.credentials[1]"
    with pytest.raises(ConfigFailure) as caught:
        _resolve_credential_limits({field: value}, label)
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    message = str(caught.value)
    assert f"{label}.{error_field}" in message
    assert "synthetic-secret" not in message
    assert "private.invalid" not in message


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ({}, "round_robin"),
        ({"credential_strategy": "round_robin"}, "round_robin"),
        ({"credential_strategy": "fill_first"}, "fill_first"),
        ({"credential_strategy": "fill_out"}, None),
        ({"credential_strategy": "https://private.invalid/key"}, None),
        ({"credential_strategy": UserString("fill_first")}, None),
        ({"credential_strategy": True}, None),
        ({"credential_strategy": []}, None),
        ({"credential_strategy": None}, None),
    ],
)
def test_credential_strategy_defaults_and_rejects_invalid_values(
    table: dict[str, object], expected: str | None
) -> None:
    from agent_search_gateway.config import _resolve_credential_strategy

    label = "web_providers.dual"
    if expected is None:
        with pytest.raises(ConfigFailure) as caught:
            _resolve_credential_strategy(table, label)
        assert caught.value.code is ErrorCode.CONFIG_ERROR
        assert f"{label}.credential_strategy" in str(caught.value)
        assert "private.invalid" not in str(caught.value)
    else:
        assert _resolve_credential_strategy(table, label) == expected


def test_resolved_credential_snapshots_read_only_options_and_hides_sensitive_values() -> None:
    from dataclasses import FrozenInstanceError

    from agent_search_gateway.config import ResolvedCredential, ResolvedRatePolicy

    endpoint = "https://private.invalid/endpoint"
    options: dict[str, object] = {"base_url": endpoint}
    key = SecretValue("synthetic-api-key")
    email = SecretValue("contact@example.invalid")
    credential = ResolvedCredential(
        options=options,
        api_key_env="TEST_KEY",
        api_key=key,
        contact_email_env="TEST_EMAIL",
        contact_email=email,
    )
    options.clear()
    assert credential.options == {"base_url": endpoint}
    with pytest.raises(TypeError):
        credential.options["base_url"] = ""
    with pytest.raises(FrozenInstanceError):
        credential.max_concurrency = 3  # type: ignore[misc]
    assert not hasattr(credential, "__dict__")
    assert credential.max_concurrency is None
    assert credential.rate_policy == ResolvedRatePolicy()
    assert (credential.api_key_env, credential.api_key) == ("TEST_KEY", key)
    assert (credential.contact_email_env, credential.contact_email) == ("TEST_EMAIL", email)
    for sensitive in (endpoint, key.reveal(), email.reveal()):
        assert sensitive not in repr(credential)


def test_web_credentials_preserve_order_and_validate_every_member() -> None:
    members = [
        {"api_url": "https://one.example", "api_key_env": "ONE"},
        {"api_url": "https://two.example", "api_key_env": "TWO"},
    ]
    data = {"web_providers": {"tavily": {"enable_search": True, "credentials": members}}}
    registry = build_default_registry()
    [provider] = resolve_web_provider_config(data, registry, {"ONE": "a", "TWO": "b"}).providers
    assert [(c.options, c.api_key_env) for c in provider.credentials] == [
        ({"api_url": "https://one.example"}, "ONE"),
        ({"api_url": "https://two.example"}, "TWO"),
    ]
    assert [c.api_key.reveal() for c in provider.credentials if c.api_key] == ["a", "b"]
    assert dict(provider.options) == {}
    with pytest.raises(ConfigFailure, match=r"tavily\.credentials\[1\]"):
        resolve_web_provider_config(data, registry, {"ONE": "a"})


@pytest.mark.parametrize("order", [None, [], ["exa", "jina"], ["jina"], ["zenrows", "exa"]])
def test_fetch_order_prioritizes_then_appends_enabled_providers(order: list[str] | None) -> None:
    web: dict[str, object] = {
        "exa": {
            "enable_search": True,
            "enable_fetch": True,
            "credentials": [{"api_key_env": "KEY"}],
        },
        "jina": {"enable_fetch": True, "credentials": [{}]},
        "zenrows": {"enable_fetch": False},
    }
    if order is not None:
        web["fetch_order"] = order
    group = resolve_web_provider_config(
        {"web_providers": web}, build_default_registry(), {"KEY": "x"}
    )
    assert group.fetch_order == tuple(order or [])
    assert [p.name for p in group.providers if p.enable_search] == ["exa"]
    assert [p.name for p in group.providers if p.enable_fetch] == ["exa", "jina"]


@pytest.mark.parametrize("order", [["jina", "jina"], ["unknown"], ["brave"], [1], "jina", {}])
def test_fetch_order_rejects_invalid_entries(order: object) -> None:
    with pytest.raises(ConfigFailure, match="web_providers.fetch_order"):
        resolve_web_provider_config(
            {"web_providers": {"fetch_order": order}}, build_default_registry(), {}
        )


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize(
    ("name", "field"),
    [
        ("tavily", "api_url"),
        ("tavily", "api_key_env"),
        ("tavily", "max_concurrency"),
        ("tavily", "rate_limits"),
        ("tavily", "min_interval_seconds"),
        ("tinyfish", "search_api_url"),
        ("tinyfish", "fetch_api_url"),
        ("unknown_disabled", "api_url"),
    ],
)
def test_legacy_fields_require_migration(name: str, field: str, enabled: bool, mixed: bool) -> None:
    provider: dict[str, object] = {"enable_search": enabled, field: "synthetic-private-value"}
    if mixed:
        provider["credentials"] = [{"api_key_env": "KEY"}]
    with pytest.raises(ConfigFailure) as error:
        resolve_web_provider_config(
            {"web_providers": {name: provider}}, build_default_registry(), {}
        )
    assert f"web_providers.{name}.{field}" in str(error.value)
    assert "credentials" in str(error.value)
    assert "synthetic-private-value" not in str(error.value)


def test_legacy_default_max_concurrency_requires_migration() -> None:
    with pytest.raises(ConfigFailure, match=r"default_max_concurrency.*credentials"):
        resolve_web_provider_config(
            {"web_providers": {"default_max_concurrency": 3}}, _registry(), {}
        )


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ({}, "round_robin"),
        ({"credential_strategy": "round_robin"}, "round_robin"),
        ({"credential_strategy": "fill_first"}, "fill_first"),
        ({"credential_strategy": "fill_out"}, None),
        ({"credential_strategy": "https://private.invalid/key"}, None),
        ({"credential_strategy": UserString("fill_first")}, None),
        ({"credential_strategy": True}, None),
        ({"credential_strategy": []}, None),
        ({"credential_strategy": None}, None),
    ],
)
def test_web_credential_strategy(table: dict[str, object], expected: str | None) -> None:
    config = {"web_providers": {"jina": {"enable_fetch": True, "credentials": [{}], **table}}}
    if expected is None:
        with pytest.raises(ConfigFailure, match="web_providers.jina.credential_strategy"):
            resolve_web_provider_config(config, build_default_registry(), {})
    else:
        [provider] = resolve_web_provider_config(config, build_default_registry(), {}).providers
        assert provider.credential_strategy == expected


def test_brightdata_credentials_override_only_their_own_zones() -> None:
    table = {
        "enable_fetch": True,
        "search_zone": "shared-search",
        "fetch_zone": "shared-fetch",
        "credentials": [
            {"api_key_env": "KEY", "fetch_zone": "first"},
            {"api_key_env": "KEY", "search_zone": "second"},
        ],
    }
    [provider] = resolve_web_provider_config(
        {"web_providers": {"brightdata": table}}, build_default_registry(), {"KEY": "x"}
    ).providers
    assert dict(provider.options) == {"search_zone": "shared-search", "fetch_zone": "shared-fetch"}
    assert [c.options for c in provider.credentials] == [
        {"search_zone": "shared-search", "fetch_zone": "first"},
        {"search_zone": "second", "fetch_zone": "shared-fetch"},
    ]


@pytest.mark.parametrize(
    ("member", "reason"),
    [
        ({"api_url": "https://ydc-index.io"}, "REST requires an API key"),
        (
            {"api_url": "https://api.you.com/mcp/free", "api_key_env": "KEY"},
            "does not accept an API key",
        ),
    ],
)
def test_youcom_credentials_validate_each_endpoint_and_key_pair(
    member: dict[str, str], reason: str
) -> None:
    rest = {"api_url": "https://ydc-index.io", "api_key_env": "KEY"}
    free = {"api_url": "https://api.you.com/mcp/free"}
    members = [rest, free]
    config = {"web_providers": {"youcom": {"enable_fetch": True, "credentials": members}}}
    registry = build_default_registry()
    [provider] = resolve_web_provider_config(config, registry, {"KEY": "x"}).providers
    assert [c.api_key is not None for c in provider.credentials] == [True, False]
    members.append(member)
    with pytest.raises(ConfigFailure) as error:
        resolve_web_provider_config(config, registry, {"KEY": "x"})
    assert "web_providers.youcom.credentials[2]" in str(error.value)
    assert reason in str(error.value)


@pytest.mark.parametrize(
    ("name", "field"),
    [
        ("tavily", "api_url"),
        ("tinyfish", "search_api_url"),
        ("tinyfish", "fetch_api_url"),
        ("brightdata", "search_zone"),
        ("brightdata", "fetch_zone"),
    ],
)
@pytest.mark.parametrize("value", [None, 42, "   ", {"private.invalid": 1}])
def test_web_credentials_validate_explicit_endpoint_and_zone_types(
    name: str, field: str, value: object
) -> None:
    members = [{"api_key_env": "KEY"}, {"api_key_env": "KEY", field: value}]
    data = {"web_providers": {name: {"enable_fetch": True, "credentials": members}}}
    with pytest.raises(ConfigFailure) as error:
        resolve_web_provider_config(data, build_default_registry(), {"KEY": "x"})
    assert f"web_providers.{name}.credentials[1].{field}" in str(error.value)
    assert "private.invalid" not in str(error.value)


@pytest.mark.parametrize(
    "fields",
    [{}, {"credentials": []}, {"credentials": {}}, {"credentials": "x"}, {"credentials": [False]}],
)
def test_web_credentials_must_be_a_nonempty_array_of_tables(fields: dict[str, object]) -> None:
    config = {"web_providers": {"jina": {"enable_fetch": True, **fields}}}
    with pytest.raises(ConfigFailure, match="web_providers.jina.credentials"):
        resolve_web_provider_config(config, build_default_registry(), {})


@pytest.mark.parametrize(
    ("name", "field"),
    [
        ("firecrawl", "search_limit"),
        ("parallel", "mode"),
        ("open_websearch", "engines"),
        ("tavily", "fetch_zone"),
        ("tinyfish", "api_url"),
        ("jina", "base_url"),
        ("jina", "use_proxy"),
        ("jina", "credentials"),
        ("jina", "arbitrary_option"),
    ],
)
def test_web_credentials_reject_unregistered_overrides(name: str, field: str) -> None:
    config = {
        "web_providers": {name: {"enable_fetch": True, "credentials": [{field: "private.invalid"}]}}
    }
    with pytest.raises(ConfigFailure) as error:
        resolve_web_provider_config(config, build_default_registry(), {})
    assert f"web_providers.{name}.credentials[0]" in str(error.value)
    assert "private.invalid" not in str(error.value)


def test_tinyfish_credentials_keep_both_registered_endpoints() -> None:
    members = [
        {
            "api_key_env": "KEY",
            "search_api_url": "https://s1.example",
            "fetch_api_url": "https://f1.example",
        },
        {
            "api_key_env": "KEY",
            "search_api_url": "https://s2.example",
            "fetch_api_url": "https://f2.example",
        },
    ]
    config = {"web_providers": {"tinyfish": {"enable_search": True, "credentials": members}}}
    [provider] = resolve_web_provider_config(
        config, build_default_registry(), {"KEY": "x"}
    ).providers
    assert [
        (c.options["search_api_url"], c.options["fetch_api_url"]) for c in provider.credentials
    ] == [
        ("https://s1.example", "https://f1.example"),
        ("https://s2.example", "https://f2.example"),
    ]


def _factory() -> object:
    return object()


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(
        WebProviderRegistration(
            name="dual",
            capabilities=ProviderCapabilities(search=True, fetch=True),
            factory=_factory,
            allowed_config_keys=frozenset(
                {"api_url", "mode", "search_fetch_policy", "extract_fetch_policy"}
            ),
        )
    )
    registry.register(
        WebProviderRegistration(
            name="search_only",
            capabilities=ProviderCapabilities(search=True, fetch=False),
            factory=_factory,
            allowed_config_keys=frozenset({"api_url"}),
        )
    )
    return registry


def _optional_registry(*, required: bool = False) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(
        WebProviderRegistration(
            name="optional",
            capabilities=ProviderCapabilities(search=True, fetch=False),
            factory=_factory,
            allowed_config_keys=frozenset(),
            requires_api_key=required,
            supports_optional_api_key=True,
        )
    )
    return registry


def test_optional_web_provider_resolves_without_key() -> None:
    data = {"web_providers": {"optional": {"enable_search": True, "credentials": [{}]}}}
    [provider] = resolve_web_provider_config(data, _optional_registry(), {}).providers
    assert provider.credentials[0].api_key_env is None
    assert provider.credentials[0].api_key is None


def test_optional_web_provider_resolves_explicit_key() -> None:
    env_name = "ENV_A"
    data = {
        "web_providers": {
            "optional": {"enable_search": True, "credentials": [{"api_key_env": env_name}]}
        }
    }
    [provider] = resolve_web_provider_config(
        data, _optional_registry(), {"ENV_A": "synthetic-value"}
    ).providers
    assert provider.credentials[0].api_key_env == "ENV_A"
    assert isinstance(provider.credentials[0].api_key, SecretValue)
    assert provider.credentials[0].api_key.reveal() == "synthetic-value"
    assert "synthetic-value" not in repr(provider.credentials[0].api_key)


@pytest.mark.parametrize(
    ("env_name", "environ"),
    [("", {}), ("   ", {}), (17, {}), (None, {}), ("ENV_A", {}), ("ENV_A", {"ENV_A": ""})],
)
def test_optional_web_provider_rejects_invalid_explicit_key(
    env_name: object, environ: dict[str, str]
) -> None:
    data = {
        "web_providers": {
            "optional": {"enable_search": True, "credentials": [{"api_key_env": env_name}]}
        }
    }
    with pytest.raises(ConfigFailure) as caught:
        resolve_web_provider_config(data, _optional_registry(), environ)
    assert caught.value.code is ErrorCode.CONFIG_ERROR


def test_required_key_takes_precedence_over_optional() -> None:
    data = {"web_providers": {"optional": {"enable_search": True, "credentials": [{}]}}}
    with pytest.raises(ConfigFailure, match="requires api_key_env"):
        resolve_web_provider_config(data, _optional_registry(required=True), {})


def test_resolve_web_provider_config_or_fail_startup() -> None:
    data = {
        "web_providers": {
            "dual": {
                "enable_search": True,
                "enable_fetch": True,
                "credentials": [
                    {
                        "api_key_env": "DUAL_KEY",
                        "max_concurrency": 5,
                        "api_url": "https://api.example.test",
                    }
                ],
                "mode": "fast",
                "search_fetch_policy": {"max_age_seconds": 3600},
                "extract_fetch_policy": {
                    "timeout_seconds": 30,
                    "disable_cache_fallback": True,
                },
            },
            "search_only": {
                "enable_search": False,
                "enable_fetch": False,
                "credentials": [{"api_url": "https://disabled.example.test"}],
            },
        }
    }

    resolved = resolve_web_provider_config(data, _registry(), {"DUAL_KEY": "super-secret"})
    assert resolved.fetch_order == ()
    dual = resolved.providers[0]
    assert dual.name == "dual"
    assert dual.enable_search is True
    assert dual.enable_fetch is True
    assert dual.credentials[0].max_concurrency == 5
    assert dual.credentials[0].api_key_env == "DUAL_KEY"
    assert dual.credentials[0].api_key is not None
    assert dual.credentials[0].api_key.reveal() == "super-secret"
    assert "super-secret" not in repr(dual.credentials[0].api_key)
    assert dict(dual.credentials[0].options) == {
        "api_url": "https://api.example.test",
        "mode": "fast",
        "search_fetch_policy": {"max_age_seconds": 3600},
        "extract_fetch_policy": {
            "timeout_seconds": 30,
            "disable_cache_fallback": True,
        },
    }
    assert "enable_search" not in dual.options
    assert "enable_fetch" not in dual.options
    assert "api_key_env" not in dual.options
    assert "max_concurrency" not in dual.options

    disabled = resolved.providers[1]
    assert disabled.name == "search_only"
    assert disabled.enable_search is False
    assert disabled.credentials == ()


def test_jina_resolves_without_key() -> None:
    data = {
        "web_providers": {
            "jina": {
                "enable_search": False,
                "enable_fetch": True,
                "credentials": [{"max_concurrency": 5, "api_url": "https://reader.example.test"}],
            },
        }
    }

    resolved = resolve_web_provider_config(data, build_default_registry(), {})

    [jina] = resolved.providers
    assert jina.name == "jina"
    assert jina.enable_search is False
    assert jina.enable_fetch is True
    assert jina.credentials[0].max_concurrency == 5
    assert jina.credentials[0].api_key_env is None
    assert jina.credentials[0].api_key is None
    assert dict(jina.credentials[0].options) == {"api_url": "https://reader.example.test"}


def test_jina_rejects_api_key_env() -> None:
    data = {
        "web_providers": {
            "jina": {
                "enable_search": False,
                "enable_fetch": True,
                "credentials": [
                    {"api_key_env": "TEST_ENV", "api_url": "https://reader.example.test"}
                ],
            }
        }
    }

    with pytest.raises(ConfigFailure) as caught:
        resolve_web_provider_config(data, build_default_registry(), {})

    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert "web_providers.jina.credentials[0] does not accept api_key_env" in str(caught.value)


@pytest.mark.parametrize(
    ("provider_name", "provider_data", "environment"),
    [
        ("missing", {"enable_search": True, "credentials": [{"api_key_env": "KEY"}]}, {"KEY": "x"}),
        (
            "search_only",
            {"enable_fetch": True, "credentials": [{"api_key_env": "KEY", "api_url": "https://x"}]},
            {"KEY": "x"},
        ),
        ("dual", {"enable_search": True, "credentials": [{"api_key_env": "MISSING"}]}, {}),
        (
            "dual",
            {"enable_search": True, "credentials": [{"api_key_env": "KEY", "max_concurrency": 0}]},
            {"KEY": "x"},
        ),
        (
            "dual",
            {"enable_search": True, "credentials": [{"api_key_env": "KEY"}], "unknown": True},
            {"KEY": "x"},
        ),
    ],
)
def test_resolve_web_provider_config_rejects_invalid_enabled_provider(
    provider_name: str,
    provider_data: dict[str, object],
    environment: dict[str, str],
) -> None:
    data = {"web_providers": {provider_name: provider_data}}
    with pytest.raises(ConfigFailure) as caught:
        resolve_web_provider_config(data, _registry(), environment)
    assert caught.value.code is ErrorCode.CONFIG_ERROR


@pytest.mark.parametrize("provider_name", ["yuanbao", "zhihu", "baiduqianfan"])
@pytest.mark.parametrize(
    ("provider_data", "environment", "error_match"),
    [
        ({"enable_search": True, "credentials": [{}]}, {}, "requires api_key_env"),
        (
            {"enable_search": True, "credentials": [{"api_key_env": "SERVICE_KEY"}]},
            {},
            "environment variable SERVICE_KEY is required",
        ),
        (
            {"enable_search": True, "credentials": [{"api_key_env": "SERVICE_KEY"}]},
            {"SERVICE_KEY": ""},
            "environment variable SERVICE_KEY is required",
        ),
        (
            {"enable_fetch": True, "credentials": [{"api_key_env": "SERVICE_KEY"}]},
            {"SERVICE_KEY": "synthetic-service-value"},
            "does not support fetch",
        ),
        (
            {
                "enable_search": True,
                "credentials": [{"api_key_env": "SERVICE_KEY", "rate_limits": 3}],
            },
            {"SERVICE_KEY": "synthetic-service-value"},
            "rate_limits must be an array of tables",
        ),
        (
            {
                "enable_search": True,
                "credentials": [{"api_key_env": "SERVICE_KEY"}],
                "unknown": True,
            },
            {"SERVICE_KEY": "synthetic-service-value"},
            "unknown config key",
        ),
        (
            {"enable_search": False, "enable_fetch": False, "unknown": True},
            {},
            "unknown config key",
        ),
    ],
    ids=[
        "missing-env-name",
        "missing-env-value",
        "empty-env-value",
        "unsupported-fetch",
        "invalid-rate-limits",
        "unknown-option",
        "disabled-unknown-option",
    ],
)
def test_service_key_search_config_rejects_invalid_provider(
    provider_name: str,
    provider_data: dict[str, object],
    environment: dict[str, str],
    error_match: str,
) -> None:
    with pytest.raises(ConfigFailure, match=error_match) as caught:
        resolve_web_provider_config(
            {"web_providers": {provider_name: provider_data}},
            build_default_registry(),
            environment,
        )

    assert caught.value.code is ErrorCode.CONFIG_ERROR


@pytest.mark.parametrize("provider_name", ["yuanbao", "zhihu", "baiduqianfan"])
@pytest.mark.parametrize("api_url", [None, "https://service.example.test/gateway"])
def test_service_key_search_config_preserves_only_explicit_options(
    provider_name: str,
    api_url: str | None,
) -> None:
    provider_data: dict[str, object] = {
        "enable_search": True,
    }
    options = {} if api_url is None else {"api_url": api_url}
    provider_data["credentials"] = [{"api_key_env": "SERVICE_KEY", **options}]

    resolved = resolve_web_provider_config(
        {"web_providers": {provider_name: provider_data}},
        build_default_registry(),
        {"SERVICE_KEY": "synthetic-service-value"},
    )

    [provider] = resolved.providers
    assert provider.name == provider_name
    assert provider.enable_search is True
    assert provider.enable_fetch is False
    assert provider.credentials[0].api_key_env == "SERVICE_KEY"
    assert provider.credentials[0].api_key is not None
    assert provider.credentials[0].api_key.reveal() == "synthetic-service-value"
    assert dict(provider.credentials[0].options) == options


@pytest.mark.parametrize("provider_name", ["yuanbao", "zhihu", "baiduqianfan"])
@pytest.mark.parametrize("api_key_env", [None, "UNAVAILABLE_SERVICE_KEY"])
def test_disabled_service_key_search_provider_skips_credentials(
    provider_name: str,
    api_key_env: str | None,
) -> None:
    provider_data: dict[str, object] = {
        "enable_search": False,
        "enable_fetch": False,
    }
    credential = {"api_url": "https://disabled.example.test"}
    if api_key_env is not None:
        credential["api_key_env"] = api_key_env
    provider_data["credentials"] = [credential]

    resolved = resolve_web_provider_config(
        {"web_providers": {provider_name: provider_data}},
        build_default_registry(),
        {},
    )

    [provider] = resolved.providers
    assert provider.name == provider_name
    assert provider.enable_search is False
    assert provider.enable_fetch is False
    assert provider.credentials == ()
    assert dict(provider.options) == {}


@pytest.mark.parametrize("enabled", [True, False])
def test_baiduqianfan_example_key_is_required_only_for_enabled_search(enabled: bool) -> None:
    key_env = "BAIDUQIANFAN_API_KEY"
    config = {
        "web_providers": {
            "baiduqianfan": {"enable_search": enabled, "credentials": [{"api_key_env": key_env}]}
        }
    }
    environment = {key_env: "example-service-key"} if enabled else {}
    [provider] = resolve_web_provider_config(
        config, build_default_registry(), environment
    ).providers

    assert provider.enable_search is enabled
    assert provider.enable_fetch is False
    if enabled:
        assert provider.credentials[0].api_key_env == key_env
        assert (
            provider.credentials[0].api_key is not None
            and provider.credentials[0].api_key.reveal() == environment[key_env]
        )
    else:
        assert provider.credentials == ()


def test_disabled_parallel_requires_no_credential_and_preserves_allowed_options() -> None:
    data = {
        "web_providers": {
            "parallel": {
                "enable_search": False,
                "enable_fetch": False,
                "credentials": [{"api_url": "https://parallel.example.test"}],
                "mode": "invalid-but-unused",
                "search_fetch_policy": {"max_age_seconds": 1},
                "extract_fetch_policy": {"unknown_nested": True},
            }
        }
    }

    resolved = resolve_web_provider_config(data, build_default_registry(), {})

    [parallel] = resolved.providers
    assert parallel.name == "parallel"
    assert parallel.credentials == ()
    assert dict(parallel.options) == {
        "mode": "invalid-but-unused",
        "search_fetch_policy": {"max_age_seconds": 1},
        "extract_fetch_policy": {"unknown_nested": True},
    }


def test_disabled_parallel_still_rejects_unknown_top_level_option() -> None:
    data = {
        "web_providers": {
            "parallel": {
                "enable_search": False,
                "enable_fetch": False,
                "unknown": True,
            }
        }
    }

    with pytest.raises(ConfigFailure) as caught:
        resolve_web_provider_config(data, build_default_registry(), {})

    assert caught.value.code is ErrorCode.CONFIG_ERROR


@pytest.mark.parametrize(
    ("provider_name", "unsupported_flag"),
    [
        ("zenrows", "enable_search"),
        ("scrapingant", "enable_search"),
        ("serpapi", "enable_fetch"),
        ("jina", "enable_search"),
    ],
)
def test_new_provider_capabilities_are_enforced_by_generic_config_resolution(
    provider_name: str,
    unsupported_flag: str,
) -> None:
    data = {
        "web_providers": {
            provider_name: {
                unsupported_flag: True,
                "credentials": [{"api_key_env": "KEY", "api_url": "https://provider.example.test"}],
            }
        }
    }

    with pytest.raises(ConfigFailure) as caught:
        resolve_web_provider_config(data, build_default_registry(), {"KEY": "secret"})

    assert caught.value.code is ErrorCode.CONFIG_ERROR
    unsupported_stage = unsupported_flag.removeprefix("enable_")
    assert f"does not support {unsupported_stage}" in str(caught.value)


def test_new_provider_allowed_options_flow_through_generic_resolution() -> None:
    data = {
        "web_providers": {
            "brightdata": {
                "enable_search": True,
                "enable_fetch": True,
                "credentials": [
                    {
                        "api_key_env": "BRIGHT_KEY",
                        "api_url": "https://bright.example.test",
                        "max_concurrency": 7,
                    }
                ],
                "search_zone": "search-zone",
                "fetch_zone": "fetch-zone",
            },
            "scrape_do": {
                "enable_search": True,
                "enable_fetch": True,
                "credentials": [
                    {"api_key_env": "SCRAPE_KEY", "api_url": "https://scrape.example.test"}
                ],
            },
        }
    }

    resolved = resolve_web_provider_config(
        data,
        build_default_registry(),
        {"BRIGHT_KEY": "bright-secret", "SCRAPE_KEY": "scrape-secret"},
    )
    brightdata, scrape_do = resolved.providers

    assert dict(brightdata.credentials[0].options) == {
        "api_url": "https://bright.example.test",
        "search_zone": "search-zone",
        "fetch_zone": "fetch-zone",
    }
    assert brightdata.credentials[0].max_concurrency == 7
    assert brightdata.credentials[0].api_key is not None
    assert brightdata.credentials[0].api_key.reveal() == "bright-secret"
    assert "bright-secret" not in repr(brightdata.credentials[0].api_key)
    for shared_key in ("enable_search", "enable_fetch", "api_key_env", "max_concurrency"):
        assert shared_key not in brightdata.options

    assert dict(scrape_do.credentials[0].options) == {"api_url": "https://scrape.example.test"}
    assert scrape_do.credentials[0].api_key is not None
    assert scrape_do.credentials[0].api_key.reveal() == "scrape-secret"


def test_new_provider_unknown_option_is_rejected_generically() -> None:
    data = {
        "web_providers": {
            "scraperapi": {
                "enable_search": True,
                "credentials": [
                    {"api_key_env": "KEY", "api_url": "https://scraperapi.example.test"}
                ],
                "country": "us",
            }
        }
    }

    with pytest.raises(ConfigFailure) as caught:
        resolve_web_provider_config(data, build_default_registry(), {"KEY": "secret"})

    assert caught.value.code is ErrorCode.CONFIG_ERROR


def test_disabled_new_provider_needs_no_credential_or_constructor_validation() -> None:
    data = {
        "web_providers": {
            "brightdata": {
                "enable_search": False,
                "enable_fetch": False,
                "credentials": [{"api_url": " "}],
                "search_zone": " ",
                "fetch_zone": " ",
            }
        }
    }

    resolved = resolve_web_provider_config(data, build_default_registry(), {})

    [brightdata] = resolved.providers
    assert brightdata.credentials == ()
    assert dict(brightdata.options) == {
        "search_zone": " ",
        "fetch_zone": " ",
    }


_NEW_KEYED_PROVIDERS = ("doubao", "youcom", "keenable")
_NEW_PROVIDERS = (*_NEW_KEYED_PROVIDERS, "open_websearch")


@pytest.mark.parametrize("provider_name", _NEW_PROVIDERS)
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "7"])
def test_new_provider_rejects_invalid_concurrency_override(
    provider_name: str,
    value: object,
) -> None:
    with pytest.raises(ConfigFailure, match="positive integer"):
        resolve_web_provider_config(
            {"web_providers": {provider_name: {"credentials": [{"max_concurrency": value}]}}},
            build_default_registry(),
            {},
        )


@pytest.mark.parametrize("provider_name", _NEW_PROVIDERS)
def test_new_provider_has_no_default_concurrency(provider_name: str) -> None:
    member = {} if provider_name == "open_websearch" else {"api_key_env": "KEY"}
    [provider] = resolve_web_provider_config(
        {"web_providers": {provider_name: {"enable_search": True, "credentials": [member]}}},
        build_default_registry(),
        {"KEY": "x"},
    ).providers
    assert provider.credentials[0].max_concurrency is None


@pytest.mark.parametrize("provider_name", _NEW_PROVIDERS)
@pytest.mark.parametrize("option", ["live", "count", "limit", "searchMode"])
@pytest.mark.parametrize("enabled", [False, True])
def test_new_provider_rejects_unknown_vendor_options_even_when_disabled(
    provider_name: str,
    option: str,
    enabled: bool,
) -> None:
    table = {"enable_search": enabled, "enable_fetch": enabled, option: True}
    with pytest.raises(ConfigFailure, match="unknown config key"):
        resolve_web_provider_config(
            {"web_providers": {provider_name: table}},
            build_default_registry(),
            {},
        )


@pytest.mark.parametrize(
    "provider_name",
    [
        item.name
        for item in build_default_registry().list_in_registration_order()
        if item.name != "open_websearch"
    ],
)
def test_engines_are_not_a_shared_option(provider_name: str) -> None:
    with pytest.raises(ConfigFailure, match="unknown config key"):
        resolve_web_provider_config(
            {"web_providers": {provider_name: {"engines": ["google"]}}},
            build_default_registry(),
            {},
        )


def test_disabled_open_websearch_preserves_unvalidated_options() -> None:
    engines: list[str] = []
    [provider] = resolve_web_provider_config(
        {
            "web_providers": {
                "open_websearch": {
                    "enable_search": False,
                    "enable_fetch": False,
                    "credentials": [{"api_url": " ", "api_key_env": "MISSING"}],
                    "engines": engines,
                }
            }
        },
        build_default_registry(),
        {},
    ).providers
    assert dict(provider.options) == {"engines": []}
    assert provider.options["engines"] is engines
    assert provider.credentials == ()


@pytest.mark.parametrize("provider_name", _NEW_PROVIDERS)
def test_new_provider_options_preserve_shared_fields(provider_name: str) -> None:
    engines = ["google", "bing", "google"]
    table: dict[str, object] = {
        "enable_search": True,
        "enable_fetch": True,
    }
    member: dict[str, object] = {"api_url": "https://provider.example.test/", "max_concurrency": 7}
    table["credentials"] = [member]
    expected: dict[str, object] = {"api_url": "https://provider.example.test/"}
    if provider_name in _NEW_KEYED_PROVIDERS:
        member["api_key_env"] = "KEY"
    else:
        table["engines"] = engines
        expected["engines"] = engines
    [provider] = resolve_web_provider_config(
        {"web_providers": {provider_name: table}},
        build_default_registry(),
        {"KEY": "fixture-value"},
    ).providers
    assert dict(provider.credentials[0].options) == expected
    assert provider.credentials[0].max_concurrency == 7
    if provider_name == "open_websearch":
        assert provider.options["engines"] is engines
        assert isinstance(provider.options["engines"], list)


@pytest.mark.parametrize("provider_name", _NEW_PROVIDERS)
@pytest.mark.parametrize(
    ("search", "fetch"), [(False, False), (True, False), (False, True), (True, True)]
)
def test_new_provider_independent_capability_switches(
    provider_name: str,
    search: bool,
    fetch: bool,
) -> None:
    member: dict[str, object] = {}
    table: dict[str, object] = {
        "enable_search": search,
        "enable_fetch": fetch,
        "credentials": [member],
    }
    environment = {}
    if provider_name in _NEW_KEYED_PROVIDERS:
        member["api_key_env"] = "KEY"
        if search or fetch:
            environment["KEY"] = "fixture-value"
    [provider] = resolve_web_provider_config(
        {"web_providers": {provider_name: table}},
        build_default_registry(),
        environment,
    ).providers
    assert provider.enable_search is search
    assert provider.enable_fetch is fetch
    if not search and not fetch:
        assert provider.credentials == ()
    elif provider_name in _NEW_KEYED_PROVIDERS:
        assert provider.credentials[0].api_key is not None
    else:
        assert provider.credentials[0].api_key is None


@pytest.mark.parametrize("include_key", [False, True])
def test_open_websearch_credential_policy(include_key: bool) -> None:
    member: dict[str, object] = {}
    table: dict[str, object] = {
        "enable_search": True,
        "enable_fetch": True,
        "credentials": [member],
    }
    if include_key:
        member["api_key_env"] = "KEY"
        with pytest.raises(ConfigFailure, match="does not accept api_key_env"):
            resolve_web_provider_config(
                {"web_providers": {"open_websearch": table}},
                build_default_registry(),
                {"KEY": "fixture-value"},
            )
    else:
        [provider] = resolve_web_provider_config(
            {"web_providers": {"open_websearch": table}},
            build_default_registry(),
            {},
        ).providers
        assert provider.credentials[0].api_key is None
        assert provider.credentials[0].api_key_env is None


@pytest.mark.parametrize("provider_name", _NEW_KEYED_PROVIDERS)
def test_new_keyed_provider_resolves_only_explicit_secret(provider_name: str) -> None:
    data = {
        "web_providers": {
            provider_name: {
                "enable_search": True,
                "credentials": [{"api_key_env": "KEY"}],
            }
        }
    }
    [provider] = resolve_web_provider_config(
        data,
        build_default_registry(),
        {"KEY": "fixture-value", "OTHER": "unused"},
    ).providers

    assert isinstance(provider.credentials[0].api_key, SecretValue)
    assert provider.credentials[0].api_key.reveal() == "fixture-value"
    assert provider.credentials[0].api_key_env == "KEY"
    assert "fixture-value" not in repr(provider.credentials[0].api_key)
    assert "fixture-value" not in repr(provider)


@pytest.mark.parametrize("provider_name", _NEW_KEYED_PROVIDERS)
@pytest.mark.parametrize("credential", [None, "", "MISSING"])
def test_new_keyed_provider_requires_named_environment_value(
    provider_name: str,
    credential: str | None,
) -> None:
    member = {} if credential is None else {"api_key_env": credential}
    table: dict[str, object] = {"enable_fetch": True, "credentials": [member]}
    with pytest.raises(ConfigFailure) as caught:
        resolve_web_provider_config(
            {"web_providers": {provider_name: table}},
            build_default_registry(),
            {"OTHER": "unused"},
        )
    assert caught.value.code is ErrorCode.CONFIG_ERROR


def test_web_provider_resolves_multiple_rate_windows_without_adapter_options() -> None:
    from dataclasses import FrozenInstanceError

    [provider] = resolve_web_provider_config(
        {
            "web_providers": {
                "dual": {
                    "enable_search": True,
                    "mode": "fast",
                    "credentials": [
                        {
                            "api_key_env": "TEST_RATE",
                            "rate_limits": [
                                {"requests": 2, "period_seconds": 1},
                                {"requests": 30, "period_seconds": 60.5},
                            ],
                            "min_interval_seconds": 0.25,
                        }
                    ],
                }
            }
        },
        _registry(),
        {"TEST_RATE": "x"},
    ).providers

    policy = provider.credentials[0].rate_policy
    assert isinstance(policy.windows, tuple)
    assert [(window.requests, window.period_seconds) for window in policy.windows] == [
        (2, 1.0),
        (30, 60.5),
    ]
    assert policy.min_interval_seconds == 0.25
    assert dict(provider.options) == {"mode": "fast"}
    with pytest.raises(FrozenInstanceError):
        policy.min_interval_seconds = 1.0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        policy.windows[0].requests = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("rate_fields", "expected_windows", "expected_interval"),
    [
        ({}, [], None),
        ({"rate_limits": []}, [], None),
        ({"rate_limits": [{"requests": 3, "period_seconds": 2}]}, [(3, 2.0)], None),
        (
            {
                "rate_limits": [
                    {"requests": 3, "period_seconds": 2},
                    {"requests": 10, "period_seconds": 60.5},
                ]
            },
            [(3, 2.0), (10, 60.5)],
            None,
        ),
        ({"min_interval_seconds": 0.125}, [], 0.125),
    ],
    ids=["default", "empty", "single", "multiple", "interval-only"],
)
def test_web_rate_policy_values(
    rate_fields: dict[str, object],
    expected_windows: list[tuple[int, float]],
    expected_interval: float | None,
) -> None:
    [provider] = resolve_web_provider_config(
        {
            "web_providers": {
                "dual": {
                    "enable_search": True,
                    "credentials": [{"api_key_env": "TEST_RATE", **rate_fields}],
                }
            }
        },
        _registry(),
        {"TEST_RATE": "x"},
    ).providers
    windows = [(w.requests, w.period_seconds) for w in provider.credentials[0].rate_policy.windows]
    assert windows == expected_windows
    assert provider.credentials[0].rate_policy.min_interval_seconds == expected_interval
    assert dict(provider.options) == {}


@pytest.mark.parametrize(
    ("rate_limits", "field"),
    [
        (None, "rate_limits"),
        ({}, "rate_limits"),
        ((), "rate_limits"),
        ("do-not-echo-rate-value", "rate_limits"),
        ([None], "rate_limits[0]"),
        ([True], "rate_limits[0]"),
        ([[]], "rate_limits[0]"),
        ([{}], "rate_limits[0].requests"),
        ([{"requests": 2}], "rate_limits[0].period_seconds"),
        ([{"period_seconds": 1}], "rate_limits[0].requests"),
        (
            [{"requests": 2, "period_seconds": 1, "do-not-echo-key": "do-not-echo-value"}],
            "rate_limits[0]",
        ),
    ],
)
@pytest.mark.parametrize("enabled", [False, True])
def test_web_rate_policy_rejects_invalid_window_structure(
    rate_limits: object,
    field: str,
    enabled: bool,
) -> None:
    with pytest.raises(ConfigFailure) as caught:
        resolve_web_provider_config(
            {
                "web_providers": {
                    "dual": {
                        "enable_search": enabled,
                        "credentials": [{"api_key_env": "TEST_RATE", "rate_limits": rate_limits}],
                    }
                }
            },
            _registry(),
            {"TEST_RATE": "x"},
        )
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert f"web_providers.dual.credentials[0].{field}" in str(caught.value)
    assert "do-not-echo" not in str(caught.value)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "value",
    [0, -1, 1.0, 1.5, True, False, "3", {"do-not-echo": 1}, [1], None],
    ids=[
        "zero",
        "negative",
        "float",
        "fraction",
        "true",
        "false",
        "string",
        "nested",
        "array",
        "null",
    ],
)
def test_web_rate_policy_rejects_invalid_requests(value: object, enabled: bool) -> None:
    with pytest.raises(ConfigFailure) as caught:
        resolve_web_provider_config(
            {
                "web_providers": {
                    "dual": {
                        "enable_search": enabled,
                        "credentials": [
                            {
                                "api_key_env": "TEST_RATE",
                                "rate_limits": [{"requests": value, "period_seconds": 1}],
                            }
                        ],
                    }
                }
            },
            _registry(),
            {"TEST_RATE": "x"},
        )
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert "web_providers.dual.credentials[0].rate_limits[0].requests" in str(caught.value)
    assert "positive integer" in str(caught.value)
    assert "do-not-echo" not in str(caught.value)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("field", ["period_seconds", "min_interval_seconds"])
@pytest.mark.parametrize(
    "value",
    [
        0,
        -1,
        True,
        False,
        float("nan"),
        float("inf"),
        float("-inf"),
        10**1000,
        "3",
        {"do-not-echo": 1},
        [1],
        None,
    ],
    ids=[
        "zero",
        "negative",
        "true",
        "false",
        "nan",
        "inf",
        "negative-inf",
        "overflow",
        "string",
        "nested",
        "array",
        "null",
    ],
)
def test_web_rate_policy_rejects_invalid_seconds(
    value: object,
    field: str,
    enabled: bool,
) -> None:
    rate_fields = (
        {"rate_limits": [{"requests": 2, "period_seconds": value}]}
        if field == "period_seconds"
        else {"min_interval_seconds": value}
    )
    with pytest.raises(ConfigFailure) as caught:
        resolve_web_provider_config(
            {
                "web_providers": {
                    "dual": {
                        "enable_search": enabled,
                        "credentials": [{"api_key_env": "TEST_RATE", **rate_fields}],
                    }
                }
            },
            _registry(),
            {"TEST_RATE": "x"},
        )
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    path = f"rate_limits[0].{field}" if field == "period_seconds" else field
    assert f"web_providers.dual.credentials[0].{path}" in str(caught.value)
    assert "positive number" in str(caught.value)
    assert "do-not-echo" not in str(caught.value)


@pytest.mark.parametrize("name", ["dual", "unknown_disabled"])
def test_disabled_web_rate_policy_needs_no_credentials(name: str) -> None:
    [provider] = resolve_web_provider_config(
        {
            "web_providers": {
                name: {
                    "credentials": [
                        {
                            "rate_limits": [{"requests": 2, "period_seconds": 1}],
                            "min_interval_seconds": 0.5,
                        }
                    ],
                    "mode": "fast",
                }
            }
        },
        _registry(),
        {},
    ).providers
    assert provider.credentials == ()
    assert dict(provider.options) == {"mode": "fast"}


def test_unknown_disabled_web_rate_policy_is_still_validated() -> None:
    with pytest.raises(
        ConfigFailure, match=r"web_providers\.unknown_disabled\.credentials\[0\]\.rate_limits"
    ):
        resolve_web_provider_config(
            {"web_providers": {"unknown_disabled": {"credentials": [{"rate_limits": "unused"}]}}},
            _registry(),
            {},
        )


def test_web_rate_policy_preserves_enabled_provider_and_credential_checks() -> None:
    with pytest.raises(ConfigFailure, match="unknown enabled web provider"):
        resolve_web_provider_config(
            {
                "web_providers": {
                    "unknown": {"enable_search": True, "credentials": [{"rate_limits": []}]}
                }
            },
            _registry(),
            {},
        )
    with pytest.raises(ConfigFailure, match="requires api_key_env"):
        resolve_web_provider_config(
            {
                "web_providers": {
                    "dual": {"enable_search": True, "credentials": [{"rate_limits": []}]}
                }
            },
            _registry(),
            {},
        )


def test_web_provider_is_frozen_and_defaults_to_proxy() -> None:
    from dataclasses import FrozenInstanceError

    [provider] = resolve_web_provider_config(
        {"web_providers": {"jina": {"enable_fetch": True, "credentials": [{}]}}},
        build_default_registry(),
        {},
    ).providers
    assert provider.use_proxy is True
    assert provider.credentials[0].rate_policy.windows == ()
    assert provider.credentials[0].rate_policy.min_interval_seconds is None
    with pytest.raises(FrozenInstanceError):
        provider.use_proxy = False  # type: ignore[misc]


def test_no_auth_web_provider_resolves_rate_policy() -> None:
    [provider] = resolve_web_provider_config(
        {
            "web_providers": {
                "jina": {
                    "enable_fetch": True,
                    "credentials": [{"rate_limits": [{"requests": 2, "period_seconds": 1}]}],
                }
            }
        },
        build_default_registry(),
        {},
    ).providers
    assert provider.credentials[0].api_key is None
    assert [
        (w.requests, w.period_seconds) for w in provider.credentials[0].rate_policy.windows
    ] == [(2, 1.0)]
    assert dict(provider.options) == {}


@pytest.mark.parametrize("branch", ["disabled", "no_key", "with_key"])
@pytest.mark.parametrize(
    ("proxy_fields", "expected"),
    [({}, True), ({"use_proxy": True}, True), ({"use_proxy": False}, False)],
)
def test_web_provider_use_proxy_defaults_and_boolean_values(
    branch: str, proxy_fields: dict[str, object], expected: bool
) -> None:
    name = "jina" if branch == "no_key" else "firecrawl"
    member = {"api_url": "https://provider.example.test"}
    table: dict[str, object] = {"credentials": [member], **proxy_fields}
    environment = {}
    if branch == "no_key":
        table["enable_fetch"] = True
    elif branch == "with_key":
        table["enable_search"] = True
        member["api_key_env"] = "TEST_KEY"
        environment["TEST_KEY"] = "fixture-value"
    [provider] = resolve_web_provider_config(
        {"web_providers": {name: table}}, build_default_registry(), environment
    ).providers
    assert provider.use_proxy is expected
    if branch != "disabled":
        assert dict(provider.credentials[0].options) == {"api_url": "https://provider.example.test"}
    assert provider.enable_search is (branch == "with_key")
    assert provider.enable_fetch is (branch == "no_key")
    if branch == "disabled":
        assert provider.credentials == ()


@pytest.mark.parametrize("name", ["firecrawl", "jina", "unknown_disabled"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("value", ["false", "do-not-echo-proxy", 0, 1, None, [], {}, ()])
def test_web_provider_use_proxy_is_strict_boolean(name: str, enabled: bool, value: object) -> None:
    table: dict[str, object] = {"use_proxy": value}
    if name == "jina":
        table["enable_fetch"] = enabled
    elif name != "unknown_disabled":
        table.update(enable_search=enabled, credentials=[{"api_key_env": "TEST_KEY"}])
    with pytest.raises(ConfigFailure) as caught:
        resolve_web_provider_config(
            {"web_providers": {name: table}},
            build_default_registry(),
            {"TEST_KEY": "fixture-value"} if enabled else {},
        )
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert f"web_providers.{name}.use_proxy" in str(caught.value)
    assert "do-not-echo-proxy" not in str(caught.value)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "options",
    [
        {},
        {"search_limit": 1},
        {"search_limit": 10},
        {"search_limit": 100},
        {"search_highlights": True},
        {"search_highlights": False},
        {"scrape_formats": ["markdown"]},
        {"scrape_formats": ["rawHtml"]},
        {"scrape_formats": ["rawHtml", "markdown"]},
        {"scrape_parsers": []},
        {"scrape_parsers": ["pdf"]},
        {"scrape_parsers": ["image"]},
        {"scrape_parsers": ["image", "pdf"]},
        {
            "search_limit": 12,
            "search_highlights": False,
            "scrape_formats": ["markdown", "rawHtml"],
            "scrape_parsers": ["pdf", "image"],
        },
    ],
)
def test_firecrawl_options_are_validated_before_runtime(
    options: dict[str, object], enabled: bool
) -> None:
    [provider] = resolve_web_provider_config(
        {
            "web_providers": {
                "firecrawl": {
                    "enable_search": enabled,
                    "enable_fetch": enabled,
                    "credentials": [{"api_key_env": "TEST_KEY"}],
                    "use_proxy": False,
                    **options,
                }
            }
        },
        build_default_registry(),
        {"TEST_KEY": "x"} if enabled else {},
    ).providers
    assert dict(provider.options) == {
        key: tuple(value) if isinstance(value, list) else value for key, value in options.items()
    }
    assert provider.use_proxy is False
    assert provider.enable_search is enabled
    assert provider.enable_fetch is enabled
    assert bool(provider.credentials and provider.credentials[0].api_key) is enabled


_INVALID_FIRECRAWL_LIMITS: list[object] = [0, -1, 101, True, False, 1.0, "10", None, [], {}]
_INVALID_FIRECRAWL_HIGHLIGHTS: list[object] = [0, 1, "false", None, [], {}, ()]
_INVALID_FIRECRAWL_CHOICES: list[object] = [
    "do-not-echo",
    None,
    1,
    True,
    {},
    (),
    ["do-not-echo"],
    [1],
    [None],
    [["do-not-echo"]],
    [{"do-not-echo": "value"}],
]


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        *[("search_limit", value) for value in _INVALID_FIRECRAWL_LIMITS],
        *[("search_highlights", value) for value in _INVALID_FIRECRAWL_HIGHLIGHTS],
        *[
            (field, value)
            for field in ("scrape_formats", "scrape_parsers")
            for value in _INVALID_FIRECRAWL_CHOICES
        ],
        ("scrape_formats", []),
        ("scrape_formats", ["markdown", "markdown"]),
        ("scrape_formats", ["markdown", {"do-not-echo": "value"}]),
        ("scrape_parsers", ["pdf", "pdf"]),
        ("scrape_parsers", ["pdf", ["do-not-echo"]]),
    ],
)
def test_firecrawl_explicit_options_reject_invalid_values(
    field: str, value: object, enabled: bool
) -> None:
    with pytest.raises(ConfigFailure) as caught:
        resolve_web_provider_config(
            {"web_providers": {"firecrawl": {"enable_search": enabled, field: value}}},
            build_default_registry(),
            {},
        )
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert f"web_providers.firecrawl.{field}" in str(caught.value)
    assert "do-not-echo" not in str(caught.value)


@pytest.mark.parametrize(
    "name",
    [
        registration.name
        for registration in build_default_registry().list_in_registration_order()
        if registration.name != "firecrawl"
    ],
)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("search_limit", 10),
        ("search_highlights", True),
        ("scrape_formats", ["markdown"]),
        ("scrape_parsers", []),
    ],
)
def test_firecrawl_options_are_rejected_by_other_web_providers(
    name: str, field: str, value: object
) -> None:
    with pytest.raises(ConfigFailure, match=f"unknown config key.*{name}.*{field}"):
        resolve_web_provider_config(
            {"web_providers": {name: {field: value}}}, build_default_registry(), {}
        )
