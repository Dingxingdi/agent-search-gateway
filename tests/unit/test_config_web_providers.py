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
