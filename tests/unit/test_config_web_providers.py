import pytest

from agent_search_gateway.config import resolve_web_provider_config
from agent_search_gateway.errors import ConfigFailure, ErrorCode
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
