"""Business provider interfaces backed by logical credential pools."""

from collections.abc import Callable, Mapping, Sequence

from ..credential_pool import CredentialPool, CredentialReservation
from ..input_limits import PreparedInput
from ..models import LLMInvocation, OAResolution
from ..rate_limit import SelectionBudget
from ..url_normalization import NormalizedURL
from .contracts import (
    AcademicSearchProvider,
    ChatMessage,
    KeywordSearchHit,
    KeywordSearchProvider,
    OAResolver,
    PaperSearchHit,
    PreparedLLMClient,
    URLFetchCandidate,
    URLFetchProvider,
    WebAdapter,
)


class PooledWebProvider:
    def __init__(self, pool: CredentialPool[WebAdapter]) -> None:
        self.name = pool.name
        self._pool = pool

    async def search(self, query: str) -> list[KeywordSearchHit]:
        async def search_member(adapter: WebAdapter) -> list[KeywordSearchHit]:
            if not isinstance(adapter, KeywordSearchProvider):
                raise TypeError("Web adapter does not support search")
            return await adapter.search(query)

        return await self._pool.invoke(search_member)

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        return await self._pool.invoke(lambda adapter: self._fetch(adapter, url))

    async def try_reserve_fetch(self) -> CredentialReservation[WebAdapter] | None:
        return await self._pool.try_reserve(set())

    async def wait_fetch_available(self, budget: SelectionBudget) -> None:
        await self._pool.wait_available(set(), budget)

    async def fetch_reserved(
        self, url: NormalizedURL, first: CredentialReservation[WebAdapter], budget: SelectionBudget
    ) -> URLFetchCandidate:
        return await self._pool.invoke(
            lambda adapter: self._fetch(adapter, url), first=first, budget=budget
        )

    @staticmethod
    async def _fetch(adapter: WebAdapter, url: NormalizedURL) -> URLFetchCandidate:
        if not isinstance(adapter, URLFetchProvider):
            raise TypeError("Web adapter does not support fetch")
        return await adapter.fetch(url)


class PooledAcademicProvider:
    def __init__(self, pool: CredentialPool[AcademicSearchProvider]) -> None:
        self.name = pool.name
        self._pool = pool

    async def search(self, query: str) -> list[PaperSearchHit]:
        return await self._pool.invoke(lambda adapter: adapter.search(query))


class PooledOAResolver:
    def __init__(self, pool: CredentialPool[OAResolver]) -> None:
        self.name = pool.name
        self._pool = pool

    async def resolve(self, doi: str) -> OAResolution | None:
        return await self._pool.invoke(lambda adapter: adapter.resolve(doi))


class PooledLLMClient:
    def __init__(
        self,
        pool: CredentialPool[PreparedLLMClient],
        prepare_request: Callable[[LLMInvocation, Sequence[ChatMessage]], PreparedInput],
    ) -> None:
        self.name = pool.name
        self._pool = pool
        self._prepare_request = prepare_request

    async def complete_text(
        self, invocation: LLMInvocation, messages: Sequence[ChatMessage]
    ) -> str:
        prepared = self._prepare_request(invocation, messages)
        return await self._pool.invoke(
            lambda client: client.complete_text_prepared(invocation, messages, prepared)
        )

    async def complete_json(
        self, invocation: LLMInvocation, messages: Sequence[ChatMessage]
    ) -> Mapping[str, object]:
        prepared = self._prepare_request(invocation, messages)
        return await self._pool.invoke(
            lambda client: client.complete_json_prepared(invocation, messages, prepared)
        )
