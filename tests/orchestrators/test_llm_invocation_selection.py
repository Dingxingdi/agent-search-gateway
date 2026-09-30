from pathlib import Path

import pytest

from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.errors import ErrorCode, ExecutionFailure, InputFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_store import URLStore
from tests.support.fakes import FakeLLMClient


def _invocations() -> tuple[LLMInvocation, ...]:
    return (
        LLMInvocation("openai_main", "gpt-5", {"temperature": 0.1}),
        LLMInvocation("openai_main", "gpt-5-mini"),
        LLMInvocation("deepseek_main", "deepseek-v3"),
        LLMInvocation("azure_main", "gpt-5"),
    )


def _orchestrator(tmp_path: Path, invocations: tuple[LLMInvocation, ...]) -> SearchOrchestrator:
    fallback = LLMInvocation("fetch_only", "global")
    client = FakeLLMClient(fallback.provider)
    return SearchOrchestrator(
        keyword_providers=(),
        llm_invocations=invocations,
        quotas=ProviderQuotaManager(web_limits={}, llm_limits={}),
        stages=LLMStages(
            {client.name: client},
            judge=fallback,
            safety=fallback,
            content_clean=fallback,
            focus_summary=fallback,
        ),
        store=URLStore(),
        result_writer=ResultWriter(tmp_path / "results"),
    )


def test_llm_provider_selection_keeps_all_models(tmp_path: Path) -> None:
    invocations = _invocations()
    selected = _orchestrator(tmp_path, invocations)._select_llm_invocations(
        provider="openai_main", model=None
    )
    assert selected == invocations[:2]
    assert all(actual is expected for actual, expected in zip(selected, invocations, strict=False))


@pytest.mark.parametrize(
    ("provider", "model", "indexes"),
    [
        (None, None, [0, 1, 2, 3]),
        ("openai_main", None, [0, 1]),
        (None, "gpt-5", [0, 3]),
        ("openai_main", "gpt-5", [0]),
    ],
)
def test_llm_selection_matrix_preserves_objects_and_order(
    tmp_path: Path, provider: str | None, model: str | None, indexes: list[int]
) -> None:
    invocations = _invocations()
    orchestrator = _orchestrator(tmp_path, invocations)
    selected = orchestrator._select_llm_invocations(provider=provider, model=model)
    assert len(selected) == len(indexes)
    assert all(
        actual is invocations[index] for actual, index in zip(selected, indexes, strict=True)
    )
    assert orchestrator._llm_invocations is invocations
    if provider is None and model is None:
        assert selected is invocations


def test_duplicate_pair_retains_distinct_extra_body_and_identity(tmp_path: Path) -> None:
    first = LLMInvocation("openai_main", "gpt-5", {"temperature": 0.1})
    second = LLMInvocation("openai_main", "gpt-5", {"temperature": 0.9})
    invocations = (first, *_invocations()[1:], second, first)
    selected = _orchestrator(tmp_path, invocations)._select_llm_invocations(
        provider="openai_main", model="gpt-5"
    )
    assert len(selected) == 3
    assert selected[0] is first and selected[1] is second and selected[2] is first
    assert first.extra_body == {"temperature": 0.1}
    assert second.extra_body == {"temperature": 0.9}


MISMATCHES = [
    ("missing", None, "No LLM search invocation matches provider 'missing'"),
    (None, "missing-model", "No LLM search invocation matches model 'missing-model'"),
    (
        "missing",
        "missing-model",
        "No LLM search invocation matches provider 'missing' or model 'missing-model'",
    ),
    ("missing", "gpt-5", "No LLM search invocation matches provider 'missing'"),
    ("openai_main", "missing-model", "No LLM search invocation matches model 'missing-model'"),
    (
        "openai_main",
        "deepseek-v3",
        "No LLM search invocation matches provider 'openai_main' with model 'deepseek-v3'",
    ),
]


@pytest.mark.parametrize(("provider", "model", "message"), MISMATCHES)
def test_llm_mismatch_diagnostics(
    tmp_path: Path, provider: str | None, model: str | None, message: str
) -> None:
    with pytest.raises(InputFailure) as error:
        _orchestrator(tmp_path, _invocations())._select_llm_invocations(
            provider=provider, model=model
        )
    assert error.value.code is ErrorCode.BAD_REQUEST
    assert error.value.message == message
    assert not list((tmp_path / "results").glob("*"))


@pytest.mark.parametrize(
    ("provider", "model", "message"),
    [
        ("OPENAI_MAIN", None, "No LLM search invocation matches provider 'OPENAI_MAIN'"),
        (" openai_main ", None, "No LLM search invocation matches provider ' openai_main '"),
        (None, "GPT-5", "No LLM search invocation matches model 'GPT-5'"),
        (None, " gpt-5 ", "No LLM search invocation matches model ' gpt-5 '"),
        ("", None, "Provider must not be empty"),
        (" \t ", "gpt-5", "Provider must not be empty"),
        (None, "", "Model must not be empty"),
        ("openai_main", " \t ", "Model must not be empty"),
    ],
)
def test_llm_selector_exactness_and_blank_values(
    tmp_path: Path, provider: str | None, model: str | None, message: str
) -> None:
    with pytest.raises(InputFailure) as error:
        _orchestrator(tmp_path, _invocations())._select_llm_invocations(
            provider=provider, model=model
        )
    assert error.value.code is ErrorCode.BAD_REQUEST
    assert error.value.message == message


@pytest.mark.parametrize(
    ("provider", "model"), [(None, None), ("missing", "missing-model"), ("", " ")]
)
def test_llm_empty_runtime_precedes_selector_errors(
    tmp_path: Path, provider: str | None, model: str | None
) -> None:
    with pytest.raises(ExecutionFailure) as error:
        _orchestrator(tmp_path, ())._select_llm_invocations(provider=provider, model=model)
    assert error.value.code is ErrorCode.NO_LLM_SEARCH_PROVIDERS
    assert error.value.message == "No LLM search providers are configured"
