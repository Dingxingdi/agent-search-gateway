"""TOML and environment configuration resolution."""

import copy
import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast
from urllib.parse import urlsplit

from .errors import ConfigFailure, ErrorCode
from .models import AgentInvocation, LLMInvocation, RetryPolicy
from .observability import SecretValue
from .providers.academic.registry import (
    AcademicProviderRegistration,
    AcademicProviderRegistry,
    OAResolverRegistration,
    OAResolverRegistry,
    Requirement,
)
from .providers.registry import WEB_ENDPOINT_KEYS, ProviderRegistry, WebProviderRegistration

_WEB_PROVIDER_KEYS = frozenset(
    {"use_proxy", "enable_search", "enable_fetch", "credentials", "credential_strategy"}
)
_WEB_CREDENTIAL_KEYS = frozenset(
    {"api_key_env", "max_concurrency", "rate_limits", "min_interval_seconds"}
)
_ACADEMIC_SHARED_KEYS = frozenset({"use_proxy", "enabled", "credentials", "credential_strategy"})
_ACADEMIC_CREDENTIAL_KEYS = _WEB_CREDENTIAL_KEYS | {"contact_email_env"}
_ACADEMIC_LEGACY_KEYS = _ACADEMIC_CREDENTIAL_KEYS | {"api_url", "default_max_concurrency"}
_ACADEMIC_RESERVED_OPTIONS = frozenset({"executor", "api_key", "contact_email", "name"})


@dataclass(frozen=True, slots=True)
class ResolvedRateWindow:
    requests: int
    period_seconds: float


@dataclass(frozen=True, slots=True)
class ResolvedRatePolicy:
    windows: tuple[ResolvedRateWindow, ...] = ()
    min_interval_seconds: float | None = None


CredentialStrategy = Literal["round_robin", "fill_first"]


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    options: Mapping[str, object] = field(repr=False)
    api_key_env: str | None = None
    api_key: SecretValue | None = None
    contact_email_env: str | None = None
    contact_email: SecretValue | None = None
    max_concurrency: int | None = None
    rate_policy: ResolvedRatePolicy = ResolvedRatePolicy()

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


def _resolve_credential_strategy(table: Mapping[str, object], label: str) -> CredentialStrategy:
    value = table.get("credential_strategy", "round_robin")
    if isinstance(value, str) and value in ("round_robin", "fill_first"):
        return cast(CredentialStrategy, value)
    raise _config_error(f"{label}.credential_strategy must be round_robin or fill_first")


def _resolve_credential_limits(
    table: Mapping[str, object], label: str
) -> tuple[int | None, ResolvedRatePolicy]:
    capacity = (
        _positive_int(table["max_concurrency"], f"{label}.max_concurrency")
        if "max_concurrency" in table
        else None
    )
    return capacity, _resolve_rate_policy(table, label)


def _positive_rate_seconds(value: object, label: str) -> float:
    try:
        return _positive_float(value, label)
    except OverflowError:
        raise _config_error(f"{label} must be a positive number") from None


def _resolve_rate_policy(table: Mapping[str, object], label: str) -> ResolvedRatePolicy:
    raw_windows = table.get("rate_limits", [])
    if not isinstance(raw_windows, list):
        raise _config_error(f"{label}.rate_limits must be an array of tables")
    windows: list[ResolvedRateWindow] = []
    for index, raw_window in enumerate(raw_windows):
        window_label = f"{label}.rate_limits[{index}]"
        window = _require_mapping(raw_window, window_label)
        if set(window) - {"requests", "period_seconds"}:
            raise _config_error(f"{window_label} only accepts requests and period_seconds")
        for key in ("requests", "period_seconds"):
            if key not in window:
                raise _config_error(f"{window_label}.{key} is required")
        windows.append(
            ResolvedRateWindow(
                _positive_int(window["requests"], f"{window_label}.requests"),
                _positive_rate_seconds(window["period_seconds"], f"{window_label}.period_seconds"),
            )
        )
    interval = (
        _positive_rate_seconds(table["min_interval_seconds"], f"{label}.min_interval_seconds")
        if "min_interval_seconds" in table
        else None
    )
    return ResolvedRatePolicy(tuple(windows), interval)


@dataclass(frozen=True, slots=True)
class ResolvedWebProviderConfig:
    name: str
    enable_search: bool
    enable_fetch: bool
    options: Mapping[str, object]
    credentials: tuple[ResolvedCredential, ...]
    credential_strategy: CredentialStrategy = "round_robin"
    use_proxy: bool = True


@dataclass(frozen=True, slots=True)
class ResolvedWebProviderGroup:
    providers: tuple[ResolvedWebProviderConfig, ...]
    fetch_order: tuple[str, ...] = ()


def _config_error(message: str) -> ConfigFailure:
    return ConfigFailure(ErrorCode.CONFIG_ERROR, message)


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise _config_error(f"{label} must be a table")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _config_error(f"{label} must be a positive integer")
    return value


def _optional_bool(table: Mapping[str, object], key: str) -> bool:
    value = table.get(key, False)
    if not isinstance(value, bool):
        raise _config_error(f"{key} must be a boolean")
    return value


def _resolve_use_proxy(table: Mapping[str, object], label: str) -> bool:
    value = table.get("use_proxy", True)
    if type(value) is not bool:
        raise _config_error(f"{label}.use_proxy must be a boolean")
    return value


def _validate_choice_list(
    value: object,
    label: str,
    choices: frozenset[str],
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _config_error(f"{label} must be an array of strings")
    if not value and not allow_empty:
        raise _config_error(f"{label} must be a non-empty array")
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or item not in choices:
            allowed = ", ".join(sorted(choices))
            raise _config_error(f"{label}[{index}] must be one of: {allowed}")
        if item in seen:
            raise _config_error(f"{label} must not contain duplicate values")
        seen.add(item)
    return tuple(value)


def _validate_firecrawl_options(options: dict[str, object]) -> None:
    label = "web_providers.firecrawl"
    if "search_limit" in options:
        limit = _positive_int(options["search_limit"], f"{label}.search_limit")
        if limit > 100:
            raise _config_error(f"{label}.search_limit must be at most 100")
    if "search_highlights" in options and not isinstance(options["search_highlights"], bool):
        raise _config_error(f"{label}.search_highlights must be a boolean")
    if "scrape_formats" in options:
        options["scrape_formats"] = _validate_choice_list(
            options["scrape_formats"], f"{label}.scrape_formats", frozenset({"markdown", "rawHtml"})
        )
    if "scrape_parsers" in options:
        options["scrape_parsers"] = _validate_choice_list(
            options["scrape_parsers"],
            f"{label}.scrape_parsers",
            frozenset({"pdf", "image"}),
            allow_empty=True,
        )


def _validate_options(
    registration: WebProviderRegistration,
    options: dict[str, object],
) -> None:
    unknown = set(options) - registration.allowed_config_keys
    if unknown:
        names = ", ".join(sorted(unknown))
        raise _config_error(f"unknown config key(s) for {registration.name}: {names}")
    if registration.name == "firecrawl":
        _validate_firecrawl_options(options)


def _resolve_web_credentials(
    name: str,
    table: Mapping[str, object],
    registration: WebProviderRegistration | None,
    options: Mapping[str, object],
    environ: Mapping[str, str],
    *,
    enabled: bool,
) -> tuple[ResolvedCredential, ...]:
    label = f"web_providers.{name}.credentials"
    raw_members = table.get("credentials", [])
    if not isinstance(raw_members, list) or (enabled and not raw_members):
        raise _config_error(f"{label} must be a non-empty array of tables when enabled")
    credentials: list[ResolvedCredential] = []
    for index, raw_member in enumerate(raw_members):
        member_label = f"{label}[{index}]"
        member = _require_mapping(raw_member, member_label)
        overrides = {key: value for key, value in member.items() if key not in _WEB_CREDENTIAL_KEYS}
        if registration and set(overrides) - registration.credential_option_keys:
            raise _config_error(f"{member_label} contains an unknown credential option")
        capacity, rate_policy = _resolve_credential_limits(member, member_label)
        if not enabled or registration is None:
            continue

        requirement: Requirement = "none"
        if registration.requires_api_key:
            requirement = "required"
        elif registration.supports_optional_api_key:
            requirement = "optional"
        env_name, auth = _resolve_environment_value(
            member, key="api_key_env", requirement=requirement, label=member_label, environ=environ
        )
        effective = {**options, **overrides}
        for key in registration.credential_option_keys & effective.keys():
            _required_string(effective, key, member_label)
        if registration.validate_config is not None:
            try:
                registration.validate_config(effective, auth is not None)
            except ConfigFailure as exc:
                raise _config_error(f"{member_label}: {exc.message}") from exc
        credentials.append(
            ResolvedCredential(
                options=effective,
                api_key_env=env_name,
                api_key=auth,
                max_concurrency=capacity,
                rate_policy=rate_policy,
            )
        )
    return tuple(credentials)


def _resolve_one_web_provider(
    name: str,
    table: Mapping[str, object],
    registry: ProviderRegistry,
    environ: Mapping[str, str],
) -> ResolvedWebProviderConfig:
    label = f"web_providers.{name}"
    for key in table:
        if key in _WEB_CREDENTIAL_KEYS or key in WEB_ENDPOINT_KEYS:
            raise _config_error(f"{label}.{key} was removed; move it into credentials")
    use_proxy = _resolve_use_proxy(table, label)
    enable_search = _optional_bool(table, "enable_search")
    enable_fetch = _optional_bool(table, "enable_fetch")
    enabled = enable_search or enable_fetch
    options = {key: value for key, value in table.items() if key not in _WEB_PROVIDER_KEYS}
    registration = registry.get(name)
    if registration is not None:
        _validate_options(registration, options)
        if enable_search and not registration.capabilities.search:
            raise _config_error(f"web provider {name} does not support search")
        if enable_fetch and not registration.capabilities.fetch:
            raise _config_error(f"web provider {name} does not support fetch")
    elif enabled:
        raise _config_error(f"unknown enabled web provider: {name}")

    return ResolvedWebProviderConfig(
        name=name,
        enable_search=enable_search,
        enable_fetch=enable_fetch,
        options=MappingProxyType(options),
        credential_strategy=_resolve_credential_strategy(table, label),
        credentials=_resolve_web_credentials(
            name, table, registration, options, environ, enabled=enabled
        ),
        use_proxy=use_proxy,
    )


def resolve_web_provider_config(
    data: Mapping[str, object],
    registry: ProviderRegistry,
    environ: Mapping[str, str],
) -> ResolvedWebProviderGroup:
    web_table = _require_mapping(data.get("web_providers", {}), "web_providers")
    if "default_max_concurrency" in web_table:
        raise _config_error(
            "web_providers.default_max_concurrency was removed; set max_concurrency in credentials"
        )
    fetch_order = _validate_choice_list(
        web_table.get("fetch_order", []),
        "web_providers.fetch_order",
        frozenset(item.name for item in registry.for_stage("fetch")),
        allow_empty=True,
    )
    providers: list[ResolvedWebProviderConfig] = []
    for name, value in web_table.items():
        if name == "fetch_order":
            continue
        provider_table = _require_mapping(value, f"web_providers.{name}")
        providers.append(_resolve_one_web_provider(name, provider_table, registry, environ))
    return ResolvedWebProviderGroup(tuple(providers), fetch_order)


@dataclass(frozen=True, slots=True)
class ResolvedAcademicProviderConfig:
    name: str
    enabled: bool
    options: Mapping[str, object]
    credentials: tuple[ResolvedCredential, ...]
    credential_strategy: CredentialStrategy = "round_robin"
    use_proxy: bool = True


@dataclass(frozen=True, slots=True)
class ResolvedAcademicProviderGroup:
    providers: tuple[ResolvedAcademicProviderConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedOAResolverConfig:
    name: str
    options: Mapping[str, object]
    credentials: tuple[ResolvedCredential, ...]
    credential_strategy: CredentialStrategy = "round_robin"
    use_proxy: bool = True


def _enabled(table: Mapping[str, object], label: str) -> bool:
    value = table.get("enabled", False)
    if not isinstance(value, bool):
        raise _config_error(f"{label}.enabled must be a boolean")
    return value


def _validate_http_url(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise _config_error(f"{label} must be a non-empty HTTP(S) URL")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise _config_error(f"{label} must be a non-empty HTTP(S) URL") from exc
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise _config_error(f"{label} must be a non-empty HTTP(S) URL")


def _validate_academic_options(
    name: str,
    registration: AcademicProviderRegistration | OAResolverRegistration,
    options: dict[str, object],
    *,
    label: str | None = None,
) -> None:
    label = label or name
    reserved = set(options) & _ACADEMIC_RESERVED_OPTIONS
    if reserved:
        names = ", ".join(sorted(reserved))
        raise _config_error(f"reserved config key(s) for {name}: {names}")
    unknown = set(options) - registration.allowed_config_keys
    if unknown:
        names = ", ".join(sorted(unknown))
        raise _config_error(f"unknown config key(s) for {name}: {names}")
    if "api_url" in options:
        _validate_http_url(options["api_url"], f"{label}.api_url")
    if isinstance(registration, AcademicProviderRegistration) and name == "arxiv":
        if "query_fields" in options:
            options["query_fields"] = _validate_choice_list(
                options["query_fields"], f"{label}.query_fields", frozenset({"ti", "abs"})
            )
        if "max_results" in options:
            limit = _positive_int(options["max_results"], f"{label}.max_results")
            if limit > 100:
                raise _config_error(f"{label}.max_results must be at most 100")
        for key, choices in (
            ("sort_by", ("relevance", "submittedDate", "lastUpdatedDate")),
            ("sort_order", ("ascending", "descending")),
        ):
            if key in options:
                value = options[key]
                if not isinstance(value, str) or value not in choices:
                    allowed = ", ".join(choices)
                    raise _config_error(f"{label}.{key} must be one of: {allowed}")


def _resolve_environment_value(
    table: Mapping[str, object],
    *,
    key: str,
    requirement: Requirement,
    label: str,
    environ: Mapping[str, str],
) -> tuple[str | None, SecretValue | None]:
    if requirement == "none":
        if key in table:
            raise _config_error(f"{label} does not accept {key}")
        return None, None
    if key not in table:
        if requirement == "required":
            raise _config_error(f"{label} requires {key}")
        return None, None
    env_name = table[key]
    if not isinstance(env_name, str) or not env_name.strip():
        raise _config_error(f"{label}.{key} must be a non-empty string")
    resolved_value = environ.get(env_name)
    if not resolved_value:
        raise _config_error(f"{label}.{key}: environment variable {env_name} is required")
    return env_name, SecretValue(resolved_value)


def _resolve_academic_credentials(
    table: Mapping[str, object],
    registration: AcademicProviderRegistration | OAResolverRegistration,
    options: Mapping[str, object],
    environ: Mapping[str, str],
    *,
    label: str,
    enabled: bool,
) -> tuple[ResolvedCredential, ...]:
    raw_members = table.get("credentials", [])
    if not isinstance(raw_members, list) or (enabled and not raw_members):
        raise _config_error(f"{label}.credentials must be a non-empty array of tables when enabled")
    credentials: list[ResolvedCredential] = []
    for index, raw_member in enumerate(raw_members):
        member_label = f"{label}.credentials[{index}]"
        member = _require_mapping(raw_member, member_label)
        overrides = {
            key: value for key, value in member.items() if key not in _ACADEMIC_CREDENTIAL_KEYS
        }
        if set(overrides) - registration.credential_option_keys:
            raise _config_error(f"{member_label} contains an unknown credential option")
        capacity, rate_policy = _resolve_credential_limits(member, member_label)
        effective = {**options, **overrides}
        _validate_academic_options(registration.name, registration, effective, label=member_label)
        values: dict[str, tuple[str | None, SecretValue | None]] = {}
        for key, requirement in (
            ("api_key_env", registration.authentication),
            ("contact_email_env", registration.contact),
        ):
            if enabled:
                values[key] = _resolve_environment_value(
                    member, key=key, requirement=requirement, label=member_label, environ=environ
                )
            elif key in member:
                if requirement == "none":
                    raise _config_error(f"{member_label} does not accept {key}")
                _required_string(member, key, member_label)
        if not enabled:
            continue
        auth_env, auth_value = values["api_key_env"]
        contact_env, contact_value = values["contact_email_env"]
        credentials.append(
            ResolvedCredential(
                options=effective,
                api_key_env=auth_env,
                api_key=auth_value,
                contact_email_env=contact_env,
                contact_email=contact_value,
                max_concurrency=capacity,
                rate_policy=rate_policy,
            )
        )
    return tuple(credentials)


def _resolve_academic_entry(
    table: Mapping[str, object],
    registration: AcademicProviderRegistration | OAResolverRegistration,
    environ: Mapping[str, str],
    label: str,
) -> tuple[bool, bool, dict[str, object], tuple[ResolvedCredential, ...], CredentialStrategy]:
    for key in table:
        if key in _ACADEMIC_LEGACY_KEYS:
            raise _config_error(f"{label}.{key} was removed; move it into credentials")
    enabled = _enabled(table, label)
    use_proxy = _resolve_use_proxy(table, label)
    strategy = _resolve_credential_strategy(table, label)
    raw_options = {key: value for key, value in table.items() if key not in _ACADEMIC_SHARED_KEYS}
    options = dict(raw_options)
    _validate_academic_options(registration.name, registration, options, label=label)
    credentials = _resolve_academic_credentials(
        table, registration, raw_options, environ, label=label, enabled=enabled
    )
    return enabled, use_proxy, options, credentials, strategy


def _reject_academic_default_limit(group: Mapping[str, object], label: str) -> None:
    if "default_max_concurrency" in group:
        raise _config_error(
            f"{label}.default_max_concurrency was removed; set max_concurrency in credentials"
        )


def resolve_academic_provider_config(
    data: Mapping[str, object],
    registry: AcademicProviderRegistry,
    environ: Mapping[str, str],
) -> ResolvedAcademicProviderGroup:
    group = _require_mapping(data.get("academic_providers", {}), "academic_providers")
    _reject_academic_default_limit(group, "academic_providers")
    providers: list[ResolvedAcademicProviderConfig] = []
    for name, raw_value in group.items():
        label = f"academic_providers.{name}"
        table = _require_mapping(raw_value, label)
        registration = registry.get(name)
        if registration is None:
            raise _config_error(f"unknown academic provider: {name}")
        enabled, use_proxy, options, credentials, strategy = _resolve_academic_entry(
            table, registration, environ, label
        )
        providers.append(
            ResolvedAcademicProviderConfig(
                name=name,
                enabled=enabled,
                options=MappingProxyType(options),
                credentials=credentials,
                credential_strategy=strategy,
                use_proxy=use_proxy,
            )
        )
    return ResolvedAcademicProviderGroup(tuple(providers))


def resolve_oa_resolver_config(
    data: Mapping[str, object],
    registry: OAResolverRegistry,
    environ: Mapping[str, str],
) -> ResolvedOAResolverConfig | None:
    group = _require_mapping(data.get("oa_resolvers", {}), "oa_resolvers")
    _reject_academic_default_limit(group, "oa_resolvers")
    resolved: ResolvedOAResolverConfig | None = None
    for name, raw_value in group.items():
        label = f"oa_resolvers.{name}"
        table = _require_mapping(raw_value, label)
        registration = registry.get(name)
        if registration is None:
            raise _config_error(f"unknown OA resolver: {name}")
        enabled, use_proxy, options, credentials, strategy = _resolve_academic_entry(
            table, registration, environ, label
        )
        if not enabled:
            continue
        if resolved is not None:
            raise _config_error("only one OA resolver may be enabled")
        resolved = ResolvedOAResolverConfig(
            name=name,
            options=MappingProxyType(options),
            credentials=credentials,
            credential_strategy=strategy,
            use_proxy=use_proxy,
        )
    return resolved


@dataclass(frozen=True, slots=True)
class LLMProviderConfig:
    name: str
    protocol: str
    api_endpoint: str
    credentials: tuple[ResolvedCredential, ...]
    credential_strategy: CredentialStrategy = "round_robin"
    use_proxy: bool = True


@dataclass(frozen=True, slots=True)
class ResolvedLLMConfig:
    providers: tuple[LLMProviderConfig, ...]
    search_invocations: tuple[LLMInvocation, ...]
    judge: LLMInvocation
    safety: LLMInvocation
    content_clean: LLMInvocation
    focus_summary: LLMInvocation


@dataclass(frozen=True, slots=True)
class AgentProviderConfig:
    name: str
    harness: str
    command: str
    max_concurrency: int | None
    timeout_seconds: float
    pass_env: tuple[str, ...]
    rate_policy: ResolvedRatePolicy = ResolvedRatePolicy()


@dataclass(frozen=True, slots=True)
class ResolvedAgentConfig:
    providers: tuple[AgentProviderConfig, ...] = ()
    search_invocations: tuple[AgentInvocation, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedInputLimits:
    llm_request_body_bytes: int = 131072
    agent_prompt_bytes: int = 98304


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    web: ResolvedWebProviderGroup
    llm: ResolvedLLMConfig
    retry: RetryPolicy
    academic: ResolvedAcademicProviderGroup = field(default_factory=ResolvedAcademicProviderGroup)
    oa_resolver: ResolvedOAResolverConfig | None = None
    agent: ResolvedAgentConfig = field(default_factory=ResolvedAgentConfig)
    input_limits: ResolvedInputLimits = ResolvedInputLimits()


def _reject_removed_default_limit(table: Mapping[str, object], label: str) -> None:
    if "default_max_concurrency" in table:
        raise _config_error(
            f"{label}.default_max_concurrency was removed; configure explicit max_concurrency"
        )


def _required_string(table: Mapping[str, object], key: str, label: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _config_error(f"{label}.{key} must be a non-empty string")
    return value


def _first_defined(key: str, *tables: Mapping[str, object]) -> object | None:
    for table in tables:
        if key in table:
            return table[key]
    return None


def _resolve_extra_body(*tables: Mapping[str, object]) -> dict[str, object]:
    value = _first_defined("extra_body", *tables)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _config_error("LLM extra_body must be a table")
    return copy.deepcopy(value)


def _resolve_invocation(
    label: str,
    scopes: tuple[Mapping[str, object], ...],
    *,
    require_explicit_provider: bool = False,
) -> LLMInvocation:
    provider_value = (
        scopes[0].get("provider")
        if require_explicit_provider
        else _first_defined("provider", *scopes)
    )
    model_value = _first_defined("model", *scopes)
    if not isinstance(provider_value, str) or not provider_value.strip():
        raise _config_error(f"{label} requires provider")
    if not isinstance(model_value, str) or not model_value.strip():
        raise _config_error(f"{label} requires model")
    return LLMInvocation(
        provider=provider_value,
        model=model_value,
        extra_body=_resolve_extra_body(*scopes),
    )


def _search_invocations(
    data: Mapping[str, object],
    global_table: Mapping[str, object],
) -> tuple[LLMInvocation, ...]:
    search_table = _require_mapping(data.get("search_llm", {}), "search_llm")
    _reject_removed_default_limit(search_table, "search_llm")
    raw_entries = search_table.get("providers", [])
    if not isinstance(raw_entries, list):
        raise _config_error("search_llm.providers must be an array of tables")

    resolved: list[LLMInvocation] = []
    for index, raw_entry in enumerate(raw_entries):
        entry = _require_mapping(raw_entry, f"search_llm.providers[{index}]")
        _reject_removed_default_limit(entry, f"search_llm.providers[{index}]")
        resolved.append(
            _resolve_invocation(
                f"search_llm.providers[{index}]",
                (entry, global_table),
                require_explicit_provider=True,
            )
        )
    return tuple(resolved)


def _fetch_invocations(
    data: Mapping[str, object],
    global_table: Mapping[str, object],
) -> tuple[LLMInvocation, LLMInvocation, LLMInvocation, LLMInvocation]:
    fetch_table = _require_mapping(data.get("fetch_llm", {}), "fetch_llm")
    _reject_removed_default_limit(fetch_table, "fetch_llm")
    names = ("judge", "safety", "content_clean", "focus_summary")
    invocations: list[LLMInvocation] = []
    for name in names:
        stage_table = _require_mapping(fetch_table.get(name, {}), f"fetch_llm.{name}")
        _reject_removed_default_limit(stage_table, f"fetch_llm.{name}")
        invocations.append(
            _resolve_invocation(
                f"fetch_llm.{name}",
                (stage_table, fetch_table, global_table),
            )
        )
    return invocations[0], invocations[1], invocations[2], invocations[3]


def _resolve_referenced_llm_providers(
    llm_table: Mapping[str, object],
    referenced: set[str],
    environ: Mapping[str, str],
) -> tuple[LLMProviderConfig, ...]:
    providers: list[LLMProviderConfig] = []
    for name, raw_value in llm_table.items():
        label = f"llm_providers.{name}"
        if isinstance(raw_value, Mapping):
            for key in raw_value:
                if key in _ACADEMIC_LEGACY_KEYS | {"api_key", "contact_email", "secret"}:
                    raise _config_error(f"{label}.{key} was removed; move it into credentials")
        if name not in referenced:
            continue
        table = _require_mapping(raw_value, f"llm_providers.{name}")
        use_proxy = _resolve_use_proxy(table, f"llm_providers.{name}")
        protocol = _required_string(table, "protocol", f"llm_providers.{name}")
        endpoint = _required_string(table, "api_endpoint", f"llm_providers.{name}")
        if protocol != "openai" or endpoint != "chat_completions":
            raise _config_error(f"unsupported LLM provider protocol/endpoint: {name}")
        raw_members = table.get("credentials")
        if not isinstance(raw_members, list) or not raw_members:
            raise _config_error(
                f"llm_providers.{name}.credentials must be a non-empty array of tables"
            )
        credentials: list[ResolvedCredential] = []
        for index, raw_member in enumerate(raw_members):
            label = f"llm_providers.{name}.credentials[{index}]"
            member = _require_mapping(raw_member, label)
            _reject_removed_default_limit(member, label)
            if set(member) - (_WEB_CREDENTIAL_KEYS | {"api_url"}):
                raise _config_error(f"{label} contains an unknown credential option")
            api_url = _required_string(member, "api_url", label)
            auth_env, auth_value = _resolve_environment_value(
                member, key="api_key_env", requirement="required", label=label, environ=environ
            )
            capacity, rate_policy = _resolve_credential_limits(member, label)
            credentials.append(
                ResolvedCredential(
                    options={"api_url": api_url},
                    api_key_env=auth_env,
                    api_key=auth_value,
                    max_concurrency=capacity,
                    rate_policy=rate_policy,
                )
            )
        providers.append(
            LLMProviderConfig(
                name=name,
                protocol=protocol,
                api_endpoint=endpoint,
                credentials=tuple(credentials),
                credential_strategy=_resolve_credential_strategy(table, f"llm_providers.{name}"),
                use_proxy=use_proxy,
            )
        )

    configured = {provider.name for provider in providers}
    missing = referenced - configured
    if missing:
        raise _config_error(f"unknown referenced LLM provider(s): {', '.join(sorted(missing))}")
    return tuple(providers)


def resolve_llm_config(
    data: Mapping[str, object],
    environ: Mapping[str, str],
) -> ResolvedLLMConfig:
    llm_table = _require_mapping(data.get("llm_providers", {}), "llm_providers")
    if "default_max_concurrency" in llm_table:
        raise _config_error(
            "llm_providers.default_max_concurrency was removed; set max_concurrency in credentials"
        )
    global_table = _require_mapping(data.get("global_default_llm", {}), "global_default_llm")
    _reject_removed_default_limit(global_table, "global_default_llm")
    search_invocations = _search_invocations(data, global_table)
    judge, safety, content_clean, focus_summary = _fetch_invocations(data, global_table)
    all_invocations = (*search_invocations, judge, safety, content_clean, focus_summary)
    providers = _resolve_referenced_llm_providers(
        llm_table,
        {invocation.provider for invocation in all_invocations},
        environ,
    )
    return ResolvedLLMConfig(
        providers=providers,
        search_invocations=search_invocations,
        judge=judge,
        safety=safety,
        content_clean=content_clean,
        focus_summary=focus_summary,
    )


def _positive_float(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise _config_error(f"{label} must be a positive number")
    return float(value)


def resolve_agent_config(data: Mapping[str, object]) -> ResolvedAgentConfig:
    search = _require_mapping(data.get("search_agent", {}), "search_agent")
    _reject_removed_default_limit(search, "search_agent")
    if set(search) - {"providers"}:
        raise _config_error("search_agent contains unsupported fields")
    group = _require_mapping(data.get("agent_providers", {}), "agent_providers")
    if "default_max_concurrency" in group:
        raise _config_error(
            "agent_providers.default_max_concurrency was removed; set max_concurrency on each alias"
        )
    entries = search.get("providers", [])
    if not isinstance(entries, list):
        raise _config_error("search_agent.providers must be an array of tables")
    invocations: list[AgentInvocation] = []
    for index, raw_entry in enumerate(entries):
        label = f"search_agent.providers[{index}]"
        entry = _require_mapping(raw_entry, label)
        _reject_removed_default_limit(entry, label)
        if set(entry) - {"provider", "model"}:
            raise _config_error(f"{label} contains unsupported fields")
        invocations.append(
            AgentInvocation(
                _required_string(entry, "provider", label),
                _required_string(entry, "model", label) if "model" in entry else None,
            )
        )
    referenced = {invocation.provider for invocation in invocations}
    providers: list[AgentProviderConfig] = []
    for name, raw_table in group.items():
        label = f"agent_providers.{name}"
        if isinstance(raw_table, Mapping):
            if "default_max_concurrency" in raw_table:
                raise _config_error(
                    f"{label}.default_max_concurrency was removed; set max_concurrency on the alias"
                )
            for key in raw_table:
                if key in {
                    "credentials",
                    "credential_strategy",
                    "api_url",
                    "api_key_env",
                    "api_key",
                    "contact_email_env",
                    "contact_email",
                    "secret",
                }:
                    raise _config_error(f"{label}.{key} is not supported for CLI Agent providers")
        if name not in referenced:
            continue
        table = _require_mapping(raw_table, label)
        if set(table) - {
            "harness",
            "command",
            "max_concurrency",
            "rate_limits",
            "min_interval_seconds",
            "timeout_seconds",
            "pass_env",
        }:
            raise _config_error(f"{label} contains unsupported fields")
        harness = _required_string(table, "harness", label)
        if harness != "antigravity":
            raise _config_error(f"{label}.harness must be antigravity")
        command = _required_string(table, "command", label) if "command" in table else "agy"
        if (
            command != command.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in command)
            or any(character in command for character in "|&;<>`$()")
            or re.search(r"(?:^|\s)-", command)
            or ("/" not in command and any(character.isspace() for character in command))
        ):
            raise _config_error(
                f"{label}.command must be one executable name or path without flags"
            )
        timeout_value = table.get("timeout_seconds", 300)
        timeout = _positive_float(timeout_value, f"{label}.timeout_seconds")
        pass_env = table.get("pass_env", [])
        if not isinstance(pass_env, list) or not all(
            isinstance(item, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item)
            for item in pass_env
        ):
            raise _config_error(f"{label}.pass_env must be an array of environment variable names")
        capacity, rate_policy = _resolve_credential_limits(table, label)
        providers.append(
            AgentProviderConfig(
                name=name,
                harness=harness,
                command=command,
                max_concurrency=capacity,
                rate_policy=rate_policy,
                timeout_seconds=timeout,
                pass_env=tuple(pass_env),
            )
        )
    if referenced - {provider.name for provider in providers}:
        raise _config_error("search_agent references an unconfigured Agent provider")
    return ResolvedAgentConfig(tuple(providers), tuple(invocations))


def resolve_retry_policy(data: Mapping[str, object]) -> RetryPolicy:
    retry_table = _require_mapping(data.get("retry", {}), "retry")
    return RetryPolicy(
        max_attempts=_positive_int(retry_table.get("max_attempts", 3), "retry.max_attempts"),
        base_delay_seconds=_positive_float(
            retry_table.get("base_delay_seconds", 0.25),
            "retry.base_delay_seconds",
        ),
        max_delay_seconds=_positive_float(
            retry_table.get("max_delay_seconds", 2.0),
            "retry.max_delay_seconds",
        ),
        request_timeout_seconds=_positive_float(
            retry_table.get("request_timeout_seconds", 30.0),
            "retry.request_timeout_seconds",
        ),
    )


def _resolve_input_limits(data: Mapping[str, object]) -> ResolvedInputLimits:
    table = _require_mapping(data.get("input_limits", {}), "input_limits")
    if set(table) - {"llm_request_body_bytes", "agent_prompt_bytes"}:
        raise _config_error(
            "input_limits only accepts llm_request_body_bytes and agent_prompt_bytes"
        )
    return ResolvedInputLimits(
        llm_request_body_bytes=_positive_int(
            table.get("llm_request_body_bytes", 131072), "input_limits.llm_request_body_bytes"
        ),
        agent_prompt_bytes=_positive_int(
            table.get("agent_prompt_bytes", 98304), "input_limits.agent_prompt_bytes"
        ),
    )


def resolve_config(
    data: Mapping[str, object],
    registry: ProviderRegistry,
    environ: Mapping[str, str],
    *,
    academic_registry: AcademicProviderRegistry | None = None,
    oa_resolver_registry: OAResolverRegistry | None = None,
) -> ResolvedConfig:
    academic = (
        resolve_academic_provider_config(data, academic_registry, environ)
        if academic_registry is not None
        else ResolvedAcademicProviderGroup()
    )
    oa_resolver = (
        resolve_oa_resolver_config(data, oa_resolver_registry, environ)
        if oa_resolver_registry is not None
        else None
    )
    return ResolvedConfig(
        web=resolve_web_provider_config(data, registry, environ),
        llm=resolve_llm_config(data, environ),
        retry=resolve_retry_policy(data),
        academic=academic,
        oa_resolver=oa_resolver,
        agent=resolve_agent_config(data),
        input_limits=_resolve_input_limits(data),
    )


def load_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _config_error(f"failed to load config: {exc}") from exc
