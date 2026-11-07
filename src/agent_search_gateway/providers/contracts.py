"""Provider-facing contracts and candidate value objects."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from ..models import LLMInvocation, OAResolution
from ..url_normalization import NormalizedURL

ChatMessage = Mapping[str, str]
AgentTextRunner = Callable[[str | None, Sequence[ChatMessage]], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class KeywordSearchHit:
    url: str
    title: str = ""
    snippet: str = ""
    raw_content: str = ""
    content: str = ""


@dataclass(frozen=True, slots=True)
class URLFetchCandidate:
    raw_content: str
    content: str = ""


@dataclass(frozen=True, slots=True)
class PaperSearchHit:
    source: str
    source_id: str
    title: str
    authors: tuple[str, ...] = ()
    abstract: str = ""
    doi: str = ""
    arxiv_id: str = ""
    published_date: date | None = None
    updated_date: date | None = None
    url: str = ""
    pdf_url: str = ""
    venue: str = ""
    topics: tuple[str, ...] = ()
    citation_count: int | None = None
    is_open_access: bool | None = None
    oa_status: str = ""
    license: str = ""


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    search: bool
    fetch: bool


@runtime_checkable
class KeywordSearchProvider(Protocol):
    name: str

    async def search(self, query: str) -> list[KeywordSearchHit]: ...


@runtime_checkable
class URLFetchProvider(Protocol):
    name: str

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate: ...


@runtime_checkable
class AcademicSearchProvider(Protocol):
    name: str

    async def search(self, query: str) -> list[PaperSearchHit]: ...


@runtime_checkable
class OAResolver(Protocol):
    name: str

    async def resolve(self, doi: str) -> OAResolution | None: ...


@runtime_checkable
class LLMClient(Protocol):
    name: str

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str: ...

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]: ...

    async def aclose(self) -> None: ...
