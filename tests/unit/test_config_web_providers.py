import pytest

from agent_search_gateway.config import resolve_web_provider_config
from agent_search_gateway.errors import ConfigFailure, ErrorCode
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import ProviderCapabilities
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.providers.registry import ProviderRegistry, WebProviderRegistration


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


def test_resolve_web_provider_config_or_fail_startup() -> None:
    data = {
        "web_providers": {
            "default_max_concurrency": 3,
            "dual": {
                "enable_search": True,
                "enable_fetch": True,
                "api_key_env": "DUAL_KEY",
                "max_concurrency": 5,
                "api_url": "https://api.example.test",
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
                "api_url": "https://disabled.example.test",
            },
        }
    }

    resolved = resolve_web_provider_config(data, _registry(), {"DUAL_KEY": "super-secret"})
    assert resolved.default_max_concurrency == 3
    dual = resolved.providers[0]
    assert dual.name == "dual"
    assert dual.enable_search is True
    assert dual.enable_fetch is True
    assert dual.max_concurrency == 5
    assert dual.api_key_env == "DUAL_KEY"
    assert dual.secret is not None
    assert dual.secret.reveal() == "super-secret"
    assert "super-secret" not in repr(dual.secret)
    assert dict(dual.options) == {
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
    assert disabled.secret is None


def test_jina_resolves_without_credential() -> None:
    data = {
        "web_providers": {
            "default_max_concurrency": 3,
            "jina": {
                "enable_search": False,
                "enable_fetch": True,
                "max_concurrency": 5,
                "api_url": "https://reader.example.test",
            },
        }
    }

    resolved = resolve_web_provider_config(data, build_default_registry(), {})

    [jina] = resolved.providers
    assert jina.name == "jina"
    assert jina.enable_search is False
    assert jina.enable_fetch is True
    assert jina.max_concurrency == 5
    assert jina.api_key_env is None
    assert jina.secret is None
    assert dict(jina.options) == {"api_url": "https://reader.example.test"}


def test_jina_rejects_api_key_env() -> None:
    data = {
        "web_providers": {
            "jina": {
                "enable_search": False,
                "enable_fetch": True,
                "api_key_env": "[REDACTED_SECRET]",
                "api_url": "https://reader.example.test",
            }
        }
    }

    with pytest.raises(ConfigFailure) as caught:
        resolve_web_provider_config(data, build_default_registry(), {})

    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert "web provider jina does not accept api_key_env" in str(caught.value)


@pytest.mark.parametrize(
    ("provider_name", "provider_data", "environment"),
    [
        ("missing", {"enable_search": True, "api_key_env": "KEY"}, {"KEY": "x"}),
        (
            "search_only",
            {"enable_fetch": True, "api_key_env": "KEY", "api_url": "https://x"},
            {"KEY": "x"},
        ),
        ("dual", {"enable_search": True, "api_key_env": "MISSING"}, {}),
        ("dual", {"enable_search": True, "api_key_env": "KEY", "max_concurrency": 0}, {"KEY": "x"}),
        (
            "dual",
            {"enable_search": True, "api_key_env": "KEY", "unknown": True},
            {"KEY": "x"},
        ),
    ],
)
def test_resolve_web_provider_config_rejects_invalid_enabled_provider(
    provider_name: str,
    provider_data: dict[str, object],
    environment: dict[str, str],
) -> None:
    data = {"web_providers": {"default_max_concurrency": 3, provider_name: provider_data}}
    with pytest.raises(ConfigFailure) as caught:
        resolve_web_provider_config(data, _registry(), environment)
    assert caught.value.code is ErrorCode.CONFIG_ERROR


@pytest.mark.parametrize("provider_name", ["yuanbao", "zhihu"])
@pytest.mark.parametrize(
    ("provider_data", "environment", "error_match"),
    [
        ({"enable_search": True}, {}, "requires api_key_env"),
        (
            {"enable_search": True, "api_key_env": "SERVICE_KEY"},
            {},
            "environment variable SERVICE_KEY is required",
        ),
        (
            {"enable_search": True, "api_key_env": "SERVICE_KEY"},
            {"SERVICE_KEY": ""},
            "environment variable SERVICE_KEY is required",
        ),
        (
            {"enable_fetch": True, "api_key_env": "SERVICE_KEY"},
            {"SERVICE_KEY": "synthetic-service-value"},
            "does not support fetch",
        ),
        (
            {"enable_search": True, "api_key_env": "SERVICE_KEY", "unknown": True},
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


@pytest.mark.parametrize("provider_name", ["yuanbao", "zhihu"])
@pytest.mark.parametrize("api_url", [None, "https://service.example.test/gateway"])
def test_service_key_search_config_preserves_only_explicit_options(
    provider_name: str,
    api_url: str | None,
) -> None:
    provider_data: dict[str, object] = {
        "enable_search": True,
        "api_key_env": "SERVICE_KEY",
    }
    options = {} if api_url is None else {"api_url": api_url}
    provider_data.update(options)

    resolved = resolve_web_provider_config(
        {"web_providers": {provider_name: provider_data}},
        build_default_registry(),
        {"SERVICE_KEY": "synthetic-service-value"},
    )

    [provider] = resolved.providers
    assert provider.name == provider_name
    assert provider.enable_search is True
    assert provider.enable_fetch is False
    assert provider.api_key_env == "SERVICE_KEY"
    assert provider.secret is not None
    assert provider.secret.reveal() == "synthetic-service-value"
    assert dict(provider.options) == options


@pytest.mark.parametrize("provider_name", ["yuanbao", "zhihu"])
@pytest.mark.parametrize("api_key_env", [None, "UNAVAILABLE_SERVICE_KEY"])
def test_disabled_service_key_search_provider_skips_credentials(
    provider_name: str,
    api_key_env: str | None,
) -> None:
    provider_data: dict[str, object] = {
        "enable_search": False,
        "enable_fetch": False,
        "api_url": "https://disabled.example.test",
    }
    if api_key_env is not None:
        provider_data["api_key_env"] = api_key_env

    resolved = resolve_web_provider_config(
        {"web_providers": {provider_name: provider_data}},
        build_default_registry(),
        {},
    )

    [provider] = resolved.providers
    assert provider.name == provider_name
    assert provider.enable_search is False
    assert provider.enable_fetch is False
    assert provider.api_key_env is None
    assert provider.secret is None
    assert dict(provider.options) == {"api_url": "https://disabled.example.test"}


def test_disabled_parallel_requires_no_credential_and_preserves_allowed_options() -> None:
    data = {
        "web_providers": {
            "parallel": {
                "enable_search": False,
                "enable_fetch": False,
                "api_url": "https://parallel.example.test",
                "mode": "invalid-but-unused",
                "search_fetch_policy": {"max_age_seconds": 1},
                "extract_fetch_policy": {"unknown_nested": True},
            }
        }
    }

    resolved = resolve_web_provider_config(data, build_default_registry(), {})

    [parallel] = resolved.providers
    assert parallel.name == "parallel"
    assert parallel.secret is None
    assert dict(parallel.options) == {
        "api_url": "https://parallel.example.test",
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
                "api_key_env": "KEY",
                "api_url": "https://provider.example.test",
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
                "api_key_env": "BRIGHT_KEY",
                "api_url": "https://bright.example.test",
                "search_zone": "search-zone",
                "fetch_zone": "fetch-zone",
                "max_concurrency": 7,
            },
            "scrape_do": {
                "enable_search": True,
                "enable_fetch": True,
                "api_key_env": "SCRAPE_KEY",
                "api_url": "https://scrape.example.test",
            },
        }
    }

    resolved = resolve_web_provider_config(
        data,
        build_default_registry(),
        {"BRIGHT_KEY": "bright-secret", "SCRAPE_KEY": "scrape-secret"},
    )
    brightdata, scrape_do = resolved.providers

    assert dict(brightdata.options) == {
        "api_url": "https://bright.example.test",
        "search_zone": "search-zone",
        "fetch_zone": "fetch-zone",
    }
    assert brightdata.max_concurrency == 7
    assert brightdata.secret is not None
    assert brightdata.secret.reveal() == "bright-secret"
    assert "bright-secret" not in repr(brightdata.secret)
    for shared_key in ("enable_search", "enable_fetch", "api_key_env", "max_concurrency"):
        assert shared_key not in brightdata.options

    assert dict(scrape_do.options) == {"api_url": "https://scrape.example.test"}
    assert scrape_do.secret is not None
    assert scrape_do.secret.reveal() == "scrape-secret"


def test_new_provider_unknown_option_is_rejected_generically() -> None:
    data = {
        "web_providers": {
            "scraperapi": {
                "enable_search": True,
                "api_key_env": "KEY",
                "api_url": "https://scraperapi.example.test",
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
                "api_url": " ",
                "search_zone": " ",
                "fetch_zone": " ",
            }
        }
    }

    resolved = resolve_web_provider_config(data, build_default_registry(), {})

    [brightdata] = resolved.providers
    assert brightdata.secret is None
    assert dict(brightdata.options) == {
        "api_url": " ",
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
            {"web_providers": {provider_name: {"max_concurrency": value}}},
            build_default_registry(),
            {},
        )


@pytest.mark.parametrize("provider_name", _NEW_PROVIDERS)
def test_new_provider_inherits_group_concurrency(provider_name: str) -> None:
    [provider] = resolve_web_provider_config(
        {"web_providers": {"default_max_concurrency": 5, provider_name: {}}},
        build_default_registry(),
        {},
    ).providers
    assert provider.max_concurrency == 5


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
                    "api_url": " ",
                    "engines": engines,
                    "api_key_env": "MISSING",
                }
            }
        },
        build_default_registry(),
        {},
    ).providers
    assert dict(provider.options) == {"api_url": " ", "engines": []}
    assert provider.options["engines"] is engines
    assert provider.secret is None
    assert provider.api_key_env is None


@pytest.mark.parametrize("provider_name", _NEW_PROVIDERS)
def test_new_provider_options_preserve_shared_fields(provider_name: str) -> None:
    engines = ["google", "bing", "google"]
    table: dict[str, object] = {
        "enable_search": True,
        "enable_fetch": True,
        "api_url": "https://provider.example.test/",
        "max_concurrency": 7,
    }
    expected: dict[str, object] = {"api_url": "https://provider.example.test/"}
    if provider_name in _NEW_KEYED_PROVIDERS:
        table["api_key_env"] = "KEY"
    else:
        table["engines"] = engines
        expected["engines"] = engines
    [provider] = resolve_web_provider_config(
        {"web_providers": {provider_name: table}},
        build_default_registry(),
        {"KEY": "fixture-value"},
    ).providers
    assert dict(provider.options) == expected
    assert provider.max_concurrency == 7
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
    table: dict[str, object] = {"enable_search": search, "enable_fetch": fetch}
    environment = {}
    if provider_name in _NEW_KEYED_PROVIDERS:
        table["api_key_env"] = "KEY"
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
        assert provider.secret is None
        assert provider.api_key_env is None
    elif provider_name in _NEW_KEYED_PROVIDERS:
        assert provider.secret is not None
    else:
        assert provider.secret is None


@pytest.mark.parametrize("include_key", [False, True])
def test_open_websearch_credential_policy(include_key: bool) -> None:
    table: dict[str, object] = {"enable_search": True, "enable_fetch": True}
    if include_key:
        table["api_key_env"] = "KEY"
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
        assert provider.secret is None
        assert provider.api_key_env is None


@pytest.mark.parametrize("provider_name", _NEW_KEYED_PROVIDERS)
def test_new_keyed_provider_resolves_only_explicit_secret(provider_name: str) -> None:
    data = {
        "web_providers": {
            provider_name: {
                "enable_search": True,
                "api_key_env": "KEY",
            }
        }
    }
    [provider] = resolve_web_provider_config(
        data,
        build_default_registry(),
        {"KEY": "fixture-value", "OTHER": "unused"},
    ).providers

    assert isinstance(provider.secret, SecretValue)
    assert provider.secret.reveal() == "fixture-value"
    assert provider.api_key_env == "KEY"
    assert "fixture-value" not in repr(provider.secret)
    assert "fixture-value" not in repr(provider)


@pytest.mark.parametrize("provider_name", _NEW_KEYED_PROVIDERS)
@pytest.mark.parametrize("credential", [None, "", "MISSING"])
def test_new_keyed_provider_requires_named_environment_value(
    provider_name: str,
    credential: str | None,
) -> None:
    table: dict[str, object] = {"enable_fetch": True}
    if credential is not None:
        table["api_key_env"] = credential
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
                    "api_key_env": "TEST_RATE",
                    "mode": "fast",
                    "rate_limits": [
                        {"requests": 2, "period_seconds": 1},
                        {"requests": 30, "period_seconds": 60.5},
                    ],
                    "min_interval_seconds": 0.25,
                }
            }
        },
        _registry(),
        {"TEST_RATE": "x"},
    ).providers

    policy = provider.rate_policy
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
                    "api_key_env": "TEST_RATE",
                    **rate_fields,
                }
            }
        },
        _registry(),
        {"TEST_RATE": "x"},
    ).providers
    windows = [(w.requests, w.period_seconds) for w in provider.rate_policy.windows]
    assert windows == expected_windows
    assert provider.rate_policy.min_interval_seconds == expected_interval
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
                        "api_key_env": "TEST_RATE",
                        "rate_limits": rate_limits,
                    }
                }
            },
            _registry(),
            {"TEST_RATE": "x"},
        )
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert f"web_providers.dual.{field}" in str(caught.value)
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
                        "api_key_env": "TEST_RATE",
                        "rate_limits": [{"requests": value, "period_seconds": 1}],
                    }
                }
            },
            _registry(),
            {"TEST_RATE": "x"},
        )
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert "web_providers.dual.rate_limits[0].requests" in str(caught.value)
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
                        "api_key_env": "TEST_RATE",
                        **rate_fields,
                    }
                }
            },
            _registry(),
            {"TEST_RATE": "x"},
        )
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    path = f"rate_limits[0].{field}" if field == "period_seconds" else field
    assert f"web_providers.dual.{path}" in str(caught.value)
    assert "positive number" in str(caught.value)
    assert "do-not-echo" not in str(caught.value)


@pytest.mark.parametrize("name", ["dual", "unknown_disabled"])
def test_disabled_web_rate_policy_needs_no_credentials(name: str) -> None:
    [provider] = resolve_web_provider_config(
        {
            "web_providers": {
                name: {
                    "rate_limits": [{"requests": 2, "period_seconds": 1}],
                    "min_interval_seconds": 0.5,
                    "mode": "fast",
                }
            }
        },
        _registry(),
        {},
    ).providers
    assert provider.secret is None
    assert provider.api_key_env is None
    assert [(w.requests, w.period_seconds) for w in provider.rate_policy.windows] == [(2, 1.0)]
    assert provider.rate_policy.min_interval_seconds == 0.5
    assert dict(provider.options) == {"mode": "fast"}


def test_unknown_disabled_web_rate_policy_is_still_validated() -> None:
    with pytest.raises(ConfigFailure, match="web_providers.unknown_disabled.rate_limits"):
        resolve_web_provider_config(
            {"web_providers": {"unknown_disabled": {"rate_limits": "unused"}}},
            _registry(),
            {},
        )


def test_web_rate_policy_preserves_enabled_provider_and_credential_checks() -> None:
    with pytest.raises(ConfigFailure, match="unknown enabled web provider"):
        resolve_web_provider_config(
            {"web_providers": {"unknown": {"enable_search": True, "rate_limits": []}}},
            _registry(),
            {},
        )
    with pytest.raises(ConfigFailure, match="requires api_key_env"):
        resolve_web_provider_config(
            {"web_providers": {"dual": {"enable_search": True, "rate_limits": []}}},
            _registry(),
            {},
        )


def test_web_old_constructor_defaults_to_empty_rate_policy() -> None:
    from agent_search_gateway.config import ResolvedWebProviderConfig

    provider = ResolvedWebProviderConfig("dual", True, True, 3, None, None, {})
    assert provider.rate_policy.windows == ()
    assert provider.rate_policy.min_interval_seconds is None
    assert provider.use_proxy is True


def test_web_provider_use_proxy_preserves_positional_rate_policy_and_frozen_config() -> None:
    from dataclasses import FrozenInstanceError

    from agent_search_gateway.config import ResolvedRatePolicy, ResolvedWebProviderConfig

    policy = ResolvedRatePolicy(min_interval_seconds=0.5)
    provider = ResolvedWebProviderConfig("dual", True, True, 3, None, None, {}, policy)
    assert provider.rate_policy is policy
    assert provider.use_proxy is True
    with pytest.raises(FrozenInstanceError):
        provider.use_proxy = False  # type: ignore[misc]


def test_no_auth_web_provider_resolves_rate_policy() -> None:
    [provider] = resolve_web_provider_config(
        {
            "web_providers": {
                "jina": {
                    "enable_fetch": True,
                    "rate_limits": [{"requests": 2, "period_seconds": 1}],
                }
            }
        },
        build_default_registry(),
        {},
    ).providers
    assert provider.secret is None
    assert [(w.requests, w.period_seconds) for w in provider.rate_policy.windows] == [(2, 1.0)]
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
    table: dict[str, object] = {"api_url": "https://provider.example.test", **proxy_fields}
    environment = {}
    if branch == "no_key":
        table["enable_fetch"] = True
    elif branch == "with_key":
        table.update(enable_search=True, api_key_env="TEST_KEY")
        environment["TEST_KEY"] = "fixture-value"
    [provider] = resolve_web_provider_config(
        {"web_providers": {name: table}}, build_default_registry(), environment
    ).providers
    assert provider.use_proxy is expected
    assert dict(provider.options) == {"api_url": "https://provider.example.test"}
    assert provider.enable_search is (branch == "with_key")
    assert provider.enable_fetch is (branch == "no_key")
    if branch == "disabled":
        assert provider.secret is None
        assert provider.api_key_env is None


@pytest.mark.parametrize("name", ["firecrawl", "jina", "unknown_disabled"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("value", ["false", "do-not-echo-proxy", 0, 1, None, [], {}, ()])
def test_web_provider_use_proxy_is_strict_boolean(name: str, enabled: bool, value: object) -> None:
    table: dict[str, object] = {"use_proxy": value}
    if name == "jina":
        table["enable_fetch"] = enabled
    elif name != "unknown_disabled":
        table.update(enable_search=enabled, api_key_env="TEST_KEY")
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
                    "api_key_env": "TEST_KEY",
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
    assert (provider.secret is not None) is enabled


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
