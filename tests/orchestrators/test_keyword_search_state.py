import asyncio
import json
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.llm import stages as stage_module
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import URLRecord, URLStatus
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.orchestrators import search as search_module
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.web.baiduqianfan import BaiduQianfanAdapter
from agent_search_gateway.request_ids import bind_request_id
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_normalization import NormalizedURL, normalize_url
from agent_search_gateway.url_store import URLStore
from tests.orchestrators.test_keyword_provider_filtering import _build as _build_keyword
from tests.orchestrators.test_keyword_search_pipeline import _orchestrator, _stages
from tests.orchestrators.test_url_fetch_flow import _build as _build_fetch
from tests.orchestrators.test_url_fetch_flow import _FlowClient
from tests.support.fakes import FakeKeywordSearchProvider, FakeURLFetchProvider
from tests.support.http import RecordingJsonExecutor
from tests.support.logging import structured_test_logger


async def test_baiduqianfan_results_use_existing_url_normalization_and_dedup(
    tmp_path: Path,
) -> None:
    web_urls = [
        "https://example.com/same",
        "  HTTPS://EXAMPLE.COM/case  ",
        "https://www.google.com/goto?url=https%3A%2F%2Fexample.com%2Fpaper%3Fx%3D1",
        "https://example.com/tracked?utm_source=one",
    ]
    agentic_urls = [
        "https://example.com/same",
        "https://example.com/case",
        "https://example.com/paper?x=1",
        "https://example.com/tracked?utm_source=two",
        "https://example.com/paper?x=2",
    ]
    http = RecordingJsonExecutor(
        [
            {"references": [{"url": url, "snippet": f"Web {i}"} for i, url in enumerate(web_urls)]},
            {
                "web_page_result": {
                    "web_pages": [
                        {"url": url, "snippet": f"Agentic {i}"}
                        for i, url in enumerate(agentic_urls)
                    ]
                }
            },
        ]
    )
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan", secret=SecretValue("sample"), http_executor=http
    )
    orchestrator, store, _, _ = _build_keyword(tmp_path, [adapter])

    path = Path(await orchestrator.keyword_search("query", request_id="13131313"))

    expected = [
        ("https://example.com/same", "Web 0"),
        ("https://example.com/case", "Web 1"),
        ("https://example.com/paper?x=1", "Web 2"),
        ("https://example.com/tracked?utm_source=one", "Web 3"),
        ("https://example.com/tracked?utm_source=two", "Agentic 3"),
        ("https://example.com/paper?x=2", "Agentic 4"),
    ]
    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"url": url, "abstract": abstract} for url, abstract in expected
    ]
    for url, abstract in expected:
        snapshot = store.get(normalize_url(url))
        assert snapshot is not None and snapshot.abstract == abstract
    assert store.get(normalize_url(web_urls[2])) is None


@pytest.mark.parametrize(
    ("raw_content", "status"),
    [("  \t ", URLStatus.NEED_FETCH), ("Original article.", URLStatus.NEED_JUDGE)],
)
async def test_baiduqianfan_admits_natural_body_and_abstract(
    tmp_path: Path, raw_content: str, status: URLStatus
) -> None:
    http = RecordingJsonExecutor(
        [
            {"references": []},
            {
                "web_page_result": {
                    "web_pages": [
                        {
                            "url": "https://example.com/rejected",
                            "snippet": "Retained abstract",
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
    orchestrator, store, client, _ = _build_keyword(tmp_path, [adapter])
    client.json_result = {"ok": False}

    path = Path(await orchestrator.keyword_search("query", request_id="14141414"))

    assert json.loads(path.read_text()) == {
        "url": "https://example.com/rejected",
        "abstract": "Retained abstract",
    }
    snapshot = store.get(normalize_url("https://example.com/rejected"))
    assert snapshot is not None and snapshot.status is status
    assert snapshot.abstract == "Retained abstract"
    assert (snapshot.raw_content, snapshot.content) == (raw_content.strip(), "")
    assert client.json_calls == client.text_calls == []


@pytest.mark.parametrize("with_title", [False, True], ids=["url-only", "title-only"])
async def test_baiduqianfan_media_preserves_abstract_without_body_processing(
    tmp_path: Path,
    with_title: bool,
) -> None:
    def media(source: str, kind: str) -> dict[str, object]:
        url = f"https://example.com/{source}-{kind}"
        record: dict[str, object] = (
            {"type": kind, kind: {"url": url}} if source == "web" else {"url": url}
        )
        if with_title:
            record["title"] = f"{source} {kind} title"
        return record

    http = RecordingJsonExecutor(
        [
            {
                "references": [
                    media("web", "image"),
                    media("web", "video"),
                ]
            },
            {
                "image_result": {"images": [media("agentic", "image")]},
                "video_result": {"videos": [media("agentic", "video")]},
            },
        ]
    )
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan", secret=SecretValue("sample"), http_executor=http
    )
    orchestrator, store, client, _ = _build_keyword(tmp_path, [adapter])

    path = Path(await orchestrator.keyword_search("query", request_id="15151515"))

    expected = [
        (f"{source}-{kind}", f"{source} {kind} title" if with_title else kind.title())
        for source in ("web", "agentic")
        for kind in ("image", "video")
    ]
    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"url": f"https://example.com/{suffix}", "abstract": label} for suffix, label in expected
    ]
    for suffix, label in expected:
        snapshot = store.get(normalize_url(f"https://example.com/{suffix}"))
        assert snapshot is not None and snapshot.abstract == label
        assert (snapshot.raw_content, snapshot.content) == ("", "")
    assert client.json_calls == []
    assert client.text_calls == []
    assert len(http.requests) == 2


@pytest.mark.parametrize(
    ("web_fails", "agentic_fails"), [(True, False), (False, True), (True, True)]
)
async def test_baiduqianfan_emits_one_logical_provider_terminal_event(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    web_fails: bool,
    agentic_fails: bool,
) -> None:
    caplog.set_level("DEBUG")
    failure = ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "Synthetic branch failure")
    http = RecordingJsonExecutor(
        [
            failure
            if web_fails
            else {"references": [{"url": "https://example.com/web", "snippet": "Web result"}]},
            failure
            if agentic_fails
            else {
                "web_page_result": {
                    "web_pages": [
                        {"url": "https://example.com/agentic", "snippet": "Agentic result"}
                    ]
                }
            },
        ]
    )
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan", secret=SecretValue("sample"), http_executor=http
    )
    orchestrator, _, _, _ = _build_keyword(tmp_path, [adapter])

    if web_fails and agentic_fails:
        with pytest.raises(ExecutionFailure) as error:
            await orchestrator.keyword_search("query", request_id="16161616")
        assert error.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    else:
        path = Path(await orchestrator.keyword_search("query", request_id="16161616"))
        branch = "agentic" if web_fails else "web"
        assert json.loads(path.read_text())["url"] == f"https://example.com/{branch}"

    events = [
        vars(record)["gateway_event"]
        for record in caplog.records
        if getattr(record, "gateway_fields", {}).get("provider") == "baiduqianfan"
    ]
    both_failed = web_fails and agentic_fails
    assert events.count("provider_completed") == (0 if both_failed else 1)
    assert events.count("provider_failed") == (1 if both_failed else 0)
    assert events.count("provider_partial_failure") == (0 if both_failed else 1)
    assert len(http.requests) == 2


@pytest.fixture
def no_body_stages(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cheap = Mock(return_value=False)
    monkeypatch.setattr(stage_module, "cheap_check", cheap)
    monkeypatch.setattr(search_module, "cheap_check", cheap, raising=False)
    stages = {name: AsyncMock() for name in ("judge", "content_clean", "safety", "focus_summary")}
    for name, stage in stages.items():
        monkeypatch.setattr(LLMStages, name, stage)
    yield
    cheap.assert_not_called()
    for stage in stages.values():
        stage.assert_not_called()


@pytest.mark.parametrize(
    ("raw", "content"),
    [(" r ", ""), ("", " c "), (" raw ", " clean "), (" \t ", "content")],
)
async def test_keyword_search_caches_natural_body_without_body_stages(
    tmp_path: Path, no_body_stages: None, raw: str, content: str
) -> None:
    url = normalize_url("https://example.org/hit")
    provider = FakeKeywordSearchProvider(
        "web", [KeywordSearchHit(url, title=" Title ", raw_content=raw, content=content)]
    )
    orchestrator, store, _, _ = _build_keyword(tmp_path, [provider])

    path = Path(await orchestrator.keyword_search("query", request_id="ac000001"))

    assert json.loads(path.read_text()) == {"url": url, "abstract": "Title"}
    record = store.get(url)
    assert record is not None and record.status is URLStatus.NEED_JUDGE
    assert (record.raw_content, record.content) == (raw if raw.strip() else "", content)


@pytest.mark.parametrize(("raw", "content"), [("", ""), ("  ", "\n")])
async def test_keyword_search_admits_metadata_without_a_candidate(
    tmp_path: Path, no_body_stages: None, raw: str, content: str
) -> None:
    url = normalize_url("https://example.org/metadata")
    provider = FakeKeywordSearchProvider(
        "web", [KeywordSearchHit(url, snippet="Summary", raw_content=raw, content=content)]
    )
    orchestrator, store, _, _ = _build_keyword(tmp_path, [provider])
    # Metadata admission must complete even when Fetch owns this URL's body lock.
    async with store.serialization(url), asyncio.timeout(1):
        path = Path(await orchestrator.keyword_search("query", request_id="ac000002"))

    assert json.loads(path.read_text()) == {"url": url, "abstract": "Summary"}
    record = store.get(url)
    assert record is not None and record.status is URLStatus.NEED_FETCH
    assert (record.raw_content, record.content) == ("", "")


@pytest.mark.parametrize(
    ("raw", "content", "cached_pair", "result"),
    [
        ("valid raw", "\ud800", ("valid raw", ""), "cleaned body"),
        ("\udfff", "valid content", ("", "valid content"), "valid content"),
        ("\ud800", "broken\udfff", ("", ""), "fetched content"),
    ],
    ids=["raw-only", "content-only", "both-invalid"],
)
async def test_keyword_search_with_invalid_utf8_body_can_be_fetched(
    tmp_path: Path, raw: str, content: str, cached_pair: tuple[str, str], result: str
) -> None:
    url = normalize_url("https://example.org/invalid-body")
    provider = FakeKeywordSearchProvider(
        "web", [KeywordSearchHit(url, title="Title", raw_content=raw, content=content)]
    )
    search, store, search_client, _ = _build_keyword(tmp_path, [provider])
    path = Path(await search.keyword_search("query", request_id="ac000003"))
    cached = store.get(url)

    fetch_provider = FakeURLFetchProvider(
        "fetch", URLFetchCandidate(raw_content="fetched raw", content="fetched content")
    )
    client = _FlowClient()
    fetch = _build_fetch(store, client, [fetch_provider])
    fetched = await fetch.url_fetch(str(url))

    assert json.loads(path.read_text()) == {"url": url, "abstract": "Title"}
    assert search_client.json_calls == search_client.text_calls == []
    assert cached is not None
    assert (cached.raw_content, cached.content) == cached_pair
    assert cached.status is (URLStatus.NEED_JUDGE if any(cached_pair) else URLStatus.NEED_FETCH)
    assert fetch_provider.calls == ([] if any(cached_pair) else [url])
    assert fetched == result
    assert [model for model, _ in client.calls] == (
        ["judge-model", "clean-model", "safety-model"]
        if cached_pair[0]
        else ["judge-model", "safety-model"]
    )
    final = store.get(url)
    assert final is not None and final.status is URLStatus.READY


class _OrderedProvider:
    def __init__(
        self,
        name: str,
        hits: list[KeywordSearchHit],
        *,
        wait_for: asyncio.Event | None = None,
        signal: asyncio.Event | None = None,
    ) -> None:
        self.name = name
        self.hits = hits
        self.wait_for = wait_for
        self.signal = signal
        self.calls = 0

    async def search(self, query: str) -> list[KeywordSearchHit]:
        self.calls += 1
        if self.signal is not None:
            self.signal.set()
        if self.wait_for is not None:
            await self.wait_for.wait()
        return list(self.hits)


async def test_keyword_admission_uses_declared_provider_order(tmp_path: Path) -> None:
    url = normalize_url("https://example.org/shared")
    release_first = asyncio.Event()
    first = _OrderedProvider(
        "first",
        [
            KeywordSearchHit(url, snippet="First abstract", raw_content="\ud800"),
            KeywordSearchHit(url, snippet="First body", raw_content="first raw"),
            KeywordSearchHit(url, snippet="Later hit", content="later clean"),
        ],
        wait_for=release_first,
    )
    second = _OrderedProvider(
        "second",
        [KeywordSearchHit(url, snippet="Second abstract", content="second clean")],
        signal=release_first,
    )
    orchestrator, store, _, _ = _build_keyword(tmp_path, [first, second])

    async with asyncio.timeout(1):
        path = Path(await orchestrator.keyword_search("query", request_id="11111111"))

    assert [first.calls, second.calls] == [1, 1]
    assert json.loads(path.read_text()) == {"url": url, "abstract": "First abstract"}
    record = store.get(url)
    assert record is not None and record.status is URLStatus.NEED_JUDGE
    assert (record.raw_content, record.content) == ("first raw", "")


class _ObservedAdmissions(URLStore):
    """Signal when Search enters and returns from the real candidate admission."""

    def __init__(self) -> None:
        super().__init__()
        self.started: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self.finished: set[str] = set()

    async def admit_candidate(
        self, url: NormalizedURL, abstract: str, *, raw_content: str = "", content: str = ""
    ) -> URLRecord:
        self.started[abstract].set()
        record = await super().admit_candidate(
            url, abstract, raw_content=raw_content, content=content
        )
        self.finished.add(abstract)
        return record


@pytest.mark.parametrize(
    ("paused_model", "replace_cached", "pending"),
    [
        ("judge-model", False, URLStatus.NEED_JUDGE),
        ("clean-model", False, URLStatus.NEED_CLEAN),
        ("safety-model", False, URLStatus.NEED_SAFETY),
        ("judge-model", True, URLStatus.NEED_FETCH),
    ],
)
async def test_search_body_waits_then_rereads_committed_fetch_state(
    tmp_path: Path, paused_model: str, replace_cached: bool, pending: URLStatus
) -> None:
    url = normalize_url("https://example.org/shared")
    store = _ObservedAdmissions()
    await store.admit_candidate(
        url,
        "Original",
        raw_content="cached raw",
        content="semantic-reject" if replace_cached else "",
    )
    search_provider = FakeKeywordSearchProvider(
        "web",
        [KeywordSearchHit(url, title="Later", raw_content="search raw", content="search clean")],
    )
    search = _orchestrator(tmp_path, [search_provider], store)
    client = _FlowClient()
    fetch_provider = FakeURLFetchProvider(
        "fetch", URLFetchCandidate(raw_content="fetch raw", content="fetch clean")
    )
    fetch = _build_fetch(store, client, [fetch_provider])
    entered, release = client.pause(paused_model)

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        fetching = tasks.create_task(fetch.url_fetch(str(url)))
        await entered.wait()
        if replace_cached:
            # Let Judge reject the cached pair, then pause Judge on the replacement.
            next_entered, next_release = client.pause(paused_model)
            release.set()
            await next_entered.wait()
            release = next_release
        before = store.get(url)
        assert before is not None and before.status is pending
        if replace_cached:
            assert (before.raw_content, before.content) == ("", "")
        searching = tasks.create_task(search.keyword_search("query", request_id="ac001801"))
        await store.started["Later"].wait()
        assert "Later" not in store.finished
        assert store.get(url) is before
        release.set()

    expected_raw, expected_body = (
        ("fetch raw", "fetch clean") if replace_cached else ("cached raw", "cleaned body")
    )
    assert fetching.result() == expected_body
    assert json.loads(Path(searching.result()).read_text()) == {"url": url, "abstract": "Original"}
    assert "Later" in store.finished
    final = store.get(url)
    assert final is not None
    assert (final.status, final.raw_content, final.content) == (
        URLStatus.READY,
        expected_raw,
        expected_body,
    )
    assert fetch_provider.calls == ([url] if replace_cached else [])
    assert [model for model, _ in client.calls] == (
        ["judge-model", "judge-model", "safety-model"]
        if replace_cached
        else ["judge-model", "clean-model", "safety-model"]
    )


async def test_two_searches_wait_for_same_url_while_other_url_completes(tmp_path: Path) -> None:
    url = normalize_url("https://example.org/contended")
    other = normalize_url("https://example.org/independent")
    store = _ObservedAdmissions()
    providers = [
        FakeKeywordSearchProvider("first", [KeywordSearchHit(url, title="A", raw_content="A raw")]),
        FakeKeywordSearchProvider("second", [KeywordSearchHit(url, title="B", content="B clean")]),
        FakeKeywordSearchProvider(
            "other", [KeywordSearchHit(other, title="C", raw_content="C raw")]
        ),
    ]
    search = _orchestrator(tmp_path, providers, store)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks, store.serialization(url):
        first = tasks.create_task(
            search.keyword_search("a", request_id="ac001802", provider="first")
        )
        await store.started["A"].wait()
        second = tasks.create_task(
            search.keyword_search("b", request_id="ac001803", provider="second")
        )
        await store.started["B"].wait()
        await search.keyword_search("c", request_id="ac001804", provider="other")
        independent = store.get(other)
        assert independent is not None
        assert (independent.status, independent.raw_content) == (URLStatus.NEED_JUDGE, "C raw")
        assert store.finished == {"C"}
        assert store.get(url) is None

    for result in (first.result(), second.result()):
        assert json.loads(Path(result).read_text()) == {"url": url, "abstract": "A"}
    record = store.get(url)
    assert record is not None
    assert (record.status, record.raw_content, record.content) == (
        URLStatus.NEED_JUDGE,
        "A raw",
        "",
    )


@pytest.mark.parametrize("status", [URLStatus.READY, URLStatus.UNAVAILABLE])
async def test_keyword_search_preserves_terminal_snapshot(
    tmp_path: Path, no_body_stages: None, status: URLStatus
) -> None:
    url = normalize_url("https://example.org/terminal")
    provider = FakeKeywordSearchProvider(
        "web", [KeywordSearchHit(url, snippet="New abstract", raw_content="new body")]
    )
    orchestrator, store, _, _ = _build_keyword(tmp_path, [provider])
    store.admit(url, "Stored abstract")
    async with store.serialization(url):
        store.accept_candidate(url, content="stored content")
        if status is URLStatus.READY:
            original = store.mark_ready(url)
        else:
            original = store.mark_unavailable(url)

    path = Path(await orchestrator.keyword_search("query", request_id="11111112"))

    assert json.loads(path.read_text()) == {"url": url, "abstract": "Stored abstract"}
    assert store.get(url) == original


async def test_keyword_search_debug_events_cover_provider_candidate_body_and_persistence(
    tmp_path: Path,
) -> None:
    logger, stream = structured_test_logger("tests.search.keyword-events")
    failed = FakeKeywordSearchProvider(
        "failed",
        failure=ExecutionFailure(
            ErrorCode.ALL_PROVIDERS_FAILED,
            "PROVIDER_FAILURE_DETAIL_SENTINEL",
        ),
    )
    success = FakeKeywordSearchProvider(
        "success",
        [
            KeywordSearchHit("https://EXAMPLE.COM/empty?id=1", title=" ", snippet=""),
            KeywordSearchHit(
                "https://log-user:LOG_PASSWORD_SENTINEL@EXAMPLE.COM/no-body?id=42&mode=test#frag",
                snippet="No body",
            ),
            KeywordSearchHit(
                "https://example.com/space?id=2",
                snippet="Whitespace body",
                raw_content="   ",
            ),
            KeywordSearchHit(
                "https://example.com/reject?id=3",
                snippet="Rejected body",
                raw_content="reject-body PAGE_REJECT_SENTINEL",
            ),
            KeywordSearchHit(
                "https://example.com/accepted?id=4",
                snippet="Accepted body",
                raw_content="PAGE_ACCEPT_SENTINEL",
            ),
            KeywordSearchHit(
                "https://log-user:LOG_PASSWORD_SENTINEL@EXAMPLE.COM/no-body?id=42&mode=test#frag",
                snippet="Duplicate later",
            ),
        ],
    )
    orchestrator = SearchOrchestrator(
        keyword_providers=[failed, success],
        llm_invocations=(),
        stages=_stages(),
        store=URLStore(),
        result_writer=ResultWriter(tmp_path / "debug-results"),
        logger=logger,
    )

    with bind_request_id("abcddcba"):
        result_path = Path(
            await orchestrator.keyword_search(
                "QUERY_BODY_SENTINEL",
                request_id="abcddcba",
            )
        )

    records = [json.loads(line) for line in result_path.read_text(encoding="utf-8").splitlines()]
    assert all(set(record) == {"url", "abstract"} for record in records)
    assert result_path.name == "keyword-abcddcba.jsonl"

    logged = stream.getvalue()
    lines = logged.splitlines()
    assert all("request=abcddcba" in line for line in lines)
    assert any(
        "event=provider_failed" in line
        and "provider=failed" in line
        and "error_type=ExecutionFailure" in line
        for line in lines
    )
    assert any(
        "event=provider_completed" in line and "provider=success" in line and "hits=6" in line
        for line in lines
    )
    assert any(
        "event=candidate_rejected" in line
        and "url=https://example.com/empty " in line
        and "reason=empty_abstract" in line
        for line in lines
    )
    assert "url=https://example.com/no-body " in logged
    assert "LOG_PASSWORD_SENTINEL" not in logged
    assert all("event=body_" not in line for line in lines)
    assert any(
        "event=candidate_rejected" in line
        and "url=https://example.com/no-body " in line
        and "reason=duplicate" in line
        for line in lines
    )
    assert any(
        "event=results_written" in line and "kind=keyword" in line and str(result_path) in line
        for line in lines
    )
    for sentinel in (
        "QUERY_BODY_SENTINEL",
        "PAGE_REJECT_SENTINEL",
        "PAGE_ACCEPT_SENTINEL",
        "PROVIDER_FAILURE_DETAIL_SENTINEL",
    ):
        assert sentinel not in logged


async def test_keyword_search_unavailable_body_stage_does_not_drop_hits(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="agent_search_gateway.orchestrators.search")
    url = normalize_url("https://example.org/body")
    valid = FakeKeywordSearchProvider(
        "valid",
        [
            KeywordSearchHit("https://example.org/meta", snippet="Metadata"),
            KeywordSearchHit(url, snippet="Body hit", raw_content="natural text"),
        ],
    )
    invalid = FakeKeywordSearchProvider("invalid", [KeywordSearchHit(cast(str, 1), title="Bad")])
    orchestrator, store, client, _ = _build_keyword(tmp_path, [valid, invalid])
    client.failure = ExecutionFailure(ErrorCode.LLM_STAGE_FAILED, "stage unavailable")

    path = Path(await orchestrator.keyword_search("query", request_id="aa000001"))

    assert [json.loads(line)["abstract"] for line in path.read_text().splitlines()] == [
        "Metadata",
        "Body hit",
    ]
    assert client.json_calls == client.text_calls == []
    record = store.get(url)
    assert record is not None and record.status is URLStatus.NEED_JUDGE
    terminals = [
        (
            vars(event)["gateway_event"],
            vars(event)["gateway_fields"]["provider"],
            vars(event)["gateway_fields"]["stage"],
        )
        for event in caplog.records
        if getattr(event, "gateway_event", None)
        in {"provider_failed", "provider_completed", "provider_partial_failure"}
    ]
    assert sorted(terminals) == [
        ("provider_completed", "valid", "search"),
        ("provider_failed", "invalid", "hit_staging"),
    ]


async def test_keyword_cancellation_during_provider_search_skips_admission(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="agent_search_gateway.orchestrators.search")
    entered = asyncio.Event()
    url = normalize_url("https://example.org/pending")
    waiting = _OrderedProvider(
        "waiting",
        [KeywordSearchHit(url, title="Pending")],
        wait_for=asyncio.Event(),
        signal=entered,
    )
    orchestrator, store, _, _ = _build_keyword(tmp_path, [waiting])
    task = asyncio.create_task(orchestrator.keyword_search("query", request_id="aa000002"))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get(url) is None
    assert not list((tmp_path / "results").glob("*.jsonl"))
    terminals = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) in {"provider_failed", "provider_completed"}
    ]
    assert len(terminals) == 1
    assert terminals[0]["stage"] == "search"
    assert terminals[0]["reason"] == "cancelled"
