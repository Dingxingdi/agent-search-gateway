import pytest

from agent_search_gateway.config import (
    ResolvedAcademicProviderConfig,
    ResolvedCredential,
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


def test_arxiv_oa_resolver_uses_its_own_registered_options() -> None:
    registry = OAResolverRegistry()
    registry.register(OAResolverRegistration("arxiv", _factory, frozenset({"max_results"})))
    resolver = resolve_oa_resolver_config(
        {"oa_resolvers": {"arxiv": {"enabled": True, "credentials": [{}], "max_results": 200}}},
        registry,
        {},
    )
    assert resolver is not None
    assert resolver.options == {"max_results": 200}


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
            {"academic_providers": {"openalex": {"enabled": True, "credentials": [{}]}}},
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
            {
                "academic_providers": {
                    "openalex": {"enabled": True, "credentials": [{"api_key_env": "AUTH_ENV"}]}
                }
            },
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
    assert provider.credentials == ()


@pytest.mark.parametrize("with_contact", [False, True])
def test_enabled_openalex_resolves_api_key_and_optional_contact(with_contact: bool) -> None:
    from agent_search_gateway.providers.academic.defaults import build_default_academic_registry

    member = {"api_key_env": "AUTH_ENV"}
    table: dict[str, object] = {"enabled": True, "credentials": [member]}
    environment = {"AUTH_ENV": "[REDACTED_SECRET]"}
    if with_contact:
        member["contact_email_env"] = "CONTACT_ENV"
        environment["CONTACT_ENV"] = "research@example.test"
    [provider] = resolve_academic_provider_config(
        {"academic_providers": {"openalex": table}},
        build_default_academic_registry(),
        environment,
    ).providers
    assert provider.credentials[0].api_key is not None
    assert provider.credentials[0].api_key.reveal() == "[REDACTED_SECRET]"
    assert "[REDACTED_SECRET]" not in repr(provider)
    if with_contact:
        assert provider.credentials[0].contact_email is not None
        assert provider.credentials[0].contact_email.reveal() == "research@example.test"
    else:
        assert provider.credentials[0].contact_email is None
    assert dict(provider.options) == {}


def test_academic_config_supports_none_optional_and_required_modes() -> None:
    data = {
        "academic_providers": {
            "no_auth": {
                "enabled": True,
                "credentials": [{"api_url": "https://no-auth.example/v1", "max_concurrency": 4}],
            },
            "optional": {"enabled": True, "credentials": [{}]},
            "required": {
                "enabled": True,
                "credentials": [{"api_key_env": "AUTH_ENV", "contact_email_env": "CONTACT_ENV"}],
            },
        }
    }
    resolved = resolve_academic_provider_config(
        data,
        _academic_registry(),
        {"AUTH_ENV": "[REDACTED_SECRET]", "CONTACT_ENV": "[REDACTED_SECRET]"},
    )
    assert resolved.providers[0].credentials[0].max_concurrency == 4
    assert [item.name for item in resolved.providers] == ["no_auth", "optional", "required"]
    assert resolved.providers[0].credentials[0].api_key is None
    assert resolved.providers[1].credentials[0].api_key is None
    assert resolved.providers[2].credentials[0].api_key is not None
    assert resolved.providers[2].credentials[0].contact_email is not None


@pytest.mark.parametrize(
    ("provider", "table", "environment"),
    [
        ("missing", {"enabled": True, "credentials": [{}]}, {}),
        ("no_auth", {"enabled": True, "credentials": [{"api_key_env": "AUTH_ENV"}]}, {}),
        ("optional", {"enabled": True, "credentials": [{"api_key_env": "MISSING_ENV"}]}, {}),
        ("optional", {"enabled": True, "credentials": [{"contact_email_env": "MISSING_ENV"}]}, {}),
        ("required", {"enabled": True, "credentials": [{}]}, {}),
        ("optional", {"enabled": True, "credentials": [{"max_concurrency": 0}]}, {}),
        ("optional", {"enabled": "yes"}, {}),
        ("optional", {"enabled": True, "credentials": [{"api_url": "ftp://invalid.example"}]}, {}),
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
    assert resolved.providers[0].credentials == ()


def test_unpaywall_is_optional_but_enabled_instance_requires_contact() -> None:
    registry = _resolver_registry()
    assert resolve_oa_resolver_config({}, registry, {}) is None
    assert (
        resolve_oa_resolver_config(
            {"oa_resolvers": {"unpaywall": {"enabled": False}}}, registry, {}
        )
        is None
    )

    data = {
        "oa_resolvers": {
            "unpaywall": {"enabled": True, "credentials": [{"contact_email_env": "CONTACT_ENV"}]}
        }
    }
    with pytest.raises(ConfigFailure):
        resolve_oa_resolver_config(data, registry, {})
    resolved = resolve_oa_resolver_config(
        data,
        registry,
        {"CONTACT_ENV": "[REDACTED_SECRET]"},
    )
    assert resolved is not None
    assert resolved.credentials[0].contact_email is not None

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
                        "credentials": [{"contact_email_env": "CONTACT_ENV"}],
                    },
                    "other": {"enabled": True, "credentials": [{}]},
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
    member = {"api_url": "https://example.test", **rate_fields}
    table = {"enabled": True, "credentials": [member]}
    provider: ResolvedAcademicProviderConfig | ResolvedOAResolverConfig | None
    if kind == "academic":
        [provider] = resolve_academic_provider_config(
            {"academic_providers": {"optional": table}},
            _academic_registry(),
            {},
        ).providers
    else:
        member["contact_email_env"] = "TEST_CONTACT"
        provider = resolve_oa_resolver_config(
            {"oa_resolvers": {"unpaywall": table}},
            _resolver_registry(),
            {"TEST_CONTACT": "test@example.test"},
        )
        assert provider is not None
    assert isinstance(provider.credentials[0].rate_policy.windows, tuple)
    windows = [(w.requests, w.period_seconds) for w in provider.credentials[0].rate_policy.windows]
    assert windows == expected_windows
    assert provider.credentials[0].rate_policy.min_interval_seconds == expected_interval
    assert dict(provider.options) == {}
    assert dict(provider.credentials[0].options) == {"api_url": "https://example.test"}


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
    member = dict(rate_fields)
    table = {"enabled": enabled, "credentials": [member]}
    with pytest.raises(ConfigFailure) as caught:
        if kind == "academic":
            resolve_academic_provider_config(
                {"academic_providers": {"optional": table}},
                _academic_registry(),
                {},
            )
        else:
            member["contact_email_env"] = "TEST_CONTACT"
            resolve_oa_resolver_config(
                {"oa_resolvers": {"unpaywall": table}},
                _resolver_registry(),
                {"TEST_CONTACT": "test@example.test"},
            )
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    prefix = "academic_providers.optional" if kind == "academic" else "oa_resolvers.unpaywall"
    assert f"{prefix}.credentials[0].{path}" in str(caught.value)
    assert "do-not-echo" not in str(caught.value)


def test_disabled_academic_rate_policy_needs_no_credentials() -> None:
    [provider] = resolve_academic_provider_config(
        {
            "academic_providers": {
                "required": {
                    "enabled": False,
                    "credentials": [
                        {
                            "rate_limits": [{"requests": 2, "period_seconds": 1}],
                            "min_interval_seconds": 0.5,
                        }
                    ],
                }
            }
        },
        _academic_registry(),
        {},
    ).providers
    assert provider.credentials == ()
    assert dict(provider.options) == {}


def test_disabled_oa_rate_policy_needs_no_credentials() -> None:
    assert (
        resolve_oa_resolver_config(
            {
                "oa_resolvers": {
                    "unpaywall": {
                        "enabled": False,
                        "credentials": [
                            {
                                "rate_limits": [{"requests": 2, "period_seconds": 1}],
                                "min_interval_seconds": 0.5,
                            }
                        ],
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
    table = {"enabled": False, "credentials": [{"rate_limits": []}]}
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
            {
                "academic_providers": {
                    "required": {"enabled": True, "credentials": [{"rate_limits": []}]}
                }
            },
            _academic_registry(),
            {},
        )
    with pytest.raises(ConfigFailure, match="requires contact_email_env"):
        resolve_oa_resolver_config(
            {
                "oa_resolvers": {
                    "unpaywall": {"enabled": True, "credentials": [{"rate_limits": []}]}
                }
            },
            _resolver_registry(),
            {},
        )


def test_academic_and_oa_constructors_default_to_empty_credential_limits() -> None:
    member = ResolvedCredential({})
    academic = ResolvedAcademicProviderConfig("name", True, {}, (member,))
    resolver = ResolvedOAResolverConfig("name", {}, (member,))
    for provider in (academic, resolver):
        assert provider.credentials[0].max_concurrency is None
        assert provider.credentials[0].rate_policy.windows == ()
        assert provider.credentials[0].rate_policy.min_interval_seconds is None
        assert provider.credential_strategy == "round_robin"
        assert provider.use_proxy is True


def test_academic_and_oa_use_proxy_preserves_credential_rate_policy_and_frozen_config() -> None:
    from dataclasses import FrozenInstanceError

    from agent_search_gateway.config import ResolvedRatePolicy

    policy = ResolvedRatePolicy(min_interval_seconds=0.5)
    member = ResolvedCredential({}, rate_policy=policy)
    academic = ResolvedAcademicProviderConfig("name", True, {}, (member,))
    resolver = ResolvedOAResolverConfig("name", {}, (member,))
    for provider in (academic, resolver):
        assert provider.credentials[0].rate_policy is policy
        assert provider.use_proxy is True
        with pytest.raises(FrozenInstanceError):
            provider.use_proxy = False  # type: ignore[misc]


@pytest.mark.parametrize("kind", ["academic", "oa"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    ("proxy_fields", "expected"),
    [({}, True), ({"use_proxy": True}, True), ({"use_proxy": False}, False)],
)
def test_academic_and_oa_provider_use_proxy_defaults_and_boolean_values(
    kind: str, enabled: bool, proxy_fields: dict[str, object], expected: bool
) -> None:
    member = {"api_url": "https://provider.example.test"}
    table: dict[str, object] = {
        "enabled": enabled,
        "credentials": [member],
        **proxy_fields,
    }
    environment = {}
    if enabled:
        member["contact_email_env"] = "TEST_CONTACT"
        environment["TEST_CONTACT"] = "test@example.test"
        if kind == "academic":
            member["api_key_env"] = "TEST_KEY"
            environment["TEST_KEY"] = "fixture-value"
    if kind == "academic":
        [provider] = resolve_academic_provider_config(
            {"academic_providers": {"required": table}}, _academic_registry(), environment
        ).providers
        assert provider.use_proxy is expected
        assert dict(provider.options) == {}
        if enabled:
            assert dict(provider.credentials[0].options) == {
                "api_url": "https://provider.example.test"
            }
        if not enabled:
            assert provider.credentials == ()
    else:
        resolver = resolve_oa_resolver_config(
            {"oa_resolvers": {"unpaywall": table}}, _resolver_registry(), environment
        )
        if enabled:
            assert resolver is not None
            assert resolver.use_proxy is expected
            assert dict(resolver.options) == {}
            assert dict(resolver.credentials[0].options) == {
                "api_url": "https://provider.example.test"
            }
        else:
            assert resolver is None


@pytest.mark.parametrize("kind", ["academic", "oa"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("value", ["false", "do-not-echo-proxy", 0, 1, None, [], {}, ()])
def test_academic_and_oa_provider_use_proxy_is_strict_boolean(
    kind: str, enabled: bool, value: object
) -> None:
    table = {"enabled": enabled, "use_proxy": value}
    with pytest.raises(ConfigFailure) as caught:
        if kind == "academic":
            resolve_academic_provider_config(
                {"academic_providers": {"required": table}}, _academic_registry(), {}
            )
        else:
            resolve_oa_resolver_config(
                {"oa_resolvers": {"unpaywall": table}}, _resolver_registry(), {}
            )
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    prefix = "academic_providers.required" if kind == "academic" else "oa_resolvers.unpaywall"
    assert f"{prefix}.use_proxy" in str(caught.value)
    assert "do-not-echo-proxy" not in str(caught.value)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("query_fields", ["ti"]),
        ("query_fields", ["abs"]),
        ("query_fields", ["abs", "ti"]),
        ("max_results", 1),
        ("max_results", 10),
        ("max_results", 100),
        ("sort_by", "relevance"),
        ("sort_by", "submittedDate"),
        ("sort_by", "lastUpdatedDate"),
        ("sort_order", "ascending"),
        ("sort_order", "descending"),
    ],
)
def test_config_accepts_arxiv_search_options(enabled: bool, option: str, value: object) -> None:
    from agent_search_gateway.providers.academic.defaults import build_default_academic_registry

    [provider] = resolve_academic_provider_config(
        {"academic_providers": {"arxiv": {"enabled": enabled, "credentials": [{}], option: value}}},
        build_default_academic_registry(),
        {},
    ).providers
    assert provider.enabled is enabled
    expected = tuple(value) if isinstance(value, list) else value
    assert dict(provider.options) == {option: expected}


def test_config_omitted_arxiv_search_options_remain_empty() -> None:
    from agent_search_gateway.providers.academic.defaults import build_default_academic_registry

    [provider] = resolve_academic_provider_config(
        {"academic_providers": {"arxiv": {"enabled": True, "credentials": [{}]}}},
        build_default_academic_registry(),
        {},
    ).providers
    assert dict(provider.options) == {}


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("query_fields", []),
        ("query_fields", ["ti", "ti"]),
        ("query_fields", ["abs", "ti", "abs"]),
        ("query_fields", ["au"]),
        ("query_fields", ["cat"]),
        ("query_fields", "ti"),
        ("query_fields", ["PRIVATE_INVALID_OPTION"]),
        ("query_fields", [1]),
        ("query_fields", [True]),
        ("query_fields", [None]),
        ("query_fields", [["ti"]]),
        ("query_fields", [{}]),
        ("query_fields", None),
        ("query_fields", ("ti",)),
        ("max_results", 0),
        ("max_results", -1),
        ("max_results", 101),
        ("max_results", True),
        ("max_results", False),
        ("max_results", 1.0),
        ("max_results", "10"),
        ("max_results", None),
        ("max_results", []),
        ("max_results", {}),
        ("sort_by", "PRIVATE_INVALID_OPTION"),
        ("sort_by", ""),
        ("sort_by", True),
        ("sort_by", 1),
        ("sort_by", 1.0),
        ("sort_by", None),
        ("sort_by", []),
        ("sort_by", {}),
        ("sort_order", "PRIVATE_INVALID_OPTION"),
        ("sort_order", ""),
        ("sort_order", True),
        ("sort_order", 1),
        ("sort_order", 1.0),
        ("sort_order", None),
        ("sort_order", []),
        ("sort_order", {}),
        ("sort_by", "Relevance"),
        ("sort_by", "submitteddate"),
        ("sort_by", "lastupdateddate"),
        ("sort_order", "Ascending"),
        ("sort_order", "Descending"),
    ],
)
def test_config_rejects_invalid_arxiv_search_options(
    enabled: bool, option: str, value: object
) -> None:
    from agent_search_gateway.providers.academic.defaults import build_default_academic_registry

    with pytest.raises(ConfigFailure) as caught:
        resolve_academic_provider_config(
            {
                "academic_providers": {
                    "arxiv": {"enabled": enabled, "credentials": [{}], option: value}
                }
            },
            build_default_academic_registry(),
            {},
        )
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert f"academic_providers.arxiv.{option}" in str(caught.value)
    assert "PRIVATE_INVALID_OPTION" not in str(caught.value)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "name", ["semantic_scholar", "openalex", "dblp", "crossref", "core", "unpaywall"]
)
@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("query_fields", ["ti"]),
        ("max_results", 10),
        ("sort_by", "relevance"),
        ("sort_order", "descending"),
    ],
)
def test_arxiv_options_are_not_shared_with_other_providers(
    enabled: bool, name: str, option: str, value: object
) -> None:
    from agent_search_gateway.providers.academic.defaults import (
        build_default_academic_registry,
        build_default_oa_resolver_registry,
    )

    with pytest.raises(ConfigFailure, match="unknown config key") as caught:
        if name == "unpaywall":
            resolve_oa_resolver_config(
                {"oa_resolvers": {name: {"enabled": enabled, "credentials": [{}], option: value}}},
                build_default_oa_resolver_registry(),
                {},
            )
        else:
            resolve_academic_provider_config(
                {
                    "academic_providers": {
                        name: {"enabled": enabled, "credentials": [{}], option: value}
                    }
                },
                build_default_academic_registry(),
                {},
            )
    assert caught.value.code is ErrorCode.CONFIG_ERROR
    assert option in str(caught.value)


def test_academic_rejects_legacy_default_max_concurrency() -> None:
    with pytest.raises(ConfigFailure, match="academic_providers.default_max_concurrency.*removed"):
        resolve_academic_provider_config(
            {"academic_providers": {"default_max_concurrency": 3}},
            _academic_registry(),
            {},
        )


@pytest.mark.parametrize("kind", ["academic", "oa"])
def test_academic_and_oa_validate_all_credentials(kind: str) -> None:
    members = [
        {"api_url": "https://first.example.test", "contact_email_env": "FIRST_EMAIL"},
        {
            "api_url": "https://second.example.test",
            "contact_email_env": "SECOND_EMAIL",
            "max_concurrency": 7,
        },
    ]
    environment = {"FIRST_EMAIL": "first@example.test", "SECOND_EMAIL": "second@example.test"}
    provider: ResolvedAcademicProviderConfig | ResolvedOAResolverConfig | None
    table = {"enabled": True, "credentials": members, "credential_strategy": "fill_first"}
    try:
        if kind == "academic":
            [provider] = resolve_academic_provider_config(
                {"academic_providers": {"optional": table}}, _academic_registry(), environment
            ).providers
        else:
            provider = resolve_oa_resolver_config(
                {"oa_resolvers": {"unpaywall": table}}, _resolver_registry(), environment
            )
            assert provider is not None
    except ConfigFailure as exc:
        pytest.fail(f"valid ordered credentials must resolve: {exc}")
    assert provider.credential_strategy == "fill_first"
    assert isinstance(provider.credentials, tuple)
    assert [dict(c.options) for c in provider.credentials] == [
        {"api_url": "https://first.example.test"},
        {"api_url": "https://second.example.test"},
    ]
    assert [c.contact_email_env for c in provider.credentials] == ["FIRST_EMAIL", "SECOND_EMAIL"]
    assert [c.contact_email.reveal() for c in provider.credentials if c.contact_email] == [
        "first@example.test",
        "second@example.test",
    ]
    assert [c.max_concurrency for c in provider.credentials] == [None, 7]
    assert dict(provider.options) == {}


@pytest.mark.parametrize("kind", ["academic", "oa"])
@pytest.mark.parametrize(
    "fields, expected",
    [
        ({}, "round_robin"),
        ({"credential_strategy": "round_robin"}, "round_robin"),
        ({"credential_strategy": "fill_first"}, "fill_first"),
    ],
)
def test_academic_and_oa_credential_strategy_defaults_and_valid_values(
    kind: str, fields: dict[str, object], expected: str
) -> None:
    provider: ResolvedAcademicProviderConfig | ResolvedOAResolverConfig | None
    table = {"enabled": True, "credentials": [{}], **fields}
    if kind == "academic":
        [provider] = resolve_academic_provider_config(
            {"academic_providers": {"no_auth": table}}, _academic_registry(), {}
        ).providers
    else:
        table["credentials"] = [{"contact_email_env": "EMAIL"}]
        provider = resolve_oa_resolver_config(
            {"oa_resolvers": {"unpaywall": table}},
            _resolver_registry(),
            {"EMAIL": "a@example.test"},
        )
        assert provider is not None
    assert provider.credential_strategy == expected


@pytest.mark.parametrize("kind", ["academic", "oa"])
@pytest.mark.parametrize(
    "value", ["fill_out", "", "do-not-echo-invalid", True, False, 0, 1.5, None, [], {}, ()]
)
def test_academic_and_oa_credential_strategy_rejects_invalid_values(
    kind: str, value: object
) -> None:
    table = {"enabled": True, "credentials": [{}], "credential_strategy": value}
    with pytest.raises(ConfigFailure) as caught:
        if kind == "academic":
            resolve_academic_provider_config(
                {"academic_providers": {"no_auth": table}}, _academic_registry(), {}
            )
        else:
            resolve_oa_resolver_config(
                {"oa_resolvers": {"unpaywall": table}}, _resolver_registry(), {}
            )
    prefix = "academic_providers.no_auth" if kind == "academic" else "oa_resolvers.unpaywall"
    assert f"{prefix}.credential_strategy" in str(caught.value)
    assert "do-not-echo-invalid" not in str(caught.value)


@pytest.mark.parametrize("kind", ["academic", "oa"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize(
    "field",
    [
        pytest.param("api_url"),
        pytest.param("api_key_env"),
        pytest.param("contact_email_env"),
        pytest.param("max_concurrency"),
        pytest.param("rate_limits"),
        pytest.param("min_interval_seconds"),
        pytest.param("default_max_concurrency"),
    ],
)
def test_academic_and_oa_reject_legacy_fields(
    kind: str, enabled: bool, mixed: bool, field: str
) -> None:
    table: dict[str, object] = {"enabled": enabled, field: "do-not-echo-legacy-value"}
    if mixed:
        table["credentials"] = [{}]
    with pytest.raises(ConfigFailure) as caught:
        if kind == "academic":
            resolve_academic_provider_config(
                {"academic_providers": {"optional": table}}, _academic_registry(), {}
            )
        else:
            resolve_oa_resolver_config(
                {"oa_resolvers": {"unpaywall": table}}, _resolver_registry(), {}
            )
    prefix = "academic_providers.optional" if kind == "academic" else "oa_resolvers.unpaywall"
    assert f"{prefix}.{field} was removed" in str(caught.value)
    assert "do-not-echo-legacy-value" not in str(caught.value)


def test_oa_rejects_legacy_default_max_concurrency() -> None:
    with pytest.raises(ConfigFailure, match="oa_resolvers.default_max_concurrency.*removed"):
        resolve_oa_resolver_config(
            {"oa_resolvers": {"default_max_concurrency": 3}}, _resolver_registry(), {}
        )


@pytest.mark.parametrize("kind", ["academic", "oa"])
@pytest.mark.parametrize("members", [None, {}, (), "", [], [None], [[]], [True]])
def test_academic_and_oa_credentials_require_nonempty_table_array(
    kind: str, members: object
) -> None:
    table = {"enabled": True, "credentials": members}
    with pytest.raises(ConfigFailure) as caught:
        if kind == "academic":
            resolve_academic_provider_config(
                {"academic_providers": {"optional": table}}, _academic_registry(), {}
            )
        else:
            resolve_oa_resolver_config(
                {"oa_resolvers": {"unpaywall": table}}, _resolver_registry(), {}
            )
    assert ".credentials" in str(caught.value)


@pytest.mark.parametrize("kind", ["academic", "oa"])
@pytest.mark.parametrize(
    "invalid, field",
    [
        ({"contact_email_env": "MISSING"}, "contact_email_env"),
        ({"contact_email_env": ""}, "contact_email_env"),
        ({"api_url": "do-not-echo-invalid-endpoint"}, "api_url"),
        ({"api_url": 123}, "api_url"),
        ({"max_concurrency": 0}, "max_concurrency"),
        ({"max_concurrency": True}, "max_concurrency"),
        ({"max_concurrency": 1.5}, "max_concurrency"),
        ({"rate_limits": "do-not-echo-rate"}, "rate_limits"),
        ({"min_interval_seconds": float("inf")}, "min_interval_seconds"),
    ],
)
def test_academic_and_oa_credentials_reject_invalid_second_member(
    kind: str, invalid: dict[str, object], field: str
) -> None:
    members = [{"contact_email_env": "EMAIL"}, {"contact_email_env": "EMAIL", **invalid}]
    table = {"enabled": True, "credentials": members}
    with pytest.raises(ConfigFailure) as caught:
        if kind == "academic":
            resolve_academic_provider_config(
                {"academic_providers": {"optional": table}},
                _academic_registry(),
                {"EMAIL": "a@example.test"},
            )
        else:
            resolve_oa_resolver_config(
                {"oa_resolvers": {"unpaywall": table}},
                _resolver_registry(),
                {"EMAIL": "a@example.test"},
            )
    prefix = "academic_providers.optional" if kind == "academic" else "oa_resolvers.unpaywall"
    assert f"{prefix}.credentials[1].{field}" in str(caught.value)
    assert "do-not-echo-" not in str(caught.value)


@pytest.mark.parametrize("kind", ["academic", "oa"])
@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"max_concurrency": 9},
        {"rate_limits": [{"requests": 2, "period_seconds": 5}]},
        {"min_interval_seconds": 0.1},
        {"max_concurrency": 9, "rate_limits": [], "min_interval_seconds": 0.1},
    ],
)
def test_academic_and_oa_optional_limits_are_per_credential(
    kind: str, fields: dict[str, object]
) -> None:
    provider: ResolvedAcademicProviderConfig | ResolvedOAResolverConfig | None
    table = {
        "enabled": True,
        "credentials": [{"contact_email_env": "EMAIL", **fields}, {"contact_email_env": "EMAIL"}],
    }
    if kind == "academic":
        [provider] = resolve_academic_provider_config(
            {"academic_providers": {"optional": table}},
            _academic_registry(),
            {"EMAIL": "a@example.test"},
        ).providers
    else:
        provider = resolve_oa_resolver_config(
            {"oa_resolvers": {"unpaywall": table}},
            _resolver_registry(),
            {"EMAIL": "a@example.test"},
        )
        assert provider is not None
    first, second = provider.credentials
    assert first.max_concurrency == fields.get("max_concurrency")
    assert len(first.rate_policy.windows) == len(fields.get("rate_limits", []))  # type: ignore[arg-type]
    assert first.rate_policy.min_interval_seconds == fields.get("min_interval_seconds")
    assert second.max_concurrency is None
    assert second.rate_policy.windows == ()
    assert second.rate_policy.min_interval_seconds is None


@pytest.mark.parametrize("kind", ["academic", "oa"])
def test_academic_and_oa_disabled_credentials_do_not_resolve_environment(kind: str) -> None:
    table = {
        "enabled": False,
        "credentials": [
            {"contact_email_env": "[REDACTED_SECRET]"},
            {"contact_email_env": "[REDACTED_SECRET]"},
        ],
    }
    if kind == "academic":
        table["credentials"] = [
            {"api_key_env": "[REDACTED_SECRET]", "contact_email_env": "[REDACTED_SECRET]"},
            {"api_key_env": "[REDACTED_SECRET]", "contact_email_env": "[REDACTED_SECRET]"},
        ]
        [provider] = resolve_academic_provider_config(
            {"academic_providers": {"required": table}}, _academic_registry(), {}
        ).providers
        assert provider.credentials == ()
    else:
        assert (
            resolve_oa_resolver_config(
                {"oa_resolvers": {"unpaywall": table}}, _resolver_registry(), {}
            )
            is None
        )


@pytest.mark.parametrize(
    "field", ["query_fields", "max_results", "use_proxy", "do-not-echo-option", "api_key"]
)
def test_academic_credentials_do_not_accept_business_or_unregistered_fields(field: str) -> None:
    from agent_search_gateway.providers.academic.defaults import build_default_academic_registry

    with pytest.raises(ConfigFailure) as caught:
        resolve_academic_provider_config(
            {
                "academic_providers": {
                    "arxiv": {"enabled": True, "credentials": [{field: "do-not-echo-value"}]}
                }
            },
            build_default_academic_registry(),
            {},
        )
    assert "academic_providers.arxiv.credentials[0]" in str(caught.value)
    assert "do-not-echo-" not in str(caught.value)


def test_arxiv_credentials_merge_validated_provider_business_options() -> None:
    from agent_search_gateway.providers.academic.defaults import build_default_academic_registry

    [provider] = resolve_academic_provider_config(
        {
            "academic_providers": {
                "arxiv": {
                    "enabled": True,
                    "query_fields": ["ti", "abs"],
                    "max_results": 10,
                    "credentials": [
                        {"api_url": "https://first.example.test"},
                        {"api_url": "https://second.example.test"},
                    ],
                }
            }
        },
        build_default_academic_registry(),
        {},
    ).providers
    assert provider.options == {"query_fields": ("ti", "abs"), "max_results": 10}
    for member in provider.credentials:
        assert member.api_key is None
        assert member.options["query_fields"] == ("ti", "abs")
        assert member.options["max_results"] == 10
    assert provider.credentials[0].options["api_url"] != provider.credentials[1].options["api_url"]


def test_openalex_credentials_validate_second_required_key() -> None:
    from agent_search_gateway.providers.academic.defaults import build_default_academic_registry

    with pytest.raises(ConfigFailure) as caught:
        resolve_academic_provider_config(
            {
                "academic_providers": {
                    "openalex": {
                        "enabled": True,
                        "credentials": [
                            {"api_key_env": "FIRST_ENV"},
                            {"api_key_env": "MISSING_ENV"},
                        ],
                    }
                }
            },
            build_default_academic_registry(),
            {"FIRST_ENV": "[REDACTED_SECRET]"},
        )
    assert "academic_providers.openalex.credentials[1].api_key_env" in str(caught.value)
    assert "[REDACTED_SECRET]" not in str(caught.value)
