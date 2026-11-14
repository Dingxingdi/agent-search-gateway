import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

import pytest

from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit, URLFetchCandidate
from agent_search_gateway.providers.web.parallel import ParallelAdapter
from agent_search_gateway.url_normalization import normalize_url
from tests.support.http import RecordingJsonExecutor

_FIXTURES = Path(__file__).parents[2] / "fixtures/providers/parallel"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _adapter_with_policy(
    slot: Literal["search", "extract"],
    policy: object,
) -> ParallelAdapter:
    value = cast(Mapping[str, object], policy)
    if slot == "search":
        return ParallelAdapter(
            name="parallel",
            api_url="https://parallel.example.test/",
            secret=SecretValue("x"),
            http_executor=RecordingJsonExecutor([]),
            search_fetch_policy=value,
        )
    return ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([]),
        extract_fetch_policy=value,
    )


async def test_parallel_search_builds_minimal_v1_request_and_maps_excerpts() -> None:
    executor = RecordingJsonExecutor([_fixture("search.json")])
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
    )

    hits = await adapter.search("hello world")

    assert hits == [
        KeywordSearchHit(
            url="https://example.com/parallel",
            title="Parallel result",
            snippet="First relevant excerpt.\n\nSecond relevant excerpt.",
            raw_content="",
            content="",
        )
    ]
    [request] = executor.requests
    assert request.method == "POST"
    assert request.url == "https://parallel.example.test/v1/search"
    assert request.stage == "search"
    assert request.headers == {"x-api-key": "x"}
    assert request.json_body == {"search_queries": ["hello world"]}
    assert isinstance(request.json_body, dict)
    assert "objective" not in request.json_body
    assert "mode" not in request.json_body
    assert "max_results" not in request.json_body
    assert "session_id" not in request.json_body
    assert "advanced_settings" not in request.json_body


@pytest.mark.parametrize("mode", ["turbo", "fast", "basic", "advanced"])
async def test_parallel_search_includes_configured_mode(mode: str) -> None:
    executor = RecordingJsonExecutor([{"results": []}])
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
        mode=mode,
    )

    await adapter.search("hello world")

    [request] = executor.requests
    assert request.json_body == {"search_queries": ["hello world"], "mode": mode}


async def test_parallel_search_omits_mode_when_not_configured() -> None:
    executor = RecordingJsonExecutor([{"results": []}])
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
    )

    await adapter.search("hello world")

    [request] = executor.requests
    assert request.json_body == {"search_queries": ["hello world"]}


async def test_parallel_search_maps_only_search_fetch_policy() -> None:
    executor = RecordingJsonExecutor([{"results": []}])
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
        mode="turbo",
        search_fetch_policy={
            "max_age_seconds": 3600,
            "timeout_seconds": 15,
            "disable_cache_fallback": False,
        },
        extract_fetch_policy={
            "max_age_seconds": 600,
            "timeout_seconds": 30,
            "disable_cache_fallback": True,
        },
    )

    await adapter.search("hello world")

    [request] = executor.requests
    assert request.json_body == {
        "search_queries": ["hello world"],
        "mode": "turbo",
        "advanced_settings": {
            "fetch_policy": {
                "max_age_seconds": 3600,
                "timeout_seconds": 15,
                "disable_cache_fallback": False,
            }
        },
    }


@pytest.mark.parametrize(
    "policy",
    [
        {"max_age_seconds": 3600},
        {"timeout_seconds": 15},
        {"disable_cache_fallback": False},
        {},
    ],
)
async def test_parallel_search_maps_partial_search_fetch_policy(
    policy: dict[str, object],
) -> None:
    executor = RecordingJsonExecutor([{"results": []}])
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
        search_fetch_policy=policy,
    )

    await adapter.search("hello world")

    [request] = executor.requests
    assert request.json_body == {
        "search_queries": ["hello world"],
        "advanced_settings": {"fetch_policy": policy},
    }


@pytest.mark.parametrize(
    "malformed",
    [
        1,
        {"title": "missing URL", "excerpts": []},
        {"url": 1, "title": "bad URL", "excerpts": []},
        {"url": "", "title": "empty URL", "excerpts": []},
        {"url": "https://example.com/bad", "title": 1, "excerpts": []},
        {"url": "https://example.com/bad", "title": "missing excerpts"},
        {"url": "https://example.com/bad", "title": "bad excerpts", "excerpts": "x"},
        {
            "url": "https://example.com/bad",
            "title": "bad excerpt element",
            "excerpts": ["ok", 1],
        },
    ],
)
async def test_parallel_search_tolerance_skips_only_malformed_results(malformed: object) -> None:
    executor = RecordingJsonExecutor(
        [
            {
                "results": [
                    {
                        "url": "https://example.com/first",
                        "title": "First",
                        "excerpts": ["first excerpt"],
                    },
                    malformed,
                    {
                        "url": "https://example.com/second",
                        "title": "Second",
                        "excerpts": ["second excerpt"],
                    },
                ]
            }
        ]
    )
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
    )

    hits = await adapter.search("hello world")

    assert [hit.url for hit in hits] == [
        "https://example.com/first",
        "https://example.com/second",
    ]


@pytest.mark.parametrize("payload", [[], {}, {"results": {}}])
async def test_parallel_search_malformed_top_level_response_fails(payload: object) -> None:
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([payload]),
    )

    with pytest.raises(ExecutionFailure):
        await adapter.search("hello world")


async def test_parallel_search_empty_results_are_valid() -> None:
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([{"results": []}]),
    )

    assert await adapter.search("hello world") == []


@pytest.mark.parametrize(
    ("excerpts", "expected_snippet"),
    [
        ([], ""),
        (["one"], "one"),
        (["one", "two"], "one\n\ntwo"),
        (["", "two"], "\n\ntwo"),
    ],
)
async def test_parallel_search_excerpts_preserve_provider_values(
    excerpts: list[str], expected_snippet: str
) -> None:
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor(
            [
                {
                    "results": [
                        {
                            "url": "https://example.com/parallel",
                            "title": "Parallel result",
                            "excerpts": excerpts,
                        }
                    ]
                }
            ]
        ),
    )

    [hit] = await adapter.search("hello world")

    assert hit.snippet == expected_snippet


async def test_parallel_extract_requests_full_content_and_maps_matching_body() -> None:
    executor = RecordingJsonExecutor([_fixture("extract.json")])
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
    )

    candidate = await adapter.fetch(normalize_url("https://example.com/parallel"))

    assert candidate == URLFetchCandidate(
        raw_content="# Parallel page\n\nFull page content.",
        content="# Parallel page\n\nFull page content.",
    )
    [request] = executor.requests
    assert request.method == "POST"
    assert request.url == "https://parallel.example.test/v1/extract"
    assert request.stage == "fetch"
    assert request.headers == {"x-api-key": "x"}
    assert request.json_body == {
        "urls": ["https://example.com/parallel"],
        "advanced_settings": {"full_content": True},
    }
    assert isinstance(request.json_body, dict)
    assert "objective" not in request.json_body
    assert "search_queries" not in request.json_body
    assert "max_chars_total" not in request.json_body
    assert "session_id" not in request.json_body


async def test_parallel_extract_maps_only_extract_fetch_policy() -> None:
    executor = RecordingJsonExecutor([_fixture("extract.json"), {"results": []}])
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
        search_fetch_policy={"max_age_seconds": 3600},
        extract_fetch_policy={
            "max_age_seconds": 600,
            "timeout_seconds": 30,
            "disable_cache_fallback": True,
        },
    )

    await adapter.fetch(normalize_url("https://example.com/parallel"))
    await adapter.search("hello world")

    extract_request, search_request = executor.requests
    assert extract_request.json_body == {
        "urls": ["https://example.com/parallel"],
        "advanced_settings": {
            "full_content": True,
            "fetch_policy": {
                "max_age_seconds": 600,
                "timeout_seconds": 30,
                "disable_cache_fallback": True,
            },
        },
    }
    assert search_request.json_body == {
        "search_queries": ["hello world"],
        "advanced_settings": {"fetch_policy": {"max_age_seconds": 3600}},
    }


@pytest.mark.parametrize(
    "policy",
    [
        {"max_age_seconds": 600},
        {"timeout_seconds": 30},
        {"disable_cache_fallback": True},
        {},
    ],
)
async def test_parallel_extract_maps_partial_extract_fetch_policy(
    policy: dict[str, object],
) -> None:
    executor = RecordingJsonExecutor([_fixture("extract.json")])
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
        extract_fetch_policy=policy,
    )

    await adapter.fetch(normalize_url("https://example.com/parallel"))

    [request] = executor.requests
    assert request.json_body == {
        "urls": ["https://example.com/parallel"],
        "advanced_settings": {
            "full_content": True,
            "fetch_policy": policy,
        },
    }


async def test_parallel_extract_accepts_normalization_equivalent_result_url() -> None:
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor(
            [
                {
                    "results": [
                        {
                            "url": "https://EXAMPLE.COM/parallel",
                            "full_content": "normalized body",
                        }
                    ],
                    "errors": [],
                }
            ]
        ),
    )

    candidate = await adapter.fetch(normalize_url("https://example.com/parallel"))

    assert candidate == URLFetchCandidate("normalized body", "normalized body")


async def test_parallel_extract_returns_matching_result_after_unrelated_result() -> None:
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor(
            [
                {
                    "results": [
                        {"url": "https://example.com/other", "full_content": "other"},
                        {
                            "url": "https://example.com/parallel",
                            "full_content": "matching body",
                        },
                    ],
                    "errors": [],
                }
            ]
        ),
    )

    candidate = await adapter.fetch(normalize_url("https://example.com/parallel"))

    assert candidate == URLFetchCandidate("matching body", "matching body")


async def test_parallel_extract_prefers_matching_result_over_matching_error() -> None:
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor(
            [
                {
                    "results": [
                        {
                            "url": "https://example.com/parallel",
                            "full_content": "matching body",
                        }
                    ],
                    "errors": [{"url": "https://example.com/parallel"}],
                }
            ]
        ),
    )

    candidate = await adapter.fetch(normalize_url("https://example.com/parallel"))

    assert candidate == URLFetchCandidate("matching body", "matching body")


async def test_parallel_extract_matching_provider_error_raises_sanitized_failure() -> None:
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([_fixture("extract_error.json")]),
    )

    with pytest.raises(ExecutionFailure) as caught:
        await adapter.fetch(normalize_url("https://example.com/parallel"))

    assert "provider reported extraction failure" in str(caught.value)
    assert "provider diagnostic text" not in str(caught.value)


async def test_parallel_extract_without_matching_result_or_error_fails() -> None:
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor(
            [
                {
                    "results": [{"url": "https://example.com/other", "full_content": "other body"}],
                    "errors": [{"url": "https://example.com/also-other"}],
                }
            ]
        ),
    )

    with pytest.raises(ExecutionFailure, match="matching extraction result was not returned"):
        await adapter.fetch(normalize_url("https://example.com/parallel"))


async def test_parallel_extract_malformed_result_url_fails() -> None:
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor(
            [
                {
                    "results": [{"url": "ftp://example.com/parallel", "full_content": "body"}],
                    "errors": [],
                }
            ]
        ),
    )

    with pytest.raises(ExecutionFailure, match="result URL is invalid"):
        await adapter.fetch(normalize_url("https://example.com/parallel"))


@pytest.mark.parametrize(
    "result",
    [
        {"url": "https://example.com/parallel"},
        {"url": "https://example.com/parallel", "full_content": None},
        {"url": "https://example.com/parallel", "full_content": 1},
        {"url": "https://example.com/parallel", "full_content": ""},
        {"url": "https://example.com/parallel", "full_content": "   "},
    ],
)
async def test_parallel_extract_matching_invalid_full_content_fails(
    result: dict[str, object],
) -> None:
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([{"results": [result], "errors": []}]),
    )

    with pytest.raises(ExecutionFailure):
        await adapter.fetch(normalize_url("https://example.com/parallel"))


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"errors": []},
        {"results": {}, "errors": []},
        {"results": []},
        {"results": [], "errors": {}},
    ],
)
async def test_parallel_extract_malformed_top_level_response_fails(payload: object) -> None:
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([payload]),
    )

    with pytest.raises(ExecutionFailure):
        await adapter.fetch(normalize_url("https://example.com/parallel"))


@pytest.mark.parametrize("mode", [None, "turbo", "fast", "basic", "advanced"])
def test_parallel_accepts_valid_mode(mode: str | None) -> None:
    ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=RecordingJsonExecutor([]),
        mode=mode,
    )


@pytest.mark.parametrize("invalid", ["ADVANCED", "", 1, True, {}])
def test_parallel_invalid_mode_raises_type_error(invalid: object) -> None:
    with pytest.raises(TypeError):
        ParallelAdapter(
            name="parallel",
            api_url="https://parallel.example.test/",
            secret=SecretValue("x"),
            http_executor=RecordingJsonExecutor([]),
            mode=cast(str, invalid),
        )


@pytest.mark.parametrize("slot", ["search", "extract"])
@pytest.mark.parametrize("invalid", ["live", 1, True, []])
def test_parallel_policy_validation_rejects_non_mapping_container(
    slot: Literal["search", "extract"], invalid: object
) -> None:
    with pytest.raises(TypeError):
        _adapter_with_policy(slot, invalid)


@pytest.mark.parametrize("slot", ["search", "extract"])
def test_parallel_policy_validation_rejects_unknown_key(
    slot: Literal["search", "extract"],
) -> None:
    with pytest.raises(TypeError):
        _adapter_with_policy(slot, {"max_age_seconds": 600, "unknown": True})


@pytest.mark.parametrize("slot", ["search", "extract"])
@pytest.mark.parametrize(
    "policy",
    [
        {"max_age_seconds": 600},
        {"max_age_seconds": 3600},
        {"max_age_seconds": 86400},
        {"timeout_seconds": 1},
        {"timeout_seconds": 15},
        {"timeout_seconds": 30.5},
        {"disable_cache_fallback": True},
        {"disable_cache_fallback": False},
        {},
    ],
)
def test_parallel_policy_validation_accepts_valid_fields(
    slot: Literal["search", "extract"], policy: dict[str, object]
) -> None:
    _adapter_with_policy(slot, policy)


@pytest.mark.parametrize("slot", ["search", "extract"])
@pytest.mark.parametrize("invalid", [599, 0, -1, 600.0, True, "600"])
def test_parallel_policy_validation_rejects_invalid_max_age_seconds(
    slot: Literal["search", "extract"], invalid: object
) -> None:
    with pytest.raises(TypeError):
        _adapter_with_policy(slot, {"max_age_seconds": invalid})


@pytest.mark.parametrize("slot", ["search", "extract"])
@pytest.mark.parametrize("invalid", [True, False, "30", [], {}])
def test_parallel_policy_validation_rejects_invalid_timeout_seconds(
    slot: Literal["search", "extract"], invalid: object
) -> None:
    with pytest.raises(TypeError):
        _adapter_with_policy(slot, {"timeout_seconds": invalid})


@pytest.mark.parametrize("slot", ["search", "extract"])
@pytest.mark.parametrize("invalid", [0, 1, "false", None])
def test_parallel_policy_validation_rejects_invalid_disable_cache_fallback(
    slot: Literal["search", "extract"], invalid: object
) -> None:
    with pytest.raises(TypeError):
        _adapter_with_policy(slot, {"disable_cache_fallback": invalid})


async def test_parallel_policy_mutation_does_not_change_captured_configuration() -> None:
    search_policy: dict[str, object] = {
        "max_age_seconds": 3600,
        "disable_cache_fallback": False,
    }
    extract_policy: dict[str, object] = {
        "max_age_seconds": 600,
        "timeout_seconds": 30,
    }
    executor = RecordingJsonExecutor([{"results": []}, _fixture("extract.json")])
    adapter = ParallelAdapter(
        name="parallel",
        api_url="https://parallel.example.test/",
        secret=SecretValue("x"),
        http_executor=executor,
        search_fetch_policy=search_policy,
        extract_fetch_policy=extract_policy,
    )

    search_policy["max_age_seconds"] = 86400
    search_policy["timeout_seconds"] = 99
    extract_policy.clear()

    await adapter.search("hello world")
    await adapter.fetch(normalize_url("https://example.com/parallel"))

    search_request, extract_request = executor.requests
    assert search_request.json_body == {
        "search_queries": ["hello world"],
        "advanced_settings": {
            "fetch_policy": {
                "max_age_seconds": 3600,
                "disable_cache_fallback": False,
            }
        },
    }
    assert extract_request.json_body == {
        "urls": ["https://example.com/parallel"],
        "advanced_settings": {
            "full_content": True,
            "fetch_policy": {
                "max_age_seconds": 600,
                "timeout_seconds": 30,
            },
        },
    }


@pytest.mark.parametrize("case", ["error", "success", "strict"])
async def test_fetch_reports_safe_response_diagnostics(
    caplog: pytest.LogCaptureFixture, case: str
) -> None:
    import logging

    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.observability import DiagnosticText
    from agent_search_gateway.providers.http import HttpJsonExecutor
    from tests.support.web_diagnostics import assert_safe_response_diagnostic

    caplog.set_level(logging.DEBUG)
    target = "https://example.com/page?key=PRIVATE_QUERY"
    results: list[object] = (
        []
        if case == "error"
        else [{"url": target, "full_content": "PRIVATE_BODY", "error": "PRIVATE_CONTENT_ERROR"}]
    )
    if case == "strict":
        results.insert(0, None)
    payload = {
        "results": results,
        "errors": [
            {"url": "https://example.com/other", "error": "PRIVATE_UNMATCHED"},
            {
                "url": target,
                "error": {
                    "code": "blocked",
                    "message": "target blocked",
                    "body": "PRIVATE_ERROR_BODY",
                },
            },
        ],
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        adapter = ParallelAdapter(
            name="parallel",
            api_url="https://parallel.example.test",
            secret=SecretValue("PRIVATE_AUTH"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(1, 0, 0, 5), provider_name="parallel"
            ),
        )
        if case == "success":
            assert (await adapter.fetch(normalize_url(target))).content == "PRIVATE_BODY"
        else:
            with pytest.raises(ExecutionFailure) as caught:
                await adapter.fetch(normalize_url(target))
            if case == "error":
                assert (
                    caught.value.message == "parallel/fetch: provider reported extraction failure"
                )
                message = caught.value.diagnostics["upstream_message"]
                assert isinstance(message, DiagnosticText)
                assert message.text == "target blocked"
            else:
                assert caught.value.message == "parallel/fetch: result must be an object"
                assert caught.value.diagnostics["semantic_result"] == "error"
    _, rendered = assert_safe_response_diagnostic(
        caplog, "success" if case == "success" else "error"
    )
    if case == "error":
        assert "upstream_code=blocked" in rendered
        assert 'upstream_message="target blocked"' in rendered
    else:
        assert "target blocked" not in rendered
