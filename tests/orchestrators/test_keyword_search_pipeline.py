from pathlib import Path

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
