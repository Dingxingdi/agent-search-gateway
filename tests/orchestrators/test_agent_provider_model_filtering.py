import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, InputFailure
from agent_search_gateway.models import AgentInvocation, SearchScope
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.providers.contracts import AgentTextRunner, ChatMessage
from tests.support.agent_search import agent_orchestrator, paper_markdown, web_markdown

INVOCATIONS = (
    AgentInvocation("a"),
    AgentInvocation("a", "m1"),
    AgentInvocation("b", "m2"),
    AgentInvocation("c", "m1"),
    AgentInvocation("a"),
)


def configured(
    directory: Path,
    invocations: tuple[AgentInvocation, ...] = INVOCATIONS,
) -> tuple[SearchOrchestrator, list[tuple[str, str | None]]]:
    calls: list[tuple[str, str | None]] = []

    def for_alias(alias: str) -> AgentTextRunner:
        async def runner(model: str | None, messages: Sequence[ChatMessage]) -> str:
            calls.append((alias, model))
            if "## Paper" in messages[0]["content"]:
                return paper_markdown(f"https://{alias}.test/{model or 'default'}", doi="")
            return web_markdown(f"https://{alias}.test/{model or 'default'}", alias)

        return runner

    runners = {name: for_alias(name) for name in ("a", "b", "c")}
    return agent_orchestrator(directory, invocations, runners), calls


@pytest.mark.parametrize("scope", ["web", "paper", "all"])
@pytest.mark.parametrize(
    ("provider", "model", "indexes"),
    [
        (None, None, [0, 1, 2, 3, 4]),
        ("a", None, [0, 1, 4]),
        (None, "m1", [1, 3]),
        ("a", "m1", [1]),
    ],
)
async def test_agent_provider_filter_executes_only_matching_invocations(
    tmp_path: Path,
    scope: SearchScope,
    provider: str | None,
    model: str | None,
    indexes: list[int],
) -> None:
    search, calls = configured(tmp_path)
    path = Path(
        await search.agent_search(
            "query",
            request_id="11111111",
            scope=scope,
            provider=provider,
            model=model,
        )
    )
    expected = [(INVOCATIONS[index].provider, INVOCATIONS[index].model) for index in indexes]
    assert calls == expected * (2 if scope == "all" else 1)
    assert path.name == "agent-11111111.jsonl"
    calls.clear()
    await search.agent_search("query", request_id="22222222", scope=scope)
    assert calls == [(inv.provider, inv.model) for inv in INVOCATIONS] * (
        2 if scope == "all" else 1
    )


@pytest.mark.parametrize("scope", ["web", "paper", "all"])
@pytest.mark.parametrize(
    ("provider", "model", "message"),
    [
        ("missing", None, "No Agent search invocation matches provider 'missing'"),
        (None, "missing", "No Agent search invocation matches model 'missing'"),
        (
            "missing",
            "absent",
            "No Agent search invocation matches provider 'missing' or model 'absent'",
        ),
        ("missing", "m1", "No Agent search invocation matches provider 'missing'"),
        ("a", "absent", "No Agent search invocation matches model 'absent'"),
        ("a", "m2", "No Agent search invocation matches provider 'a' with model 'm2'"),
        ("A", None, "No Agent search invocation matches provider 'A'"),
        (" a ", None, "No Agent search invocation matches provider ' a '"),
        (None, "M1", "No Agent search invocation matches model 'M1'"),
        ("", None, "Provider must not be empty"),
        (None, " ", "Model must not be empty"),
        ("llm-only", None, "No Agent search invocation matches provider 'llm-only'"),
    ],
)
async def test_agent_selectors_fail_before_any_generation(
    tmp_path: Path,
    scope: SearchScope,
    provider: str | None,
    model: str | None,
    message: str,
) -> None:
    search, calls = configured(tmp_path)
    with pytest.raises(InputFailure) as caught:
        await search.agent_search(
            "query",
            request_id="11111111",
            scope=scope,
            provider=provider,
            model=model,
        )
    assert caught.value.code is ErrorCode.BAD_REQUEST
    assert caught.value.message == message
    assert not calls and not list(tmp_path.glob("*.jsonl"))


async def test_agent_model_filter_excludes_unpinned_invocations(tmp_path: Path) -> None:
    search, calls = configured(tmp_path, (AgentInvocation("a"),))
    with pytest.raises(InputFailure) as caught:
        await search.agent_search("query", request_id="11111111", model="saved-harness-model")
    assert caught.value.message == "No Agent search invocation matches model 'saved-harness-model'"
    assert not calls


async def test_agent_direct_input_validation_order_and_empty_config(tmp_path: Path) -> None:
    search, calls = configured(tmp_path, ())
    with pytest.raises(ValueError, match="request ID"):
        await search.agent_search("", request_id="bad")
    with pytest.raises(InputFailure) as prompt_error:
        await search.agent_search(" ", request_id="11111111", scope="bad")  # type: ignore[arg-type]
    assert prompt_error.value.code is ErrorCode.EMPTY_QUERY
    with pytest.raises(InputFailure, match="scope"):
        await search.agent_search("q", request_id="11111111", scope="bad")  # type: ignore[arg-type]
    with pytest.raises(ExecutionFailure) as missing:
        await search.agent_search("q", request_id="11111111", provider="", model=" ")
    assert missing.value.code is ErrorCode.NO_AGENT_SEARCH_PROVIDERS
    assert missing.value.message == "No Agent search providers are configured"
    assert not calls and not list(tmp_path.glob("*.jsonl"))


async def test_concurrent_agent_selectors_do_not_change_configuration(tmp_path: Path) -> None:
    search, calls = configured(tmp_path)
    await asyncio.gather(
        search.agent_search("q", request_id="11111111", provider="a"),
        search.agent_search("q", request_id="22222222", model="m2"),
    )
    assert sorted((alias, model or "") for alias, model in calls) == [
        ("a", ""),
        ("a", ""),
        ("a", "m1"),
        ("b", "m2"),
    ]
    selected = search._select_agent_invocations(provider=None, model=None)
    assert selected is INVOCATIONS
    assert search._select_agent_invocations(provider="a", model=None) == (
        INVOCATIONS[0],
        INVOCATIONS[1],
        INVOCATIONS[4],
    )
