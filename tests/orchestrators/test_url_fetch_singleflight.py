import asyncio
import logging
from collections.abc import Mapping, Sequence

from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation
from agent_search_gateway.orchestrators.fetch import FetchOrchestrator
from agent_search_gateway.providers.contracts import ChatMessage, URLFetchCandidate
from agent_search_gateway.request_ids import bind_request_id, current_request_id
from agent_search_gateway.scheduler.fetch import FetchScheduler
from agent_search_gateway.url_normalization import NormalizedURL, normalize_url
from agent_search_gateway.url_store import URLStore
from tests.support.logging import structured_test_logger

_WAIT_TIMEOUT_SECONDS = 5.0


class _ControlledFetch:
    name = "fetch"

    def __init__(self) -> None:
        self.calls: list[NormalizedURL] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        self.calls.append(url)
        self.entered.set()
        await self.release.wait()
        return URLFetchCandidate(raw_content=f"raw:{url}", content=f"content:{url}")


class _SingleflightClient:
    name = "llm"

    def __init__(self) -> None:
        self.safety_calls = 0
        self.focus_calls: list[str] = []
        self.focus_entered: dict[str, asyncio.Event] = {}
        self.focus_release: dict[str, asyncio.Event] = {}
        self.active = 0
        self.max_active = 0

    def entered_event(self, focus: str) -> asyncio.Event:
        return self.focus_entered.setdefault(focus, asyncio.Event())

    def release_event(self, focus: str) -> asyncio.Event:
        return self.focus_release.setdefault(focus, asyncio.Event())

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]:
        if invocation.model == "judge-model":
            return {"ok": True}
        self.safety_calls += 1
        return {"ok": True}

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        if invocation.model == "clean-model":
            return "cleaned"
        prompt = messages[-1]["content"]
        focus_line = prompt.splitlines()[0]
        focus = focus_line.removeprefix("Focus: ")
        self.focus_calls.append(focus)
        entered = self.entered_event(focus)
        release = self.release_event(focus)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        entered.set()
        try:
            await release.wait()
        finally:
            self.active -= 1
        return f"summary:{focus}"

    async def aclose(self) -> None:
        return None


async def _await_focus(client: _SingleflightClient, focus: str) -> None:
    await asyncio.wait_for(client.entered_event(focus).wait(), timeout=_WAIT_TIMEOUT_SECONDS)


def _build(
    store: URLStore,
    client: _SingleflightClient,
    provider: _ControlledFetch,
    *,
    logger: logging.Logger | None = None,
) -> FetchOrchestrator:
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
        logger=logger,
    )
    scheduler = FetchScheduler(
        [provider],
        ProviderQuotaManager(web_limits={"fetch": 2}, llm_limits={}),
        stages,
        logger=logger,
    )
    return FetchOrchestrator(
        store=store,
        scheduler=scheduler,
        stages=stages,
        logger=logger,
    )


async def test_url_fetch_singleflight_shares_exact_request_and_serializes_different_focus() -> None:
    store = URLStore()
    url = normalize_url("https://example.com/page")
    store.admit(url, "known")
    client = _SingleflightClient()
    provider = _ControlledFetch()
    orchestrator = _build(store, client, provider)

    first = asyncio.create_task(orchestrator.url_fetch(str(url)))
    await provider.entered.wait()
    second = asyncio.create_task(orchestrator.url_fetch(str(url)))
    await asyncio.sleep(0)
    provider.release.set()
    assert tuple(await asyncio.gather(first, second)) == (f"content:{url}", f"content:{url}")
    assert provider.calls == [url]
    assert client.safety_calls == 1

    client.release_event("pricing")
    same_focus_first = asyncio.create_task(orchestrator.url_fetch(str(url), "pricing"))
    await _await_focus(client, "pricing")
    same_focus_second = asyncio.create_task(orchestrator.url_fetch(str(url), " pricing "))
    await asyncio.sleep(0)
    client.release_event("pricing").set()
    assert tuple(await asyncio.gather(same_focus_first, same_focus_second)) == (
        "summary:pricing",
        "summary:pricing",
    )
    assert client.focus_calls.count("pricing") == 1

    client.release_event("alpha")
    alpha = asyncio.create_task(orchestrator.url_fetch(str(url), "alpha"))
    await _await_focus(client, "alpha")
    client.release_event("beta")
    beta = asyncio.create_task(orchestrator.url_fetch(str(url), "beta"))
    await asyncio.sleep(0)
    assert "beta" not in client.focus_entered
    client.release_event("alpha").set()
    await _await_focus(client, "beta")
    client.release_event("beta").set()
    assert tuple(await asyncio.gather(alpha, beta)) == ("summary:alpha", "summary:beta")
    assert client.max_active == 1

    other = normalize_url("https://example.com/other")
    store.admit(other, "known", content="content-other")
    client.release_event("one")
    one = asyncio.create_task(orchestrator.url_fetch(str(url), "one"))
    await _await_focus(client, "one")
    client.release_event("two")
    two = asyncio.create_task(orchestrator.url_fetch(str(other), "two"))
    await _await_focus(client, "two")
    assert client.active == 2
    client.release_event("one").set()
    client.release_event("two").set()
    assert tuple(await asyncio.gather(one, two)) == ("summary:one", "summary:two")


async def test_url_fetch_singleflight_preserves_leader_provider_correlation() -> None:
    logger, stream = structured_test_logger("tests.fetch.singleflight-events")
    store = URLStore()
    url = normalize_url("https://example.com/correlated?id=42&mode=test")
    store.admit(url, "known")
    client = _SingleflightClient()
    provider = _ControlledFetch()
    orchestrator = _build(store, client, provider, logger=logger)

    async def invoke(request_id: str) -> str:
        with bind_request_id(request_id):
            return await orchestrator.url_fetch(str(url))

    leader = asyncio.create_task(invoke("11111111"))
    await provider.entered.wait()
    follower = asyncio.create_task(invoke("22222222"))
    await asyncio.sleep(0)
    provider.release.set()

    expected = f"content:{url}"
    assert tuple(await asyncio.gather(leader, follower)) == (expected, expected)
    assert provider.calls == [url]
    assert client.safety_calls == 1
    assert current_request_id() is None

    lines = stream.getvalue().splitlines()
    leader_lines = [line for line in lines if "request=11111111" in line]
    follower_lines = [line for line in lines if "request=22222222" in line]
    assert any("event=singleflight_leader" in line for line in leader_lines)
    assert any("event=singleflight_joined" in line for line in follower_lines)
    assert any(
        "event=provider_started" in line and "provider=fetch" in line for line in leader_lines
    )
    assert any(
        "event=provider_completed" in line and "provider=fetch" in line for line in leader_lines
    )
    assert any("event=url_lock_acquired" in line for line in leader_lines)
    assert all("event=provider_started" not in line for line in follower_lines)
    assert all("event=provider_completed" not in line for line in follower_lines)
    assert all("event=url_lock_acquired" not in line for line in follower_lines)
    assert "url=https://example.com/correlated " in stream.getvalue()
    assert "?id=42&mode=test" not in stream.getvalue()
