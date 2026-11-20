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
            "default_max_concurrency": 2,
            "primary": {
                "protocol": "openai",
                "api_endpoint": "chat_completions",
                "api_url": "https://primary.example.test",
                "api_key_env": "LLM_A_ENV",
            },
            "secondary": {
                "protocol": "openai",
                "api_endpoint": "chat_completions",
                "api_url": "https://secondary.example.test",
                "api_key_env": "LLM_B_ENV",
                "max_concurrency": 4,
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

    assert resolved.default_max_concurrency == 2
    assert [(provider.name, provider.max_concurrency) for provider in resolved.providers] == [
        ("primary", 2),
        ("secondary", 4),
    ]
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
    primary["max_concurrency"] = 0


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
