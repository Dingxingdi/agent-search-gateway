"""Contract-realistic provider and LLM fakes."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence

from agent_search_gateway.concurrency import CapacityGate
from agent_search_gateway.credential_pool import CredentialMember, CredentialPool
from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.models import LLMInvocation, OAResolution
from agent_search_gateway.providers.contracts import (
    ChatMessage,
    KeywordSearchHit,
    PaperSearchHit,
    URLFetchCandidate,
    WebAdapter,
)
from agent_search_gateway.providers.pooled import PooledWebProvider
from agent_search_gateway.rate_limit import RateGate
from agent_search_gateway.url_normalization import NormalizedURL


class FakeKeywordSearchProvider:
    def __init__(
        self,
        name: str,
        result: Sequence[KeywordSearchHit] = (),
        failure: ExecutionFailure | None = None,
    ) -> None:
        self.name = name
        self.result = list(result)
        self.failure = failure
        self.calls: list[str] = []

    async def search(self, query: str) -> list[KeywordSearchHit]:
        self.calls.append(query)
        if self.failure is not None:
            raise self.failure
        return list(self.result)


class FakeAcademicSearchProvider:
    def __init__(
        self,
        name: str,
        result: Sequence[PaperSearchHit] = (),
        failure: ExecutionFailure | None = None,
    ) -> None:
        self.name = name
        self.result = list(result)
        self.failure = failure
        self.calls: list[str] = []

    async def search(self, query: str) -> list[PaperSearchHit]:
        self.calls.append(query)
        if self.failure is not None:
            raise self.failure
        return list(self.result)


class FakeOAResolver:
    name = "unpaywall"

    def __init__(
        self,
        result: OAResolution | None = None,
        failure: ExecutionFailure | None = None,
    ) -> None:
        self.result = result
        self.failure = failure
        self.calls: list[str] = []

    async def resolve(self, doi: str) -> OAResolution | None:
        self.calls.append(doi)
        if self.failure is not None:
            raise self.failure
        return self.result


class FakeURLFetchProvider:
    def __init__(
        self,
        name: str,
        result: URLFetchCandidate | None = None,
        failure: ExecutionFailure | None = None,
    ) -> None:
        self.name = name
        self.result = result or URLFetchCandidate(raw_content="fake raw")
        self.failure = failure
        self.calls: list[NormalizedURL] = []

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        self.calls.append(url)
        if self.failure is not None:
            raise self.failure
        return self.result


class FakeLLMClient:
    def __init__(
        self,
        name: str,
        *,
        text_result: str = "fake text",
        json_result: Mapping[str, object] | None = None,
        failure: ExecutionFailure | None = None,
    ) -> None:
        self.name = name
        self.text_result = text_result
        self.json_result = dict(json_result or {"ok": True})
        self.failure = failure
        self.text_calls: list[tuple[LLMInvocation, tuple[ChatMessage, ...]]] = []
        self.json_calls: list[tuple[LLMInvocation, tuple[ChatMessage, ...]]] = []
        self.close_calls = 0

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        self.text_calls.append((invocation, tuple(messages)))
        if self.failure is not None:
            raise self.failure
        return self.text_result

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]:
        self.json_calls.append((invocation, tuple(messages)))
        if self.failure is not None:
            raise self.failure
        return dict(self.json_result)

    async def aclose(self) -> None:
        self.close_calls += 1


def pooled_web(
    adapter: WebAdapter,
    capacity: CapacityGate | None = None,
    rate: RateGate | None = None,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> PooledWebProvider:
    return PooledWebProvider(
        CredentialPool(
            "web",
            adapter.name,
            [CredentialMember(adapter, capacity, rate)],
            monotonic=monotonic,
            sleep=sleep,
        )
    )
