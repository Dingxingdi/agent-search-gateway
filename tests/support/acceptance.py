"""No-network runtime assembly used by acceptance tests."""

from collections.abc import Mapping, Sequence

from agent_search_gateway.academic.aggregator import PaperAggregator
from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation, OAResolution
from agent_search_gateway.orchestrators.fetch import FetchOrchestrator
from agent_search_gateway.orchestrators.paper import PaperSearchOrchestrator
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.contracts import (
    ChatMessage,
    KeywordSearchHit,
    PaperSearchHit,
    URLFetchCandidate,
)
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.scheduler.fetch import FetchScheduler
from agent_search_gateway.url_normalization import normalize_url
from agent_search_gateway.url_store import URLStore
from tests.support.fakes import (
    FakeAcademicSearchProvider,
    FakeKeywordSearchProvider,
    FakeLLMClient,
    FakeOAResolver,
    FakeURLFetchProvider,
)


class _AcceptanceLLMClient:
    name = "llm"

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]:
        return {"ok": True}

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        if invocation.model == "search-model":
            if "## Paper" in messages[0]["content"]:
                return (
                    "## Paper\n"
                    "Title: LLM Academic Paper\n"
                    "Authors: L. Author\n"
                    "Abstract: LLM paper abstract\n"
                    "DOI: \n"
                    "arXiv: \n"
                    "Published: 2024-01-03\n"
                    "Updated: \n"
                    "URL: https://example.com/llm-paper\n"
                    "PDF: \n"
                    "Venue: Example Workshop\n"
                    "Topics: AI\n"
                    "Citations: 3\n"
                    "Open Access: unknown\n"
                    "OA Status: \n"
                    "License: "
                )
            return "## Result\nURL: https://example.com/llm\nAbstract: LLM abstract\n"
        if invocation.model == "focus-model":
            prompt = messages[-1]["content"]
            focus = prompt.splitlines()[0].removeprefix("Focus: ")
            return f"Focused summary: {focus}"
        if invocation.model == "clean-model":
            return "Cleaned content"
        return "unused"

    async def aclose(self) -> None:
        return None


class AcceptanceRuntime:
    def __init__(self, paths: RuntimePaths) -> None:
        store = URLStore()
        keyword_provider = FakeKeywordSearchProvider(
            "keyword",
            [
                KeywordSearchHit(
                    url="https://example.com/article",
                    title="Article",
                    snippet="Keyword abstract",
                )
            ],
        )
        self.fetch_provider = FakeURLFetchProvider(
            "fetch",
            URLFetchCandidate(
                raw_content="Raw article body",
                content="Full article content",
            ),
        )
        shared_doi = "10.1000/acceptance-shared"
        openalex = FakeAcademicSearchProvider(
            "openalex",
            [
                PaperSearchHit(
                    source="openalex",
                    source_id="W111",
                    title="Direct Academic Paper",
                    authors=("A. Author",),
                    abstract="Direct academic abstract",
                    doi=shared_doi,
                    url="https://example.com/paper",
                )
            ],
        )
        failed_crossref = FakeAcademicSearchProvider(
            "crossref",
            failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "acceptance failure"),
        )
        core = FakeAcademicSearchProvider(
            "core",
            [
                PaperSearchHit(
                    source="core",
                    source_id="core-shared",
                    title="Duplicate metadata",
                    authors=("A. Author",),
                    doi=shared_doi,
                    url="https://example.com/core-copy",
                ),
                PaperSearchHit(
                    source="core",
                    source_id="core-unique",
                    title="Unique CORE Paper",
                    authors=("C. Author",),
                    abstract="Unique repository abstract",
                    url="https://example.com/core-unique",
                ),
            ],
        )
        self.academic_providers = (openalex, failed_crossref, core)
        self.oa_resolver = FakeOAResolver(
            OAResolution(
                landing_url=normalize_url("https://repository.example/paper"),
                pdf_url=normalize_url("https://repository.example/paper.pdf"),
                is_open_access=True,
                oa_status="green",
                license="cc-by",
            )
        )
        quotas = ProviderQuotaManager(
            web_limits={"keyword": 2, "fetch": 2},
            llm_limits={},
            academic_limits={"openalex": 2, "crossref": 2, "core": 2},
        )
        client = _AcceptanceLLMClient()
        judge = LLMInvocation("llm", "judge-model", {})
        safety = LLMInvocation("llm", "safety-model", {})
        clean = LLMInvocation("llm", "clean-model", {})
        focus = LLMInvocation("llm", "focus-model", {})
        search = LLMInvocation("llm", "search-model", {})
        stages = LLMStages(
            {"llm": client},
            judge=judge,
            safety=safety,
            content_clean=clean,
            focus_summary=focus,
        )
        result_writer = ResultWriter(paths.results_dir)
        paper_aggregator = PaperAggregator(("openalex", "crossref", "core", "llm:llm"))
        self.search_orchestrator = SearchOrchestrator(
            keyword_providers=[keyword_provider],
            llm_invocations=[search],
            quotas=quotas,
            stages=stages,
            store=store,
            result_writer=result_writer,
            paper_aggregator=paper_aggregator,
            paper_resolver=self.oa_resolver,
        )
        self.paper_search_orchestrator = PaperSearchOrchestrator(
            providers=self.academic_providers,
            quotas=quotas,
            aggregator=paper_aggregator,
            resolver=self.oa_resolver,
            store=store,
            result_writer=result_writer,
        )
        self.fetch_orchestrator = FetchOrchestrator(
            store=store,
            scheduler=FetchScheduler([self.fetch_provider], quotas, stages),
            stages=stages,
        )
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


def build_acceptance_runtime(paths: RuntimePaths) -> AcceptanceRuntime:
    return AcceptanceRuntime(paths)


DEBUG_QUERY_SENTINEL = "DEBUG_QUERY_SENTINEL"
DEBUG_PAGE_ACCEPT_SENTINEL = "DEBUG_PAGE_ACCEPT_SENTINEL"
DEBUG_PAGE_REJECT_SENTINEL = "DEBUG_PAGE_REJECT_SENTINEL"
DEBUG_MODEL_RESPONSE_SENTINEL = "DEBUG_MODEL_RESPONSE_SENTINEL"
DEBUG_CREDENTIAL_SENTINEL = "DEBUG_CREDENTIAL_SENTINEL"


class _DebugAcceptanceLLMClient:
    name = "judge"

    async def complete_json(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> Mapping[str, object]:
        rendered = "\n".join(value for message in messages for value in message.values())
        if DEBUG_PAGE_REJECT_SENTINEL in rendered:
            return {"ok": False, "reason": "judge rejected test body"}
        return {"ok": True, "reason": "accepted"}

    async def complete_text(
        self,
        invocation: LLMInvocation,
        messages: Sequence[ChatMessage],
    ) -> str:
        return DEBUG_MODEL_RESPONSE_SENTINEL

    async def aclose(self) -> None:
        return None


class DebugAcceptanceRuntime:
    def __init__(self, paths: RuntimePaths) -> None:
        store = URLStore()
        self.keyword_provider = FakeKeywordSearchProvider(
            "keyword",
            [
                KeywordSearchHit(
                    url="https://example.com/accepted?id=42&mode=test",
                    title="Accepted",
                    snippet="Accepted abstract",
                    raw_content=DEBUG_PAGE_ACCEPT_SENTINEL,
                ),
                KeywordSearchHit(
                    url="https://example.com/rejected?id=43&mode=test",
                    title="Rejected body",
                    snippet="Rejected abstract",
                    raw_content=DEBUG_PAGE_REJECT_SENTINEL,
                ),
            ],
        )
        quotas = ProviderQuotaManager(web_limits={"keyword": 2}, llm_limits={})
        invocation = LLMInvocation("judge", "judge-model", {})
        stages = LLMStages(
            {"judge": _DebugAcceptanceLLMClient()},
            judge=invocation,
            safety=invocation,
            content_clean=invocation,
            focus_summary=invocation,
        )
        result_writer = ResultWriter(paths.results_dir)
        self.search_orchestrator = SearchOrchestrator(
            keyword_providers=[self.keyword_provider],
            llm_invocations=(),
            quotas=quotas,
            stages=stages,
            store=store,
            result_writer=result_writer,
        )
        self.paper_search_orchestrator = PaperSearchOrchestrator(
            providers=(),
            quotas=quotas,
            aggregator=PaperAggregator(()),
            resolver=None,
            store=store,
            result_writer=result_writer,
        )
        self.fetch_orchestrator = FetchOrchestrator(
            store=store,
            scheduler=FetchScheduler([], quotas, stages),
            stages=stages,
        )
        self.close_calls = 0
        self.credential_sentinel = DEBUG_CREDENTIAL_SENTINEL

    async def aclose(self) -> None:
        self.close_calls += 1


def build_debug_acceptance_runtime(paths: RuntimePaths) -> DebugAcceptanceRuntime:
    return DebugAcceptanceRuntime(paths)


class FilteringAcceptanceRuntime:
    """A separate multi-provider runtime for request selector acceptance tests."""

    def __init__(self, paths: RuntimePaths) -> None:
        self.store = URLStore()
        self.keyword_providers = tuple(
            FakeKeywordSearchProvider(
                name,
                [
                    KeywordSearchHit(
                        url=f"https://example.com/keyword/{name}",
                        snippet=name,
                    )
                ],
            )
            for name in ("alpha", "beta")
        )
        self.academic_providers = tuple(
            FakeAcademicSearchProvider(
                name,
                [
                    PaperSearchHit(
                        source=name,
                        source_id=source_id,
                        title=f"Paper from {name}",
                        abstract=f"About {name}",
                        doi=f"10.1000/{name}",
                        url=f"https://example.com/paper/{name}",
                    )
                ],
            )
            for name, source_id in (("arxiv", "2401.00001"), ("openalex", "W1"))
        )
        self.llm_invocations = (
            LLMInvocation("openai_main", "gpt-5"),
            LLMInvocation("openai_main", "gpt-5-mini"),
            LLMInvocation("deepseek_main", "deepseek-v3"),
            LLMInvocation("azure_main", "gpt-5"),
        )
        self.llm_clients = {
            name: FakeLLMClient(
                name,
                text_result=f"## Result\nURL: https://example.com/llm/{name}\nAbstract: {name}",
            )
            for name in ("openai_main", "deepseek_main", "azure_main", "fetch_only")
        }
        fallback = LLMInvocation("fetch_only", "global-model")
        stages = LLMStages(
            self.llm_clients,
            judge=fallback,
            safety=fallback,
            content_clean=fallback,
            focus_summary=fallback,
        )
        self.fetch_provider = FakeURLFetchProvider(
            "fetch", URLFetchCandidate(raw_content="Raw fetched content", content="Fetched content")
        )
        self.quotas = ProviderQuotaManager(
            web_limits={"alpha": 2, "beta": 2, "fetch": 2},
            llm_limits={name: 2 for name in self.llm_clients},
            academic_limits={name: 2 for name in ("arxiv", "openalex")},
        )
        self.oa_resolver = FakeOAResolver()
        result_writer = ResultWriter(paths.results_dir)
        aggregator = PaperAggregator(
            ("arxiv", "openalex", "llm:openai_main", "llm:deepseek_main", "llm:azure_main")
        )
        self.search_orchestrator = SearchOrchestrator(
            keyword_providers=self.keyword_providers,
            llm_invocations=self.llm_invocations,
            quotas=self.quotas,
            stages=stages,
            store=self.store,
            result_writer=result_writer,
            paper_aggregator=aggregator,
            paper_resolver=self.oa_resolver,
        )
        self.paper_search_orchestrator = PaperSearchOrchestrator(
            providers=self.academic_providers,
            quotas=self.quotas,
            aggregator=aggregator,
            resolver=self.oa_resolver,
            store=self.store,
            result_writer=result_writer,
        )
        self.fetch_orchestrator = FetchOrchestrator(
            store=self.store,
            scheduler=FetchScheduler([self.fetch_provider], self.quotas, stages),
            stages=stages,
        )
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        for client in self.llm_clients.values():
            await client.aclose()


def build_filtering_acceptance_runtime(paths: RuntimePaths) -> FilteringAcceptanceRuntime:
    return FilteringAcceptanceRuntime(paths)
