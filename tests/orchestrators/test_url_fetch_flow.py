import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest

from agent_search_gateway.concurrency import CapacityGate
from agent_search_gateway.errors import UNAVAILABLE_MESSAGE, ErrorCode, ExecutionFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation, URLStatus
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.orchestrators import fetch as fetch_module
from agent_search_gateway.orchestrators.fetch import FetchOrchestrator
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.providers.contracts import ChatMessage, URLFetchCandidate
from agent_search_gateway.providers.web.baiduqianfan import BaiduQianfanAdapter
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.scheduler.fetch import FetchScheduler
from agent_search_gateway.url_normalization import normalize_url
from agent_search_gateway.url_store import URLStore
from tests.support.fakes import FakeURLFetchProvider, pooled_web
from tests.support.http import RecordingJsonExecutor


class _FlowClient:
    name = "llm"

    def __init__(self) -> None:
        self.safety_ok = True
        self.fail_model = ""
        self.clean_output = "cleaned body"
        self.focus_calls = 0
        self.calls: list[tuple[str, str]] = []
        self._pauses: dict[str, tuple[asyncio.Event, asyncio.Event]] = {}

    def pause(self, model: str) -> tuple[asyncio.Event, asyncio.Event]:
        """Return events for observing and releasing a call to this model."""
        events = asyncio.Event(), asyncio.Event()
        self._pauses[model] = events
        return events

    async def _record_call(self, model: str, text: str) -> None:
        self.calls.append((model, text))
        if model in self._pauses:
            entered, release = self._pauses[model]
            entered.set()
            await release.wait()

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]:
        text = messages[-1]["content"]
        await self._record_call(invocation.model, text)
        if invocation.model == self.fail_model:
            raise ExecutionFailure(ErrorCode.LLM_STAGE_FAILED, "decision transport")
        if invocation.model == "judge-model":
            if "semantic-reject" in text:
                return {"ok": False, "reason": "semantic"}
            return {"ok": True}
        return {"ok": self.safety_ok, "reason": "unsafe" if not self.safety_ok else ""}

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        await self._record_call(invocation.model, messages[-1]["content"])
        if invocation.model == self.fail_model:
            raise ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "text transport")
        if invocation.model == "clean-model":
            return self.clean_output
        if invocation.model == "focus-model":
            self.focus_calls += 1
            return f"summary-{self.focus_calls}"
        return "unused"

    async def aclose(self) -> None:
        return None


def _flow_stages(client: _FlowClient) -> LLMStages:
    judge = LLMInvocation("llm", "judge-model", {})
    safety = LLMInvocation("llm", "safety-model", {})
    clean = LLMInvocation("llm", "clean-model", {})
    focus = LLMInvocation("llm", "focus-model", {})
    return LLMStages(
        {"llm": client},
        judge=judge,
        safety=safety,
        content_clean=clean,
        focus_summary=focus,
    )


def _build(
    store: URLStore,
    client: _FlowClient,
    providers: list[FakeURLFetchProvider],
) -> FetchOrchestrator:
    stages = _flow_stages(client)
    scheduler = FetchScheduler(
        [pooled_web(p, CapacityGate(1)) for p in providers],
        stages,
    )
    return FetchOrchestrator(store=store, scheduler=scheduler, stages=stages)


@pytest.mark.parametrize("safety_ok", [True, False])
async def test_baiduqianfan_agentic_raw_content_uses_existing_clean_and_safety_pipeline(
    tmp_path: Path,
    safety_ok: bool,
) -> None:
    raw_content = "Original page text with useful factual information."
    http = RecordingJsonExecutor(
        [
            {"references": []},
            {
                "web_page_result": {
                    "web_pages": [
                        {
                            "url": "https://example.com/agentic-body",
                            "title": "Original page",
                            "snippet": "Admitted abstract",
                            "content": raw_content,
                        }
                    ]
                }
            },
        ]
    )
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan", secret=SecretValue("sample"), http_executor=http
    )
    store = URLStore()
    client = _FlowClient()
    client.safety_ok = safety_ok
    search = SearchOrchestrator(
        keyword_providers=[adapter],
        llm_invocations=(),
        stages=_flow_stages(client),
        store=store,
        result_writer=ResultWriter(tmp_path / "results"),
    )

    await search.keyword_search("query", request_id="17171717")

    url = normalize_url("https://example.com/agentic-body")
    snapshot = store.get(url)
    assert snapshot is not None and snapshot.abstract == "Admitted abstract"
    assert (snapshot.raw_content, snapshot.content) == (raw_content, "")
    assert snapshot.status is URLStatus.NEED_JUDGE
    assert client.calls == []

    webpage_fetch = FakeURLFetchProvider(
        "webpage-fetch",
        failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "Unexpected webpage fetch"),
    )
    fetch = _build(store, client, [webpage_fetch])

    result = await fetch.url_fetch(str(url))

    assert result == ("cleaned body" if safety_ok else UNAVAILABLE_MESSAGE)
    assert [model for model, _ in client.calls] == ["judge-model", "clean-model", "safety-model"]
    assert raw_content in client.calls[1][1]
    assert "cleaned body" in client.calls[2][1]
    assert webpage_fetch.calls == []
    assert len(http.requests) == 2
    refreshed = store.get(url)
    assert refreshed is not None
    assert refreshed.raw_content == raw_content
    assert refreshed.content == "cleaned body"
    assert refreshed.status is (URLStatus.READY if safety_ok else URLStatus.UNAVAILABLE)
    if not safety_ok:
        assert raw_content not in result and "cleaned body" not in result
        assert await fetch.url_fetch(str(url)) == UNAVAILABLE_MESSAGE
        assert len(client.calls) == 3


@pytest.mark.parametrize(
    ("status", "raw", "content", "models"),
    [
        (URLStatus.NEED_FETCH, "provider raw", "", ["judge-model", "clean-model", "safety-model"]),
        (URLStatus.NEED_FETCH, "provider raw", "provider clean", ["judge-model", "safety-model"]),
        (URLStatus.NEED_JUDGE, "cached raw", "", ["judge-model", "clean-model", "safety-model"]),
        (URLStatus.NEED_JUDGE, "", "cached clean", ["judge-model", "safety-model"]),
        (URLStatus.NEED_JUDGE, "cached raw", "cached clean", ["judge-model", "safety-model"]),
        (URLStatus.NEED_CLEAN, "accepted raw", "", ["clean-model", "safety-model"]),
        (URLStatus.NEED_SAFETY, "accepted raw", "clean", ["safety-model"]),
        (URLStatus.READY, "accepted raw", "clean", []),
        (URLStatus.UNAVAILABLE, "accepted raw", "unsafe", []),
    ],
)
async def test_fetch_resumes_from_url_status(
    status: URLStatus, raw: str, content: str, models: list[str]
) -> None:
    store = URLStore()
    url = normalize_url("https://example.com/state")
    if status is URLStatus.NEED_JUDGE:
        snapshot = await store.admit_candidate(url, "known", raw_content=raw, content=content)
    else:
        snapshot = store.admit(url, "known")
        async with store.serialization(url):
            if status is not URLStatus.NEED_FETCH:
                snapshot = store.accept_candidate(url, raw_content=raw, content=content)
            if status is URLStatus.READY:
                snapshot = store.mark_ready(url)
            elif status is URLStatus.UNAVAILABLE:
                snapshot = store.mark_unavailable(url)
    assert snapshot.status is status

    client = _FlowClient()
    provider = FakeURLFetchProvider("fetch", URLFetchCandidate(raw_content=raw, content=content))
    fetch = _build(store, client, [provider])
    result = await fetch.url_fetch(str(url))
    assert [model for model, _ in client.calls] == models
    assert provider.calls == ([url] if status is URLStatus.NEED_FETCH else [])

    final = store.get(url)
    assert final is not None
    if status is URLStatus.UNAVAILABLE:
        assert result == UNAVAILABLE_MESSAGE
        assert final is snapshot
    else:
        assert result == (content or "cleaned body")
        assert (final.status, final.raw_content, final.content) == (URLStatus.READY, raw, result)
        for model, prompt in client.calls:
            if model == "judge-model":
                assert prompt.endswith(content or raw)
            elif model == "clean-model":
                assert prompt.endswith(raw)
            else:
                assert prompt.endswith(result)


@pytest.mark.parametrize("rejection", ["cheap", "judge"])
async def test_cached_rejection_replaces_whole_pair_in_same_fetch(
    monkeypatch: pytest.MonkeyPatch, rejection: str
) -> None:
    store = URLStore()
    url = normalize_url("https://example.com/replacement")
    await store.admit_candidate(url, "known", raw_content="old raw", content="semantic-reject")
    if rejection == "cheap":
        monkeypatch.setattr(fetch_module, "cheap_check", lambda candidate: False)
    client = _FlowClient()
    provider = FakeURLFetchProvider("replacement", URLFetchCandidate(raw_content="fresh raw"))

    assert await _build(store, client, [provider]).url_fetch(str(url)) == "cleaned body"

    models = [model for model, _ in client.calls]
    expected = ["judge-model"] if rejection == "judge" else []
    assert models == [*expected, "judge-model", "clean-model", "safety-model"]
    assert client.calls[-3][1].endswith("fresh raw")
    assert client.calls[-2][1].endswith("fresh raw")
    assert provider.calls == [url]
    snapshot = store.get(url)
    assert snapshot is not None
    assert (snapshot.status, snapshot.raw_content, snapshot.content) == (
        URLStatus.READY,
        "fresh raw",
        "cleaned body",
    )


@pytest.mark.parametrize("with_provider", [False, True])
async def test_cached_rejection_then_fetch_execution_failure_is_recoverable(
    with_provider: bool,
) -> None:
    store = URLStore()
    url = normalize_url("https://example.com/recover")
    await store.admit_candidate(url, "known", raw_content="old raw", content="semantic-reject")
    provider = FakeURLFetchProvider(
        "offline", failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "provider execution")
    )
    client = _FlowClient()
    fetch = _build(store, client, [provider] if with_provider else [])

    with pytest.raises(ExecutionFailure) as failure:
        await fetch.url_fetch(str(url))
    assert failure.value.code is (
        ErrorCode.ALL_PROVIDERS_FAILED if with_provider else ErrorCode.NO_URL_FETCH_PROVIDERS
    )
    snapshot = store.get(url)
    assert snapshot is not None
    assert (snapshot.status, snapshot.raw_content, snapshot.content) == (
        URLStatus.NEED_FETCH,
        "",
        "",
    )
    assert provider.calls == ([url] if with_provider else [])
    assert [model for model, _ in client.calls] == ["judge-model"]
    assert client.calls[0][1].endswith("semantic-reject")

    await store.admit_candidate(url, "new abstract", content="new content")
    assert await fetch.url_fetch(str(url)) == "new content"
    assert [model for model, _ in client.calls[1:]] == ["judge-model", "safety-model"]


@pytest.mark.parametrize("source", ["fetch", "safety"])
async def test_explicit_rejection_stays_unavailable(source: str) -> None:
    store = URLStore()
    url = normalize_url("https://example.com/rejected")
    store.admit(url, "known")
    client = _FlowClient()
    client.safety_ok = False
    raw = "semantic-reject" if source == "fetch" else "accepted raw"
    provider = FakeURLFetchProvider("fetch", URLFetchCandidate(raw_content=raw))
    fetch = _build(store, client, [provider])

    assert await fetch.url_fetch(str(url), "focus") == UNAVAILABLE_MESSAGE
    snapshot = store.get(url)
    assert snapshot is not None and snapshot.status is URLStatus.UNAVAILABLE
    assert await store.admit_candidate(url, "again", content="other") is snapshot
    assert await fetch.url_fetch(str(url), "focus") == UNAVAILABLE_MESSAGE
    assert provider.calls == [url]
    assert [model for model, _ in client.calls] == (
        ["judge-model"] if source == "fetch" else ["judge-model", "clean-model", "safety-model"]
    )


@pytest.mark.parametrize(
    ("failed_model", "pending", "content", "retry"),
    [
        ("judge-model", URLStatus.NEED_JUDGE, "cached content", ["judge-model", "safety-model"]),
        ("clean-model", URLStatus.NEED_CLEAN, "", ["clean-model", "safety-model"]),
        ("blank-clean", URLStatus.NEED_CLEAN, "", ["clean-model", "safety-model"]),
        ("invalid-clean", URLStatus.NEED_CLEAN, "", ["clean-model", "safety-model"]),
        ("safety-model", URLStatus.NEED_SAFETY, "cleaned body", ["safety-model"]),
    ],
)
async def test_url_status_stage_failure_resumes_at_first_unfinished_stage(
    failed_model: str, pending: URLStatus, content: str, retry: list[str]
) -> None:
    store = URLStore()
    url = normalize_url("https://example.com/stage-failure")
    client = _FlowClient()
    client.fail_model = failed_model
    if failed_model == "blank-clean":
        client.clean_output = "   "
    elif failed_model == "invalid-clean":
        client.clean_output = cast(str, b"unexpected bytes")
    provider = FakeURLFetchProvider("fetch", URLFetchCandidate(raw_content="initial raw"))
    if pending is URLStatus.NEED_JUDGE:
        await store.admit_candidate(url, "known", raw_content="initial raw", content=content)
    else:
        store.admit(url, "known")
    fetch = _build(store, client, [provider])

    with pytest.raises(ExecutionFailure) as failure:
        await fetch.url_fetch(str(url))
    assert failure.value.code is ErrorCode.LLM_STAGE_FAILED
    snapshot = store.get(url)
    assert snapshot is not None
    assert (snapshot.status, snapshot.raw_content, snapshot.content) == (
        pending,
        "initial raw",
        content,
    )
    stages = ["judge-model", "clean-model", "safety-model"]
    assert [model for model, _ in client.calls] == stages[: stages.index(retry[0]) + 1]

    client.calls.clear()
    client.fail_model = ""
    client.clean_output = "cleaned body"
    assert await fetch.url_fetch(str(url)) == (content or "cleaned body")
    assert [model for model, _ in client.calls] == retry
    assert provider.calls == ([] if pending is URLStatus.NEED_JUDGE else [url])


@pytest.mark.parametrize(
    ("interrupted", "after_result", "pending", "content", "retry_from"),
    [
        ("judge-model", False, URLStatus.NEED_JUDGE, "", "judge-model"),
        ("clean-model", False, URLStatus.NEED_CLEAN, "", "clean-model"),
        ("safety-model", False, URLStatus.NEED_SAFETY, "cleaned body", "safety-model"),
        ("focus-model", False, URLStatus.READY, "cleaned body", "focus-model"),
        ("judge-model", True, URLStatus.NEED_CLEAN, "", "clean-model"),
        ("clean-model", True, URLStatus.NEED_SAFETY, "cleaned body", "safety-model"),
        ("safety-model", True, URLStatus.READY, "cleaned body", "focus-model"),
    ],
)
async def test_stage_cancellation_preserves_last_committed_status(
    interrupted: str, after_result: bool, pending: URLStatus, content: str, retry_from: str
) -> None:
    store = URLStore()
    url = normalize_url("https://example.com/cancel-stage")
    await store.admit_candidate(url, "known", raw_content="original raw")
    client = _FlowClient()
    entered, release = client.pause(interrupted)
    fetch = _build(store, client, [])
    stages = ["judge-model", "clean-model", "safety-model", "focus-model"]

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        request = tasks.create_task(fetch.url_fetch(str(url), "pricing"))
        await entered.wait()
        if after_result:
            # Wake the completed stage first. Cancellation runs at its next suspension.
            _, next_release = client.pause(retry_from)
            release.set()
            asyncio.get_running_loop().call_soon(request.cancel)
        else:
            next_release = release
            request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

    record = store.get(url)
    assert record is not None
    assert (record.status, record.raw_content, record.content) == (pending, "original raw", content)
    attempted = [model for model, _ in client.calls]
    through_interruption = stages[: stages.index(interrupted) + 1]
    if after_result:
        # The next stage may have started when cancellation arrives, but cannot have committed.
        assert attempted in (through_interruption, [*through_interruption, retry_from])
    else:
        assert attempted == through_interruption

    client.calls.clear()
    next_release.set()
    async with asyncio.timeout(5):
        assert await fetch.url_fetch(str(url), "pricing") == "summary-1"
    assert [model for model, _ in client.calls] == stages[stages.index(retry_from) :]
    final = store.get(url)
    assert final is not None
    assert (final.status, final.raw_content, final.content) == (
        URLStatus.READY,
        "original raw",
        "cleaned body",
    )


async def test_ready_focus_failure_does_not_fallback_to_full_content() -> None:
    store = URLStore()
    url = normalize_url("https://example.com/focus")
    store.admit(url, "known")
    async with store.serialization(url):
        store.accept_candidate(url, content="full body")
        ready = store.mark_ready(url)
    client = _FlowClient()
    provider = FakeURLFetchProvider("fetch")
    fetch = _build(store, client, [provider])
    client.fail_model = "focus-model"

    with pytest.raises(ExecutionFailure) as failure:
        await fetch.url_fetch(str(url), "pricing")
    assert failure.value.code is ErrorCode.LLM_STAGE_FAILED
    assert store.get(url) is ready

    client.fail_model = ""
    assert await fetch.url_fetch(str(url), "   ") == "full body"
    assert await fetch.url_fetch(str(url), "pricing") == "summary-1"
    assert await fetch.url_fetch(str(url), "pricing") == "summary-2"
    assert [model for model, _ in client.calls] == ["focus-model"] * 3
    assert all(prompt.endswith("full body") for _, prompt in client.calls)
    assert provider.calls == []
    assert store.get(url) is ready
