import asyncio
from collections.abc import Mapping, Sequence

import pytest

from agent_search_gateway.errors import (
    UNAVAILABLE_MESSAGE,
    ErrorCode,
    ExecutionFailure,
    InputFailure,
)
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation, URLStatus
from agent_search_gateway.orchestrators.fetch import FetchOrchestrator
from agent_search_gateway.providers.contracts import ChatMessage
from agent_search_gateway.scheduler.fetch import FetchScheduler
from agent_search_gateway.url_normalization import normalize_url
from agent_search_gateway.url_store import URLStore


class _CachedPathClient:
    name = "llm"

    def __init__(self) -> None:
        self.json_calls: list[tuple[LLMInvocation, tuple[ChatMessage, ...]]] = []
        self.text_calls: list[tuple[LLMInvocation, tuple[ChatMessage, ...]]] = []

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]:
        self.json_calls.append((invocation, tuple(messages)))
        return {"ok": True}

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        self.text_calls.append((invocation, tuple(messages)))
        if invocation.model == "clean-model":
            return "cleaned from cached raw"
        return "summary"

    async def aclose(self) -> None:
        return None


def _build(store: URLStore, client: _CachedPathClient) -> FetchOrchestrator:
    judge = LLMInvocation("llm", "judge-model", {})
    safety = LLMInvocation("llm", "safety-model", {})
    clean = LLMInvocation("llm", "clean-model", {})
    focus = LLMInvocation("llm", "focus-model", {})
    stages = LLMStages(
        {"llm": client},
        judge=judge,
        safety=safety,
        content_clean=clean,
        focus_summary=focus,
    )
    scheduler = FetchScheduler(
        [],
        stages,
    )
    return FetchOrchestrator(store=store, scheduler=scheduler, stages=stages)


async def test_url_fetch_enforces_admission_and_uses_cached_fields() -> None:
    store = URLStore()
    client = _CachedPathClient()
    orchestrator = _build(store, client)

    with pytest.raises(InputFailure) as invalid:
        await orchestrator.url_fetch("not-a-url")
    assert invalid.value.code is ErrorCode.INVALID_URL

    with pytest.raises(InputFailure) as not_admitted:
        await orchestrator.url_fetch("https://example.com/missing")
    assert not_admitted.value.code is ErrorCode.URL_NOT_ADMITTED

    unavailable_url = normalize_url("https://example.com/unavailable")
    store.admit(unavailable_url, "known")
    async with store.serialization(unavailable_url):
        store.mark_unavailable(unavailable_url)
    assert await orchestrator.url_fetch(str(unavailable_url)) == UNAVAILABLE_MESSAGE
    assert client.json_calls == []
    assert client.text_calls == []

    content_url = normalize_url("https://example.com/content")
    await store.admit_candidate(content_url, "known", raw_content="raw", content="cached content")
    assert await orchestrator.url_fetch(str(content_url)) == "cached content"
    assert [call[0].model for call in client.json_calls] == ["judge-model", "safety-model"]
    assert client.text_calls == []

    raw_url = normalize_url("https://example.com/raw")
    await store.admit_candidate(raw_url, "known", raw_content="cached raw")
    assert await orchestrator.url_fetch(str(raw_url)) == "cleaned from cached raw"
    raw_snapshot = store.get(raw_url)
    assert raw_snapshot is not None
    assert raw_snapshot.raw_content == "cached raw"
    assert raw_snapshot.content == "cleaned from cached raw"
    assert [call[0].model for call in client.text_calls] == ["clean-model"]
    assert [call[0].model for call in client.json_calls] == ["judge-model", "safety-model"] * 2

    no_body_url = normalize_url("https://example.com/no-body")
    store.admit(no_body_url, "known")
    with pytest.raises(ExecutionFailure) as no_provider:
        await orchestrator.url_fetch(str(no_body_url))
    assert no_provider.value.code is ErrorCode.NO_URL_FETCH_PROVIDERS

    snapshot = store.get(no_body_url)
    assert snapshot is not None and snapshot.status is URLStatus.NEED_FETCH


async def test_url_status_admission_is_checked_inside_store_serialization() -> None:
    store = URLStore()
    url = normalize_url("https://example.com/awaiting-admission")
    client = _CachedPathClient()
    orchestrator = _build(store, client)

    async with store.serialization(url):
        # Even a missing URL must wait for concurrent admission before deciding.
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0):
                await orchestrator.url_fetch(str(url))
        store.admit(url, "now admitted")

    with pytest.raises(ExecutionFailure) as no_provider:
        await orchestrator.url_fetch(str(url))
    assert no_provider.value.code is ErrorCode.NO_URL_FETCH_PROVIDERS
    assert client.json_calls == client.text_calls == []
