from pathlib import Path
from typing import cast

import pytest

from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.errors import ErrorCode, ExecutionFailure, InputFailure
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.providers.contracts import KeywordSearchHit
from agent_search_gateway.providers.http import ProviderResponseFailure
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_store import URLStore
from tests.support.fakes import FakeKeywordSearchProvider, FakeLLMClient


def _stages() -> LLMStages:
    invocation = LLMInvocation("llm", "model", {})
    client = FakeLLMClient("llm", json_result={"ok": True})
    return LLMStages(
        {"llm": client},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
    )


def _orchestrator(
    tmp_path: Path,
    providers: list[FakeKeywordSearchProvider],
    store: URLStore | None = None,
) -> SearchOrchestrator:
    return SearchOrchestrator(
        keyword_providers=providers,
        llm_invocations=(),
        quotas=ProviderQuotaManager(
            web_limits={provider.name: 1 for provider in providers},
            llm_limits={},
        ),
        stages=_stages(),
        store=store or URLStore(),
        result_writer=ResultWriter(tmp_path / "results"),
    )


async def test_keyword_hit_uses_google_goto_destination(tmp_path: Path) -> None:
    provider = FakeKeywordSearchProvider(
        "web",
        [
            KeywordSearchHit(
                "https://www.google.com/goto?url=https%3A%2F%2Fexample.org%2Fpaper%3Fx%3D1",
                snippet="Paper",
            )
        ],
    )
    path = Path(
        await _orchestrator(tmp_path, [provider]).keyword_search("query", request_id="00000007")
    )
    assert path.read_text(encoding="utf-8") == (
        '{"url":"https://example.org/paper?x=1","abstract":"Paper"}\n'
    )


async def test_opaque_google_goto_hit_keeps_wrapper_identity(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="agent_search_gateway.orchestrators.search")
    wrapper = "https://google.com/goto?url=CAESAA_bb-12"
    provider = FakeKeywordSearchProvider("web", [KeywordSearchHit(wrapper, snippet="Paper")])
    path = Path(
        await _orchestrator(tmp_path, [provider]).keyword_search("query", request_id="00000021")
    )
    assert path.read_text(encoding="utf-8") == (
        '{"url":"https://google.com/goto?url=CAESAA_bb-12","abstract":"Paper"}\n'
    )
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["accepted"] == 1
    assert summaries[0]["rejected"] == 0
    assert summaries[0]["rewritten"] == 0
    assert "CAESAA_bb-12" not in repr([vars(record) for record in caplog.records])


@pytest.mark.parametrize("snippet", ["Paper", ""])
async def test_invalid_google_goto_is_rejected_before_abstract(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, snippet: str
) -> None:
    caplog.set_level("DEBUG", logger="agent_search_gateway.orchestrators.search")
    provider = FakeKeywordSearchProvider(
        "web",
        [KeywordSearchHit("https://google.com/goto?url=opaque-test-value", snippet=snippet)],
    )
    path = Path(
        await _orchestrator(tmp_path, [provider]).keyword_search("query", request_id="00000008")
    )
    assert path.read_text(encoding="utf-8") == ""
    rejections = [
        record
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "candidate_rejected"
    ]
    assert len(rejections) == 1
    assert getattr(rejections[0], "gateway_fields", None) == {
        "provider": "web",
        "reason": "recognized_invalid_redirect",
    }
    assert "opaque-test-value" not in repr([vars(record) for record in caplog.records])


async def test_keyword_staging_reports_redirect_rewrites_and_rejections(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="agent_search_gateway.orchestrators.search")
    provider = FakeKeywordSearchProvider(
        "web",
        [
            KeywordSearchHit(
                "https://google.com/goto?url=https%3A%2F%2Fexample.org%2Fa", snippet="A"
            ),
            KeywordSearchHit(
                "https://www.google.com/goto?url=https%3A%2F%2Fexample.org%2Fb", snippet="B"
            ),
            KeywordSearchHit("https://google.com/goto?url=invalid", snippet="Invalid"),
            KeywordSearchHit("https://example.org/direct", snippet="Direct"),
        ],
    )
    path = Path(
        await _orchestrator(tmp_path, [provider]).keyword_search("query", request_id="00000009")
    )
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert summaries == [
        {
            "provider": "web",
            "phase": "staging",
            "received": 4,
            "accepted": 3,
            "rejected": 1,
            "rewritten": 2,
            "rejection_counts": {"recognized_invalid_redirect": 1},
            "processed": 4,
            "unprocessed": 0,
        }
    ]
    assert not any(
        getattr(record, "gateway_event", None) == "provider_failed" for record in caplog.records
    )


async def test_keyword_staging_counts_rewrite_before_empty_abstract(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="agent_search_gateway.orchestrators.search")
    provider = FakeKeywordSearchProvider(
        "web",
        [
            KeywordSearchHit("https://google.com/goto?url=https%3A%2F%2Fexample.org%2Fempty"),
            KeywordSearchHit("https://example.org/direct", snippet="Direct"),
        ],
    )
    await _orchestrator(tmp_path, [provider]).keyword_search("query", request_id="00000010")
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["rejection_counts"] == {"empty_abstract": 1}
    assert summaries[0]["rewritten"] == 1


@pytest.mark.parametrize("header_failure", [False, True], ids=["execution", "provider-header"])
async def test_keyword_search_uses_pipeline_completion_rules(
    tmp_path: Path, header_failure: bool
) -> None:
    with pytest.raises(InputFailure) as empty:
        await _orchestrator(tmp_path, []).keyword_search("   ", request_id="00000001")
    assert empty.value.code is ErrorCode.EMPTY_QUERY

    with pytest.raises(ExecutionFailure) as absent:
        await _orchestrator(tmp_path, []).keyword_search("query", request_id="00000002")
    assert absent.value.code is ErrorCode.NO_KEYWORD_SEARCH_PROVIDERS

    failure = (
        ProviderResponseFailure(
            "brightdata",
            "search",
            status_code=200,
            retryable=True,
            safe_reason="provider_reported_failure",
            diagnostics={"brightdata_error_code": "captcha", "brightdata_status": 502},
        )
        if header_failure
        else ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "provider failed")
    )
    failed = FakeKeywordSearchProvider("failed", failure=failure)
    success = FakeKeywordSearchProvider(
        "success",
        [KeywordSearchHit("https://example.com/a", snippet="A")],
    )
    orchestrator = _orchestrator(tmp_path, [failed, success])
    path = Path(await orchestrator.keyword_search(" query ", request_id="00000003"))
    assert path.exists()
    assert path.read_text(encoding="utf-8") == '{"url":"https://example.com/a","abstract":"A"}\n'
    assert failed.calls == ["query"]
    assert success.calls == ["query"]
    assert orchestrator.quotas.get_web("failed").max_observed_in_use == 1
    assert orchestrator.quotas.get_web("success").max_observed_in_use == 1

    empty_provider = FakeKeywordSearchProvider("empty", [])
    empty_path = Path(
        await _orchestrator(tmp_path, [empty_provider]).keyword_search(
            "query", request_id="00000004"
        )
    )
    assert empty_path.exists()
    assert empty_path.read_text(encoding="utf-8") == ""

    store = URLStore()
    all_failed = _orchestrator(
        tmp_path,
        [
            FakeKeywordSearchProvider(
                "one",
                failure=failure,
            ),
            FakeKeywordSearchProvider(
                "two",
                failure=ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "two"),
            ),
        ],
        store,
    )
    before = set((tmp_path / "results").glob("keyword-*.jsonl"))
    with pytest.raises(ExecutionFailure) as all_failed_error:
        await all_failed.keyword_search("query", request_id="00000005")
    after = set((tmp_path / "results").glob("keyword-*.jsonl"))
    assert all_failed_error.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert all_failed_error.value.message == "All keyword search provider pipelines failed"
    assert after == before

    with pytest.raises(ExecutionFailure) as selected_error:
        await orchestrator.keyword_search("query", request_id="00000006", provider="failed")
    assert type(selected_error.value) is ExecutionFailure
    assert selected_error.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert selected_error.value.message == "All keyword search provider pipelines failed"
    assert set((tmp_path / "results").glob("keyword-*.jsonl")) == before
    assert success.calls == ["query"]
    assert failed.calls == ["query", "query"]


@pytest.mark.parametrize(
    ("invalid", "reason"),
    [
        (cast(KeywordSearchHit, object()), "invalid_record"),
        (KeywordSearchHit(cast(str, 1), snippet="Invalid"), "invalid_record"),
        (KeywordSearchHit("not-a-url", snippet="Invalid"), "invalid_url"),
    ],
)
async def test_keyword_staging_strict_abort_counts_only_processed_hits(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    invalid: KeywordSearchHit,
    reason: str,
) -> None:
    caplog.set_level("DEBUG", logger="agent_search_gateway.orchestrators.search")
    provider = FakeKeywordSearchProvider(
        "web",
        [
            KeywordSearchHit("https://example.org/before", snippet="Before"),
            invalid,
            KeywordSearchHit("https://example.org/after", snippet="Unprocessed"),
        ],
    )
    with pytest.raises(ExecutionFailure):
        await _orchestrator(tmp_path, [provider]).keyword_search("query", request_id="00000011")
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0] == {
        "provider": "web",
        "phase": "staging",
        "received": 3,
        "accepted": 1,
        "rejected": 1,
        "rewritten": 0,
        "rejection_counts": {reason: 1},
        "processed": 2,
        "unprocessed": 1,
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://google.com/url?q=https%3A%2F%2Fexample.org%2Ftarget",
        "https://EXAMPLE.org/direct",
    ],
)
async def test_keyword_staging_counts_only_recognized_goto_rewrites(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, url: str
) -> None:
    caplog.set_level("DEBUG", logger="agent_search_gateway.orchestrators.search")
    provider = FakeKeywordSearchProvider("web", [KeywordSearchHit(url, snippet="Abstract")])
    path = Path(
        await _orchestrator(tmp_path, [provider]).keyword_search("query", request_id="00000012")
    )
    assert path.read_text() == (
        '{"url":"' + url.replace("EXAMPLE.org", "example.org") + '","abstract":"Abstract"}\n'
    )
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["accepted"] == 1 and summaries[0]["rewritten"] == 0


@pytest.mark.parametrize("empty_list", [False, True])
async def test_keyword_staging_reports_empty_and_all_rejected_lists(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, empty_list: bool
) -> None:
    caplog.set_level("DEBUG", logger="agent_search_gateway.orchestrators.search")
    hits = [] if empty_list else [KeywordSearchHit("https://google.com/goto?url=invalid")]
    path = Path(
        await _orchestrator(tmp_path, [FakeKeywordSearchProvider("web", hits)]).keyword_search(
            "query", request_id="00000013"
        )
    )
    assert path.read_text() == ""
    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["received"] == summaries[0]["rejected"] == int(not empty_list)
    assert summaries[0]["accepted"] == summaries[0]["unprocessed"] == 0
