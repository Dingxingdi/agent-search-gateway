import asyncio
from pathlib import Path

from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.models import (
    KeywordSearchRequest,
    LLMSearchRequest,
    ShutdownRequest,
    SuccessResponse,
    URLFetchRequest,
)
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.protocol import send_request
from agent_search_gateway.request_ids import current_request_id


class _RecordingSearch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def keyword_search(
        self, query: str, *, request_id: str, provider: str | None = None
    ) -> str:
        self.calls.append(("keyword", request_id, current_request_id()))
        return f"keyword-{request_id}.jsonl"

    async def llm_search(
        self,
        prompt: str,
        *,
        request_id: str,
        scope: str = "web",
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        self.calls.append(("llm", request_id, current_request_id()))
        return f"llm-{request_id}.jsonl"


class _RecordingPaper:
    async def paper_search(
        self, query: str, *, request_id: str, provider: str | None = None
    ) -> str:
        return f"paper-{request_id}.jsonl"


class _RecordingFetch:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def url_fetch(self, url: str, focus: str | None = None) -> str:
        self.calls.append(current_request_id())
        return url


class _Runtime:
    def __init__(self) -> None:
        self.search_orchestrator = _RecordingSearch()
        self.paper_search_orchestrator = _RecordingPaper()
        self.fetch_orchestrator = _RecordingFetch()

    async def aclose(self) -> None:
        return None


async def test_daemon_allocates_one_id_per_business_request_and_binds_context(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    runtime = _Runtime()
    ids = iter(["11111111", "22222222", "33333333"])
    daemon = ForegroundDaemon(
        paths,
        runtime_factory=lambda: runtime,
        request_id_factory=ids.__next__,
    )
    daemon_task = asyncio.create_task(daemon.start())
    await daemon.ready.wait()

    assert await send_request(paths.socket_file, KeywordSearchRequest("one")) == SuccessResponse(
        "keyword-11111111.jsonl"
    )
    assert await send_request(paths.socket_file, LLMSearchRequest("two")) == SuccessResponse(
        "llm-22222222.jsonl"
    )
    assert await send_request(
        paths.socket_file,
        URLFetchRequest("https://example.com", None),
    ) == SuccessResponse("https://example.com")

    assert runtime.search_orchestrator.calls == [
        ("keyword", "11111111", "11111111"),
        ("llm", "22222222", "22222222"),
    ]
    assert runtime.fetch_orchestrator.calls == ["33333333"]
    assert current_request_id() is None

    await daemon.stop_for_test()
    await daemon_task


async def test_shutdown_does_not_consume_request_id(tmp_path: Path) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    runtime = _Runtime()
    ids = iter(["aaaaaaaa"])
    daemon = ForegroundDaemon(
        paths,
        runtime_factory=lambda: runtime,
        request_id_factory=ids.__next__,
    )
    daemon_task = asyncio.create_task(daemon.start())
    await daemon.ready.wait()

    assert await send_request(paths.socket_file, ShutdownRequest()) == SuccessResponse(
        "Daemon stopped."
    )
    await daemon_task

    assert next(ids) == "aaaaaaaa"
