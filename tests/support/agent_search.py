"""Small Agent fixtures with real prompt, parser, store and writer boundaries."""

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_search_gateway.academic.aggregator import PaperAggregator
from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import AgentInvocation, LLMInvocation
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.providers.contracts import AgentTextRunner, ChatMessage, OAResolver
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_store import URLStore


def web_markdown(url: str = "https://example.test/web", abstract: str = "Agent result") -> str:
    return f"## Result\nURL: {url}\nAbstract: {abstract}\n"


def paper_markdown(
    url: str = "https://example.test/paper",
    *,
    doi: str = "10.1000/shared",
    title: str = "Shared Paper",
    pdf: str = "",
) -> str:
    return (
        f"## Paper\nTitle: {title}\nAuthors: Alice Example\nAbstract: Paper abstract\n"
        f"DOI: {doi}\narXiv:\nPublished: 2024-01-02\nUpdated:\n"
        f"URL: {url}\nPDF: {pdf}\nVenue: ExampleConf\nTopics: AI\nCitations: 7\n"
        "Open Access: unknown\nOA Status:\nLicense:"
    )


class RecordingRunner:
    def __init__(self, text: str | None = None, *, failure: Exception | None = None) -> None:
        self.text = web_markdown() if text is None else text
        self.failure = failure
        self.calls: list[tuple[str | None, Sequence[ChatMessage]]] = []

    async def __call__(self, model: str | None, messages: Sequence[ChatMessage]) -> str:
        self.calls.append((model, tuple(messages)))
        if self.failure is not None:
            raise self.failure
        return self.text


def agent_orchestrator(
    directory: Path,
    invocations: Sequence[AgentInvocation],
    runners: Mapping[str, AgentTextRunner],
    *,
    store: URLStore | None = None,
    aggregator: PaperAggregator | None = None,
    resolver: OAResolver | None = None,
    logger: logging.Logger | None = None,
) -> SearchOrchestrator:
    fallback = LLMInvocation("unused-fetch", "fetch-model")
    stages = LLMStages(
        {},
        judge=fallback,
        safety=fallback,
        content_clean=fallback,
        focus_summary=fallback,
        agent_runners=runners,
        logger=logger,
    )
    return SearchOrchestrator(
        keyword_providers=(),
        llm_invocations=(),
        agent_invocations=invocations,
        quotas=ProviderQuotaManager(web_limits={}, llm_limits={}),
        stages=stages,
        store=store if store is not None else URLStore(),
        result_writer=ResultWriter(directory),
        paper_aggregator=aggregator,
        paper_resolver=resolver,
        logger=logger,
    )
