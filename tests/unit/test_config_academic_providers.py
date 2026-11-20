import pytest

from agent_search_gateway.config import (
    ResolvedAcademicProviderConfig,
    ResolvedOAResolverConfig,
    resolve_academic_provider_config,
    resolve_oa_resolver_config,
)
from agent_search_gateway.errors import ConfigFailure, ErrorCode
from agent_search_gateway.providers.academic.registry import (
    AcademicProviderRegistration,
    AcademicProviderRegistry,
    OAResolverRegistration,
    OAResolverRegistry,
    Requirement,
)


def _factory(**_kwargs: object) -> object:
    return object()


def _academic_registry() -> AcademicProviderRegistry:
    registry = AcademicProviderRegistry()
    registrations: tuple[tuple[str, Requirement, Requirement], ...] = (
        ("no_auth", "none", "none"),
        ("optional", "optional", "optional"),
        ("required", "required", "required"),
    )
    for name, authentication, contact in registrations:
        registry.register(
            AcademicProviderRegistration(
                name=name,
                factory=_factory,
                allowed_config_keys=frozenset({"api_url"}),
                authentication=authentication,
                contact=contact,
            )
        )
    return registry


def _resolver_registry() -> OAResolverRegistry:
    registry = OAResolverRegistry()
    registry.register(
        OAResolverRegistration(
            name="unpaywall",
            factory=_factory,
            allowed_config_keys=frozenset({"api_url"}),
            authentication="none",
            contact="required",
        )
    )
    return registry


def test_enabled_openalex_requires_api_key_environment_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from agent_search_gateway.providers.academic.defaults import build_default_academic_registry

    requests: list[httpx.Request] = []

    async def reject_http(_client: httpx.AsyncClient, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("configuration must not send HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "send", reject_http)
    with pytest.raises(ConfigFailure, match="requires api_key_env") as caught:
        resolve_academic_provider_config(
            {"academic_providers": {"openalex": {"enabled": True}}},
            build_default_academic_registry(),
            {},
        )
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert requests == []


@pytest.mark.parametrize("value", [None, ""])
def test_enabled_openalex_rejects_missing_or_empty_api_key(value: str | None) -> None:
    from agent_search_gateway.providers.academic.defaults import build_default_academic_registry

    environment = {} if value is None else {"AUTH_ENV": value}
    with pytest.raises(ConfigFailure) as caught:
        resolve_academic_provider_config(
            {"academic_providers": {"openalex": {"enabled": True, "api_key_env": "AUTH_ENV"}}},
            build_default_academic_registry(),
            environment,
        )
    assert caught.value.code is ErrorCode.CONFIG_ERROR


def test_disabled_openalex_does_not_require_api_key() -> None:
    from agent_search_gateway.providers.academic.defaults import build_default_academic_registry

    [provider] = resolve_academic_provider_config(
        {"academic_providers": {"openalex": {"enabled": False}}},
        build_default_academic_registry(),
        {},
    ).providers
    assert provider.enabled is False
    assert provider.api_key is None


@pytest.mark.parametrize("with_contact", [False, True])
def test_enabled_openalex_resolves_api_key_and_optional_contact(with_contact: bool) -> None:
    from agent_search_gateway.providers.academic.defaults import build_default_academic_registry

    table: dict[str, object] = {"enabled": True, "api_key_env": "AUTH_ENV"}
    environment = {"AUTH_ENV": "[REDACTED_SECRET]"}
    if with_contact:
        table["contact_email_env"] = "CONTACT_ENV"
        environment["CONTACT_ENV"] = "research@example.test"
    [provider] = resolve_academic_provider_config(
        {"academic_providers": {"openalex": table}},
        build_default_academic_registry(),
        environment,
    ).providers
    assert provider.api_key is not None
    assert provider.api_key.reveal() == "[REDACTED_SECRET]"
    assert "[REDACTED_SECRET]" not in repr(provider)
    if with_contact:
        assert provider.contact_email is not None
        assert provider.contact_email.reveal() == "research@example.test"
    else:
        assert provider.contact_email is None
    assert dict(provider.options) == {}


def test_academic_config_supports_none_optional_and_required_modes() -> None:
    data = {
        "academic_providers": {
            "default_max_concurrency": 4,
            "no_auth": {"enabled": True, "api_url": "https://no-auth.example/v1"},
            "optional": {"enabled": True},
            "required": {
                "enabled": True,
                "api_key_env": "AUTH_ENV",
                "contact_email_env": "CONTACT_ENV",
            },
        }
    }
    resolved = resolve_academic_provider_config(
        data,
        _academic_registry(),
        {"AUTH_ENV": "[REDACTED_SECRET]", "CONTACT_ENV": "[REDACTED_SECRET]"},
    )
    assert resolved.default_max_concurrency == 4
    assert [item.name for item in resolved.providers] == ["no_auth", "optional", "required"]
    assert resolved.providers[0].api_key is None
    assert resolved.providers[1].api_key is None
    assert resolved.providers[2].api_key is not None
    assert resolved.providers[2].contact_email is not None


@pytest.mark.parametrize(
    ("provider", "table", "environment"),
    [
        ("missing", {"enabled": True}, {}),
        ("no_auth", {"enabled": True, "api_key_env": "AUTH_ENV"}, {}),
        ("optional", {"enabled": True, "api_key_env": "MISSING_ENV"}, {}),
        ("optional", {"enabled": True, "contact_email_env": "MISSING_ENV"}, {}),
        ("required", {"enabled": True}, {}),
        ("optional", {"enabled": True, "max_concurrency": 0}, {}),
        ("optional", {"enabled": "yes"}, {}),
        ("optional", {"enabled": True, "api_url": "ftp://invalid.example"}, {}),
        ("optional", {"enabled": True, "unknown": True}, {}),
        ("optional", {"enabled": True, "executor": "reserved"}, {}),
    ],
)
def test_academic_config_rejects_invalid_configuration(
    provider: str,
    table: dict[str, object],
    environment: dict[str, str],
) -> None:
    with pytest.raises(ConfigFailure) as caught:
        resolve_academic_provider_config(
            {"academic_providers": {provider: table}},
            _academic_registry(),
            environment,
        )
    assert caught.value.code is ErrorCode.CONFIG_ERROR


def test_disabled_required_provider_does_not_require_environment_values() -> None:
    resolved = resolve_academic_provider_config(
        {"academic_providers": {"required": {"enabled": False}}},
        _academic_registry(),
        {},
    )
    assert len(resolved.providers) == 1
    assert resolved.providers[0].enabled is False
    assert resolved.providers[0].api_key is None
    assert resolved.providers[0].contact_email is None


def test_unpaywall_is_optional_but_enabled_instance_requires_contact() -> None:
    registry = _resolver_registry()
    assert resolve_oa_resolver_config({}, registry, {}) is None
    assert (
        resolve_oa_resolver_config(
            {"oa_resolvers": {"unpaywall": {"enabled": False}}}, registry, {}
        )
        is None
    )

    data = {"oa_resolvers": {"unpaywall": {"enabled": True, "contact_email_env": "CONTACT_ENV"}}}
    with pytest.raises(ConfigFailure):
        resolve_oa_resolver_config(data, registry, {})
    resolved = resolve_oa_resolver_config(
        data,
        registry,
        {"CONTACT_ENV": "[REDACTED_SECRET]"},
    )
    assert resolved is not None
    assert resolved.contact_email is not None

    registry.register(
        OAResolverRegistration(
            name="other",
            factory=_factory,
            allowed_config_keys=frozenset(),
            authentication="none",
            contact="none",
        )
    )
    with pytest.raises(ConfigFailure, match="only one OA resolver may be enabled"):
        resolve_oa_resolver_config(
            {
                "oa_resolvers": {
                    "unpaywall": {
                        "enabled": True,
                        "contact_email_env": "CONTACT_ENV",
                    },
                    "other": {"enabled": True},
                }
            },
            registry,
            {"CONTACT_ENV": "[REDACTED_SECRET]"},
        )


@pytest.mark.parametrize("kind", ["academic", "oa"])
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
def test_academic_and_oa_rate_policy_values(
    kind: str,
    rate_fields: dict[str, object],
    expected_windows: list[tuple[int, float]],
    expected_interval: float | None,
) -> None:
    table = {"enabled": True, "api_url": "https://example.test", **rate_fields}
    provider: ResolvedAcademicProviderConfig | ResolvedOAResolverConfig | None
    if kind == "academic":
        [provider] = resolve_academic_provider_config(
            {"academic_providers": {"optional": table}},
            _academic_registry(),
            {},
        ).providers
    else:
        table["contact_email_env"] = "TEST_CONTACT"
        provider = resolve_oa_resolver_config(
            {"oa_resolvers": {"unpaywall": table}},
            _resolver_registry(),
            {"TEST_CONTACT": "test@example.test"},
        )
        assert provider is not None
    assert isinstance(provider.rate_policy.windows, tuple)
    windows = [(w.requests, w.period_seconds) for w in provider.rate_policy.windows]
    assert windows == expected_windows
    assert provider.rate_policy.min_interval_seconds == expected_interval
    assert dict(provider.options) == {"api_url": "https://example.test"}


@pytest.mark.parametrize("kind", ["academic", "oa"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    ("rate_fields", "path"),
    [
        ({"rate_limits": None}, "rate_limits"),
        ({"rate_limits": {}}, "rate_limits"),
        ({"rate_limits": ()}, "rate_limits"),
        ({"rate_limits": "do-not-echo-value"}, "rate_limits"),
        ({"rate_limits": [None]}, "rate_limits[0]"),
        ({"rate_limits": [True]}, "rate_limits[0]"),
        ({"rate_limits": [[]]}, "rate_limits[0]"),
        ({"rate_limits": [{}]}, "rate_limits[0].requests"),
        ({"rate_limits": [{"requests": 2}]}, "rate_limits[0].period_seconds"),
        ({"rate_limits": [{"period_seconds": 1}]}, "rate_limits[0].requests"),
        (
            {"rate_limits": [{"requests": 2, "period_seconds": 1, "do-not-echo-key": True}]},
            "rate_limits[0]",
        ),
        *[
            ({"rate_limits": [{"requests": value, "period_seconds": 1}]}, "rate_limits[0].requests")
            for value in (0, -1, 1.0, 1.5, True, False, "3", {"do-not-echo": 1}, [1], None)
        ],
        *[
            (fields, path)
            for value in (
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
            )
            for fields, path in (
                (
                    {"rate_limits": [{"requests": 2, "period_seconds": value}]},
                    "rate_limits[0].period_seconds",
                ),
                ({"min_interval_seconds": value}, "min_interval_seconds"),
            )
        ],
    ],
)
def test_academic_and_oa_rate_policy_rejects_invalid_fields(
    kind: str,
    enabled: bool,
    rate_fields: dict[str, object],
    path: str,
) -> None:
    table = {"enabled": enabled, **rate_fields}
    with pytest.raises(ConfigFailure) as caught:
        if kind == "academic":
            resolve_academic_provider_config(
                {"academic_providers": {"optional": table}},
                _academic_registry(),
                {},
            )
        else:
            table["contact_email_env"] = "TEST_CONTACT"
            resolve_oa_resolver_config(
                {"oa_resolvers": {"unpaywall": table}},
                _resolver_registry(),
                {"TEST_CONTACT": "test@example.test"},
            )
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    prefix = "academic_providers.optional" if kind == "academic" else "oa_resolvers.unpaywall"
    assert f"{prefix}.{path}" in str(caught.value)
    assert "do-not-echo" not in str(caught.value)


def test_disabled_academic_rate_policy_needs_no_credentials() -> None:
    [provider] = resolve_academic_provider_config(
        {
            "academic_providers": {
                "required": {
                    "enabled": False,
                    "rate_limits": [{"requests": 2, "period_seconds": 1}],
                    "min_interval_seconds": 0.5,
                }
            }
        },
        _academic_registry(),
        {},
    ).providers
    assert provider.api_key is None
    assert provider.contact_email is None
    assert [(w.requests, w.period_seconds) for w in provider.rate_policy.windows] == [(2, 1.0)]
    assert provider.rate_policy.min_interval_seconds == 0.5
    assert dict(provider.options) == {}


def test_disabled_oa_rate_policy_needs_no_credentials() -> None:
    assert (
        resolve_oa_resolver_config(
            {
                "oa_resolvers": {
                    "unpaywall": {
                        "enabled": False,
                        "rate_limits": [{"requests": 2, "period_seconds": 1}],
                        "min_interval_seconds": 0.5,
                    }
                }
            },
            _resolver_registry(),
            {},
        )
        is None
    )


@pytest.mark.parametrize("kind", ["academic", "oa"])
def test_unknown_disabled_academic_or_oa_provider_still_fails(kind: str) -> None:
    table = {"enabled": False, "rate_limits": []}
    with pytest.raises(ConfigFailure, match="unknown"):
        if kind == "academic":
            resolve_academic_provider_config(
                {"academic_providers": {"missing": table}},
                _academic_registry(),
                {},
            )
        else:
            resolve_oa_resolver_config(
                {"oa_resolvers": {"missing": table}},
                _resolver_registry(),
                {},
            )


def test_academic_rate_policy_preserves_required_credential_checks() -> None:
    with pytest.raises(ConfigFailure, match="requires api_key_env"):
        resolve_academic_provider_config(
            {"academic_providers": {"required": {"enabled": True, "rate_limits": []}}},
            _academic_registry(),
            {},
        )
    with pytest.raises(ConfigFailure, match="requires contact_email_env"):
        resolve_oa_resolver_config(
            {"oa_resolvers": {"unpaywall": {"enabled": True, "rate_limits": []}}},
            _resolver_registry(),
            {},
        )


def test_academic_and_oa_old_constructors_default_to_empty_rate_policy() -> None:
    from agent_search_gateway.config import (
        ResolvedAcademicProviderConfig,
        ResolvedOAResolverConfig,
    )

    academic = ResolvedAcademicProviderConfig("name", True, 3, None, None, None, None, {})
    resolver = ResolvedOAResolverConfig("name", None, None, None, None, {})
    for provider in (academic, resolver):
        assert provider.rate_policy.windows == ()
        assert provider.rate_policy.min_interval_seconds is None
