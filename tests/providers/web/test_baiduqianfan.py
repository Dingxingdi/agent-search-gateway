import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor, SecretValue
from agent_search_gateway.providers.contracts import KeywordSearchHit
from agent_search_gateway.providers.http import HttpJsonExecutor, ProviderResponseFailure
from agent_search_gateway.providers.web.baiduqianfan import BaiduQianfanAdapter
from tests.support.http import RecordingJsonExecutor

_FIXTURES = Path(__file__).parents[2] / "fixtures/providers/baiduqianfan"
_WEB = "/v2/ai_search/web_search"
_AGENTIC = "/v2/ai_search/agentic_search"


def _fixture(filename: str) -> object:
    return json.loads((_FIXTURES / filename).read_text(encoding="utf-8"))


async def test_baiduqianfan_search_posts_both_official_payloads() -> None:
    query = "search protocol"
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        fixtures = {_WEB: "web_success.json", _AGENTIC: "agentic_success.json"}
        return httpx.Response(200, json=_fixture(fixtures[request.url.path]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = BaiduQianfanAdapter(
            name="baiduqianfan",
            secret=SecretValue("fixture-key"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(1, 0, 0, 5), provider_name="baiduqianfan"
            ),
        )
        hits = await adapter.search(query)

    assert len(requests) == 2
    assert all(request.method == "POST" for request in requests)
    assert all(request.headers["Authorization"] == "Bearer fixture-key" for request in requests)
    assert all(request.headers["Content-Type"] == "application/json" for request in requests)
    assert {str(request.url): json.loads(request.content) for request in requests} == {
        f"https://qianfan.baidubce.com{_WEB}": {
            "messages": [{"role": "user", "content": query}],
            "search_source": "baidu_search_v2",
            "edition": "standard",
            "resource_type_filter": [
                {"type": "web", "top_k": 20},
                {"type": "image", "top_k": 10},
                {"type": "video", "top_k": 10},
            ],
        },
        f"https://qianfan.baidubce.com{_AGENTIC}": {"query": query, "count": 10},
    }
    assert hits == [
        KeywordSearchHit("https://example.test/web", "Web title", "Web snippet"),
        KeywordSearchHit(
            "https://example.test/agentic",
            "Agentic title",
            "Agentic snippet",
            raw_content="Original page text.",
        ),
    ]


async def test_baiduqianfan_appends_both_paths_to_configured_base() -> None:
    http = RecordingJsonExecutor([_fixture("web_success.json"), _fixture("agentic_success.json")])
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan",
        secret=SecretValue("fixture-key"),
        http_executor=http,
        api_url="https://example.test/proxy/",
    )

    await adapter.search("web pages")

    assert {request.url for request in http.requests} == {
        f"https://example.test/proxy{_WEB}",
        f"https://example.test/proxy{_AGENTIC}",
    }


async def test_baiduqianfan_preserves_query_text() -> None:
    query = "  中文 & /? +\n第二行  "
    http = RecordingJsonExecutor([_fixture("web_success.json"), _fixture("agentic_success.json")])
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan", secret=SecretValue("fixture-key"), http_executor=http
    )

    await adapter.search(query)

    bodies = {request.stage: request.json_body for request in http.requests}
    web = bodies["web_search"]
    assert isinstance(web, dict)
    assert web["messages"] == [{"role": "user", "content": query}]
    assert bodies["agentic_search"] == {"query": query, "count": 10}


async def test_baiduqianfan_maps_web_search_url_bearing_results() -> None:
    http = RecordingJsonExecutor(
        [_fixture("web_multimodal.json"), {"web_page_result": {"web_pages": []}}]
    )
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan", secret=SecretValue("fixture-key"), http_executor=http
    )

    hits = await adapter.search("web and media")

    assert hits == [
        KeywordSearchHit(
            "https://example.test/web?x=1#part", "  网页标题  ", "  原文摘要\n第二行  "
        ),
        KeywordSearchHit("https://example.test/photo.jpg", "Image", "Image"),
        KeywordSearchHit("https://example.test/movie.mp4", "Video", "Video"),
        KeywordSearchHit("https://example.test/nested-image.jpg", "Image", "Image"),
        KeywordSearchHit("https://example.test/nested-video.mp4", "Video", "Video"),
        KeywordSearchHit(
            "https://example.test/top-image", "Original image title", "Original image snippet"
        ),
        KeywordSearchHit(
            "https://example.test/top-video", "Original video title", "Original video snippet"
        ),
    ]
    assert all(hit.raw_content == hit.content == "" for hit in hits)


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(None, id="non-object"),
        pytest.param({}, id="missing-url"),
        pytest.param({"url": ""}, id="empty-url"),
        pytest.param({"url": "ftp://example.test/file"}, id="ftp-url"),
        pytest.param({"url": "javascript:alert(1)"}, id="javascript-url"),
        pytest.param({"url": "https:///missing-host"}, id="missing-host"),
        pytest.param({"url": "https://[broken/path"}, id="invalid-host"),
        pytest.param({"url": "https://example.test:70000/page"}, id="invalid-port"),
        pytest.param({"url": "https://example.test/a", "title": 42}, id="invalid-title"),
        pytest.param({"url": "https://example.test/b", "snippet": []}, id="invalid-snippet"),
        pytest.param(
            {
                "type": "image",
                "url": "ftp://example.test/bad",
                "image": {"url": "https://example.test/fallback"},
            },
            id="bad-top-level-image",
        ),
        pytest.param(
            {"type": "video", "url": 7, "video": {"url": "https://example.test/fallback"}},
            id="invalid-top-level-type",
        ),
        pytest.param(
            {
                "type": "web",
                "details": {"url": "https://example.test/deep"},
                "image": {"url": "https://example.test/not-image"},
            },
            id="unknown-deep-url",
        ),
        pytest.param(
            {
                "type": "image",
                "icon": "https://example.test/icon",
                "hover_pic": "https://example.test/hover",
            },
            id="decorative-urls",
        ),
        pytest.param({"type": "card", "card": {"url": "https://example.test/card"}}, id="card-url"),
    ],
)
async def test_baiduqianfan_rejects_bad_web_candidates_without_losing_siblings(bad: object) -> None:
    good = {"url": "https://example.test/good", "title": "Good", "snippet": "Keep me"}
    http = RecordingJsonExecutor(
        [{"references": [good, bad, good]}, {"web_page_result": {"web_pages": []}}]
    )
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan", secret=SecretValue("fixture-key"), http_executor=http
    )

    hits = await adapter.search("mixed results")

    assert hits == [KeywordSearchHit(good["url"], "Good", "Keep me")] * 2


@pytest.mark.parametrize("top_level_url", [" \t ", None], ids=["whitespace", "null"])
async def test_baiduqianfan_uses_media_fallback_for_blank_top_level_url(
    top_level_url: object,
) -> None:
    http = RecordingJsonExecutor(
        [
            {
                "references": [
                    {
                        "type": "image",
                        "url": top_level_url,
                        "image": {"url": "https://example.test/image"},
                    }
                ]
            },
            {"web_page_result": {"web_pages": []}},
        ]
    )
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan", secret=SecretValue("fixture-key"), http_executor=http
    )

    assert await adapter.search("media") == [
        KeywordSearchHit("https://example.test/image", "Image", "Image")
    ]


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize(
    ("media", "title", "reason"),
    [
        ({"url": "https://example.test/media"}, 42, "invalid_record"),
        ({}, "Valid title", "missing_url"),
    ],
)
async def test_baiduqianfan_classifies_rejected_media_by_selected_url(
    kind: str, media: dict[str, str], title: object, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    record = {"type": kind, kind: media, "title": title}
    http = RecordingJsonExecutor([{"references": [record]}, {"image_result": {"images": []}}])
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan", secret=SecretValue("fixture-key"), http_executor=http
    )
    caplog.set_level("DEBUG", logger="agent_search_gateway.providers.web.baiduqianfan")

    assert await adapter.search("media") == []

    counts = _candidate_counts(caplog, "web_search")
    keys = ("received", "accepted", "rejected", "unprocessed")
    assert [counts[key] for key in keys] == [1, 0, 1, 0]
    assert counts["rejection_counts"] == {reason: 1}


async def test_baiduqianfan_preserves_duplicate_urls() -> None:
    original = "  HTTPS://Example.TEST:443/path?b=2&a=1#fragment  "
    record = {"url": original, "title": "Original", "snippet": "Preserve me"}
    http = RecordingJsonExecutor(
        [{"references": [record, record]}, {"web_page_result": {"web_pages": []}}]
    )
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan", secret=SecretValue("fixture-key"), http_executor=http
    )

    assert (
        await adapter.search("duplicates")
        == [KeywordSearchHit(original, "Original", "Preserve me")] * 2
    )


async def test_baiduqianfan_accepts_empty_web_references() -> None:
    http = RecordingJsonExecutor(
        [_fixture("web_empty.json"), {"web_page_result": {"web_pages": []}}]
    )
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan", secret=SecretValue("fixture-key"), http_executor=http
    )

    assert await adapter.search("no results") == []


async def _search_http_branch(
    stage: str,
    respond: Callable[[httpx.Request], httpx.Response],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[KeywordSearchHit]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = BaiduQianfanAdapter(
            name="baiduqianfan",
            secret=SecretValue("fixture-key"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(2, 0, 0, 5), provider_name="baiduqianfan", sleep=sleep
            ),
        )
        search = adapter._search_web if stage == "web_search" else adapter._search_agentic
        return await search("branch response")


@pytest.mark.parametrize(
    ("stage", "payload", "detail"),
    [
        ("web_search", b"not json", "response was not valid JSON"),
        ("agentic_search", b"not json", "response was not valid JSON"),
        ("web_search", [], "response must be an object"),
        ("agentic_search", [], "response must be an object"),
        ("web_search", {}, "references must be an array"),
        ("web_search", {"references": None}, "references must be an array"),
        ("web_search", {"references": {}}, "references must be an array"),
        ("web_search", {"references": "invalid"}, "references must be an array"),
        ("agentic_search", {"other": []}, "missing a result container"),
        ("agentic_search", {"image_result": []}, "image_result must be an object"),
        ("agentic_search", {"video_result": {"videos": {}}}, "videos must be an array"),
    ],
)
async def test_baiduqianfan_invalid_envelope_is_a_branch_failure(
    stage: str, payload: object, detail: str
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if isinstance(payload, bytes):
            return httpx.Response(200, content=payload)
        return httpx.Response(200, json=payload)

    failure = ProtocolFailure if isinstance(payload, bytes) else ExecutionFailure
    with pytest.raises(failure, match=detail):
        await _search_http_branch(stage, respond)


async def test_baiduqianfan_preserves_blank_web_title_and_snippet() -> None:
    http = RecordingJsonExecutor(
        [
            {
                "references": [
                    {
                        "type": "web",
                        "url": "https://example.test/web",
                        "title": "  ",
                        "snippet": "   ",
                    }
                ]
            },
            {"web_page_result": {"web_pages": []}},
        ]
    )
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan", secret=SecretValue("fixture-key"), http_executor=http
    )

    assert await adapter.search("blank text") == [
        KeywordSearchHit("https://example.test/web", "  ", "   ")
    ]


async def test_baiduqianfan_maps_agentic_modalities_and_raw_content() -> None:
    http = RecordingJsonExecutor([_fixture("web_empty.json"), _fixture("agentic_multimodal.json")])
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan", secret=SecretValue("fixture-key"), http_executor=http
    )

    hits = await adapter.search("pages and media")

    assert hits == [
        KeywordSearchHit(
            "https://example.test/page#section",
            "Page",
            "Excerpt",
            raw_content="Original page text.\nSecond line.",
        ),
        KeywordSearchHit("https://example.test/image.jpg", "Image", "Image"),
        KeywordSearchHit("https://example.test/video.mp4", "Video", "Video"),
    ]


@pytest.mark.parametrize(
    "agentic",
    [
        pytest.param(_fixture("agentic_empty.json"), id="empty-arrays"),
        pytest.param({"web_page_result": {"web_pages": []}}, id="only-web-pages"),
        pytest.param({"image_result": {"images": None}}, id="only-null-images"),
        pytest.param({"video_result": {"videos": None}}, id="only-null-videos"),
    ],
)
async def test_baiduqianfan_accepts_empty_and_null_modalities(agentic: object) -> None:
    http = RecordingJsonExecutor([_fixture("web_empty.json"), agentic])
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan", secret=SecretValue("fixture-key"), http_executor=http
    )

    assert await adapter.search("empty results") == []


@pytest.mark.parametrize("empty", [False, True])
async def test_baiduqianfan_logs_only_safe_branch_diagnostics(
    empty: bool, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="agent_search_gateway.providers.web.baiduqianfan")
    web = [] if empty else [{"url": "https://example.test/web"}, {}]
    images = [] if empty else [{"url": "https://example.test/image"}, None, {}]
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan",
        secret=SecretValue("fixture-key"),
        http_executor=RecordingJsonExecutor(
            [{"references": web}, {"image_result": {"images": images}}]
        ),
    )

    await adapter.search("branch counts")

    summaries = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_candidates_summary"
    ]
    assert sorted(fields["stage"] for fields in summaries) == ["agentic_search", "web_search"]
    for fields in summaries:
        expected = len(web) if fields["stage"] == "web_search" else len(images)
        assert fields["provider"] == "baiduqianfan"
        assert fields["phase"] == "adapter"
        assert fields["received"] == expected
        assert fields["accepted"] == (0 if empty else 1)
        assert fields["accepted"] + fields["rejected"] == expected
        assert fields["unprocessed"] == 0


@pytest.mark.parametrize(
    ("status", "code", "expected_type", "expected_code"),
    [
        (None, None, "RuntimeError", None),
        (200, "InvalidArgument", "ProviderResponseFailure", "InvalidArgument"),
        (200, "demo-code", "ProviderResponseFailure", None),
        (429, "QUOTA_USER_DAILY_FREE", "ProviderResponseFailure", "QUOTA_USER_DAILY_FREE"),
        (429, "RATE_LIMIT_SEARCH_QPS", "HttpStatusFailure", "RATE_LIMIT_SEARCH_QPS"),
        (429, "demo-code", "HttpStatusFailure", None),
    ],
)
async def test_baiduqianfan_ordinary_exception_text_is_never_rendered(
    status: int | None,
    code: str | None,
    expected_type: str,
    expected_code: str | None,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level("DEBUG")
    formatter = KeyValueFormatter(SecretRedactor([SecretValue("fixture-key")]))
    monkeypatch.setattr(caplog.handler, "formatter", formatter)
    signature = "demo-sig"
    signed_url = f"https://example.test/demo-link?signature={signature}"
    private_message = "demo-msg"
    request_id = "demo-id"
    private_exception = "demo-exc"
    card_body = "demo-card"
    auth_value = "demo-auth"
    token_value = "demo-tok"

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == _AGENTIC:
            return httpx.Response(
                200,
                json={
                    "image_result": {"images": [{"url": signed_url}]},
                    "card_result": {"cards": [{"content": card_body}]},
                    "echo": request.headers["Authorization"].removeprefix("Bearer "),
                },
            )
        if status is None:
            raise RuntimeError(private_exception)
        return httpx.Response(
            status,
            json={
                "code": code,
                "message": private_message,
                "request_id": request_id,
                "Authorization": f"Bearer {auth_value}",
                "access_token": token_value,
            },
        )

    hits = await _search_both(httpx.MockTransport(respond))

    assert [hit.url for hit in hits] == [signed_url]
    events = [r for r in caplog.records if getattr(r, "gateway_event", None)]
    adapter_events = [
        r for r in events if r.name == "agent_search_gateway.providers.web.baiduqianfan"
    ]
    summary, partial = sorted(adapter_events, key=lambda r: vars(r)["gateway_event"])
    assert vars(summary)["gateway_event"] == "provider_candidates_summary"
    assert vars(summary)["gateway_fields"]["stage"] == "agentic_search"
    assert vars(partial)["gateway_event"] == "provider_partial_failure"
    fields = vars(partial)["gateway_fields"]
    assert fields["branch"] == "web_search"
    assert fields["reason"] == "execution_failure"
    assert fields["error_type"] == expected_type
    assert getattr(fields.get("upstream_code"), "text", None) == expected_code
    adapter_output = "\n".join(formatter.format(r) for r in adapter_events)
    for marker in (
        private_exception,
        private_message,
        request_id,
        "demo-code",
        "demo-link",
        signature,
        card_body,
        "two branches",
    ):
        assert marker not in adapter_output
        assert all(marker not in repr(vars(r)["gateway_fields"]) for r in adapter_events)
    all_output = caplog.text
    assert "event=upstream_body_preview" in all_output
    assert "image_result" in all_output
    for secret in ("fixture-key", auth_value, token_value):
        assert secret not in all_output


def _candidate_counts(caplog: pytest.LogCaptureFixture, stage: str) -> dict[str, object]:
    summaries: list[dict[str, object]] = []
    for record in caplog.records:
        if getattr(record, "gateway_event", None) == "provider_candidates_summary":
            fields = getattr(record, "gateway_fields", None)
            if isinstance(fields, dict) and fields.get("stage") == stage:
                summaries.append(fields)
    assert len(summaries) == 1
    return summaries[0]


async def test_baiduqianfan_keeps_page_with_null_cards(caplog: pytest.LogCaptureFixture) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        fixtures = {_WEB: "web_empty.json", _AGENTIC: "agentic_null_cards.json"}
        return httpx.Response(200, json=_fixture(fixtures[request.url.path]))

    caplog.set_level("DEBUG", logger="agent_search_gateway.providers.web.baiduqianfan")
    hits = await _search_both(httpx.MockTransport(respond))

    assert hits == [
        KeywordSearchHit(
            "https://example.test/library-hours",
            "Library hours",
            "Opening times",
            raw_content="The library opens at nine.",
        )
    ]
    assert hits[0].content == ""
    counts = _candidate_counts(caplog, "agentic_search")
    assert [counts[key] for key in ("received", "accepted", "rejected", "unprocessed")] == [
        1,
        1,
        0,
        0,
    ]
    assert all(
        getattr(record, "gateway_event", None) != "provider_partial_failure"
        for record in caplog.records
    )


@pytest.mark.parametrize(
    "agentic",
    [
        pytest.param(_fixture("agentic_card_only.json"), id="card-array"),
        pytest.param({"card_result": {"cards": None}}, id="null-cards"),
    ],
)
async def test_baiduqianfan_accepts_card_only_response(
    agentic: object, caplog: pytest.LogCaptureFixture
) -> None:
    http = RecordingJsonExecutor([_fixture("web_empty.json"), agentic])
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan", secret=SecretValue("fixture-key"), http_executor=http
    )
    caplog.set_level("DEBUG", logger="agent_search_gateway.providers.web.baiduqianfan")

    assert await adapter.search("cards") == []

    counts = _candidate_counts(caplog, "agentic_search")
    assert [counts[key] for key in ("received", "accepted", "rejected", "unprocessed")] == [0] * 4


@pytest.mark.parametrize(
    ("container", "key"),
    [
        ("web_page_result", "web_pages"),
        ("image_result", "images"),
        ("video_result", "videos"),
    ],
)
async def test_baiduqianfan_rejects_bad_agentic_candidates_without_losing_siblings(
    container: str, key: str, caplog: pytest.LogCaptureFixture
) -> None:
    groups: dict[str, dict[str, list[object]]] = {
        "web_page_result": {"web_pages": [{"url": "https://example.test/page", "content": "Text"}]},
        "image_result": {"images": [{"url": "https://example.test/image"}]},
        "video_result": {"videos": [{"url": "https://example.test/video"}]},
    }
    invalid: list[object] = [
        None,
        {},
        {"url": "ftp://example.test/bad"},
        {"url": "https://example.test/bad-title", "title": []},
        {"url": "https://example.test/bad-snippet", "snippet": 5},
    ]
    if key == "web_pages":
        invalid.append({"url": "https://example.test/bad-content", "content": {}})
    groups[container][key].extend([*invalid, {"url": "https://example.test/sibling"}])
    http = RecordingJsonExecutor([_fixture("web_empty.json"), groups])
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan", secret=SecretValue("fixture-key"), http_executor=http
    )
    caplog.set_level("DEBUG", logger="agent_search_gateway.providers.web.baiduqianfan")

    hits = await adapter.search("mixed agentic candidates")

    assert {hit.url for hit in hits} == {
        "https://example.test/page",
        "https://example.test/image",
        "https://example.test/video",
        "https://example.test/sibling",
    }
    assert [hit.raw_content for hit in hits if hit.raw_content] == ["Text"]
    assert all(hit.content == "" for hit in hits)
    counts = _candidate_counts(caplog, "agentic_search")
    assert [counts[key] for key in ("received", "accepted", "rejected", "unprocessed")] == [
        4 + len(invalid),
        4,
        len(invalid),
        0,
    ]


@pytest.mark.parametrize(
    "malformed",
    [
        {"web_page_result": {"web_pages": None}},
        {"image_result": {}},
        {"video_result": None},
        {"card_result": None},
        {"card_result": {}},
        {"card_result": {"cards": "invalid"}},
        {"card_result": {"cards": 0}},
        {"card_result": {"cards": {}}},
    ],
)
async def test_baiduqianfan_validates_present_agentic_containers(
    malformed: dict[str, object],
) -> None:
    payload = {"image_result": {"images": []}, **malformed}
    adapter = BaiduQianfanAdapter(
        name="baiduqianfan",
        secret=SecretValue("fixture-key"),
        http_executor=RecordingJsonExecutor([payload]),
    )

    with pytest.raises(ExecutionFailure):
        await adapter._search_agentic("invalid modality")


@pytest.mark.parametrize("stage", ["web_search", "agentic_search"])
@pytest.mark.parametrize(
    ("case", "expected_reason", "expected_code"),
    [
        ("invalid_argument", "business_error", "InvalidArgument"),
        ("internal_error", "business_error", "InternalError"),
        ("numeric", "business_error", "216003"),
        ("unknown", "business_error", None),
        ("object", "invalid_envelope", None),
        ("numeric_zero", "business_error", None),
        ("string_zero", "business_error", None),
        ("null", "invalid_envelope", None),
    ],
)
async def test_baiduqianfan_http_200_business_error_is_not_empty_success(
    stage: str, case: str, expected_reason: str, expected_code: str | None
) -> None:
    errors = _fixture("business_errors.json")
    assert isinstance(errors, dict)
    success = _fixture("web_success.json" if stage == "web_search" else "agentic_success.json")
    assert isinstance(success, dict)
    payload = {
        **success,
        **errors[case],
        "message": "private-fixture-message",
        "request_id": "private-fixture-id",
    }
    attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, json=payload)

    with pytest.raises(ProviderResponseFailure) as caught:
        await _search_http_branch(stage, respond)

    exc = caught.value
    assert attempts == 1
    assert exc.retryable is False
    assert exc.status_code == 200
    assert exc.safe_reason == expected_reason
    code = exc.diagnostics.get("upstream_code")
    if expected_code is None:
        assert code is None
    else:
        assert getattr(code, "text", None) == expected_code
    assert f"baiduqianfan/{stage}" in exc.message
    assert "private-fixture" not in f"{exc.message} {exc.diagnostics!r}"


async def _search_both(
    transport: httpx.MockTransport, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
) -> list[KeywordSearchHit]:
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = BaiduQianfanAdapter(
            name="baiduqianfan",
            secret=SecretValue("fixture-key"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(2, 0, 0, 5), provider_name="baiduqianfan", sleep=sleep
            ),
        )
        return await adapter.search("two branches")


async def test_baiduqianfan_starts_both_branches_before_either_finishes() -> None:
    entered = {path: asyncio.Event() for path in (_WEB, _AGENTIC)}
    release = asyncio.Event()
    fixtures = {_WEB: "web_success.json", _AGENTIC: "agentic_success.json"}

    async def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        entered[path].set()
        await release.wait()
        return httpx.Response(200, json=_fixture(fixtures[path]))

    search = asyncio.create_task(_search_both(httpx.MockTransport(respond)))
    try:
        async with asyncio.timeout(1):
            await asyncio.gather(*(event.wait() for event in entered.values()))
        assert not search.done()
    finally:
        release.set()
        hits = await search
    assert [hit.title for hit in hits] == ["Web title", "Agentic title"]


@pytest.mark.parametrize("failed_path", [_WEB, _AGENTIC])
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize(
    ("status", "body", "error_type"),
    [
        (403, b"private-fixture-body", "HttpStatusFailure"),
        (200, b"private-fixture-body", "ProtocolFailure"),
        (
            200,
            b'{"code":"InvalidArgument","message":"private-fixture-body"}',
            "ProviderResponseFailure",
        ),
    ],
)
async def test_baiduqianfan_preserves_successful_branch_after_sibling_failure(
    failed_path: str,
    empty: bool,
    status: int,
    body: bytes,
    error_type: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    attempts = dict.fromkeys((_WEB, _AGENTIC), 0)
    survivor = "agentic" if failed_path == _WEB else "web"
    fixture = f"{survivor}_{'empty' if empty else 'success'}.json"

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        attempts[path] += 1
        if path == failed_path:
            return httpx.Response(status, content=body)
        return httpx.Response(200, json=_fixture(fixture))

    hits = await _search_both(httpx.MockTransport(respond))

    assert [hit.url for hit in hits] == ([] if empty else [f"https://example.test/{survivor}"])
    assert attempts == {_WEB: 1, _AGENTIC: 1}
    (partial,) = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_partial_failure"
    ]
    assert partial["provider"] == "baiduqianfan"
    assert partial["stage"] == "search"
    assert partial["branch"] == failed_path.rsplit("/", 1)[1]
    assert partial["error_type"] == error_type
    assert partial["reason"] == "execution_failure"
    assert "private-fixture-body" not in repr(partial)
    assert not any(
        getattr(record, "gateway_event", None) in {"provider_completed", "provider_failed"}
        for record in caplog.records
    )


@pytest.mark.parametrize("cancelled_path", [_WEB, _AGENTIC])
async def test_baiduqianfan_branch_cancellation_cancels_and_awaits_sibling(
    cancelled_path: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    sibling_entered = asyncio.Event()
    sibling_cleaned = asyncio.Event()
    blocked = asyncio.Event()
    cancellation = asyncio.CancelledError("branch cancelled")
    sibling_tasks: list[asyncio.Task[object]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == cancelled_path:
            await sibling_entered.wait()
            raise cancellation
        task = asyncio.current_task()
        assert task is not None
        sibling_tasks.append(task)
        sibling_entered.set()
        try:
            await blocked.wait()
            raise AssertionError("blocked request returned")
        finally:
            sibling_cleaned.set()

    try:
        async with asyncio.timeout(1):
            with pytest.raises(asyncio.CancelledError) as caught:
                await _search_both(httpx.MockTransport(respond))
            assert caught.value is cancellation
            assert sibling_cleaned.is_set()
            assert sibling_tasks[0].cancelled()
    finally:
        for task in sibling_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*sibling_tasks, return_exceptions=True)

    assert not any(
        getattr(record, "gateway_event", None)
        in {"provider_partial_failure", "provider_candidates_summary", "provider_completed"}
        for record in caplog.records
    )


@pytest.mark.parametrize("repeat_cancel", [False, True], ids=["once", "twice"])
async def test_baiduqianfan_caller_cancellation_awaits_asynchronous_branch_cleanup(
    repeat_cancel: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    entered = {path: asyncio.Event() for path in (_WEB, _AGENTIC)}
    blocked = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    first_done = asyncio.Event()
    tasks: dict[str, asyncio.Task[object]] = {}
    cleaned: set[str] = set()

    async def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        task = asyncio.current_task()
        assert task is not None
        tasks[path] = task
        if path == _WEB:
            task.add_done_callback(lambda _: first_done.set())
        entered[path].set()
        try:
            await blocked.wait()
            raise AssertionError("blocked request returned")
        finally:
            if path == _AGENTIC:
                cleanup_started.set()
                await release_cleanup.wait()
            cleaned.add(path)

    search = asyncio.create_task(_search_both(httpx.MockTransport(respond)))
    try:
        async with asyncio.timeout(1):
            await asyncio.gather(*(event.wait() for event in entered.values()))
            search.cancel("caller cancelled")
            await cleanup_started.wait()
            await first_done.wait()
            assert not search.done()
            if repeat_cancel:
                # Let search start awaiting the remaining branch after Web finishes.
                await asyncio.sleep(0)
                search.cancel("second cancellation during cleanup")
                await asyncio.sleep(0)
            release_cleanup.set()
            with pytest.raises(asyncio.CancelledError, match="caller cancelled"):
                await search
            assert cleaned == {_WEB, _AGENTIC}
            assert all(task.cancelled() for task in tasks.values())
    finally:
        release_cleanup.set()
        for task in (search, *tasks.values()):
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(search, *tasks.values(), return_exceptions=True)

    assert not any(
        getattr(record, "gateway_event", None)
        in {"provider_partial_failure", "provider_candidates_summary", "provider_completed"}
        for record in caplog.records
    )


async def test_baiduqianfan_retry_sleep_cancellation_stops_later_requests(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    sleep_entered = asyncio.Event()
    blocked = asyncio.Event()
    sleep_cleaned = asyncio.Event()
    sibling_responded = asyncio.Event()
    delays: list[float] = []
    attempts = dict.fromkeys((_WEB, _AGENTIC), 0)

    async def sleep(delay: float) -> None:
        delays.append(delay)
        sleep_entered.set()
        try:
            await blocked.wait()
        finally:
            sleep_cleaned.set()

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        attempts[path] += 1
        if path == _WEB:
            return httpx.Response(429, headers={"Retry-After": "2"})
        sibling_responded.set()
        return httpx.Response(200, json=_fixture("agentic_success.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = BaiduQianfanAdapter(
            name="baiduqianfan",
            secret=SecretValue("fixture-key"),
            http_executor=HttpJsonExecutor(
                client, RetryPolicy(3, 0, 0, 5), provider_name="baiduqianfan", sleep=sleep
            ),
        )
        search = asyncio.create_task(adapter.search("cancel retry wait"))
        try:
            async with asyncio.timeout(1):
                await asyncio.gather(sleep_entered.wait(), sibling_responded.wait())
                search.cancel("retry sleep cancelled")
                with pytest.raises(asyncio.CancelledError, match="retry sleep cancelled"):
                    await search
        finally:
            if not search.done():
                search.cancel()
            await asyncio.gather(search, return_exceptions=True)

    assert search.cancelled()
    assert delays == [2.0]
    assert sleep_cleaned.is_set()
    assert attempts == {_WEB: 1, _AGENTIC: 1}
    assert not any(
        getattr(record, "gateway_event", None) in {"provider_partial_failure", "provider_completed"}
        for record in caplog.records
    )


def _success_response(path: str) -> httpx.Response:
    fixtures = {_WEB: "web_success.json", _AGENTIC: "agentic_success.json"}
    return httpx.Response(200, json=_fixture(fixtures[path]))


@pytest.mark.parametrize("failed_path", [_WEB, _AGENTIC])
@pytest.mark.parametrize("case", ["daily_quota", "insufficient_balance"])
async def test_baiduqianfan_quota_and_balance_429_are_not_retried(
    failed_path: str, case: str, caplog: pytest.LogCaptureFixture
) -> None:
    errors = _fixture("business_errors.json")
    assert isinstance(errors, dict)
    attempts = dict.fromkeys((_WEB, _AGENTIC), 0)

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        attempts[path] += 1
        if path == failed_path:
            return httpx.Response(429, json=errors[case])
        return _success_response(path)

    hits = await _search_both(httpx.MockTransport(respond))

    assert attempts == {_WEB: 1, _AGENTIC: 1}
    survivor = "agentic" if failed_path == _WEB else "web"
    assert [hit.url for hit in hits] == [f"https://example.test/{survivor}"]
    (partial,) = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", None) == "provider_partial_failure"
    ]
    assert partial["error_type"] == "ProviderResponseFailure"
    assert partial["http_status"] == 429
    assert getattr(partial["upstream_code"], "text", None) == errors[case]["code"]


async def test_baiduqianfan_undecodable_nested_429_keeps_retry_after() -> None:
    class NestedResponse(httpx.Response):
        def json(self, **kwargs: object) -> object:
            raise RecursionError("JSON exceeds decoder depth")

    attempts = 0
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return NestedResponse(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"references": []})

    assert await _search_http_branch("web_search", respond, sleep=sleep) == []
    assert attempts == 2
    assert delays == [1.0]


@pytest.mark.parametrize("limited_path", [_WEB, _AGENTIC])
@pytest.mark.parametrize("case", ["search_qps", "unknown", "invalid_json"])
async def test_baiduqianfan_qps_and_unknown_429_keep_retry_after(
    limited_path: str, case: str
) -> None:
    errors = _fixture("business_errors.json")
    assert isinstance(errors, dict)
    attempts = dict.fromkeys((_WEB, _AGENTIC), 0)
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        attempts[path] += 1
        if path == limited_path and attempts[path] == 1:
            headers = {"Retry-After": "3"}
            if case == "invalid_json":
                return httpx.Response(429, content=b"invalid-json", headers=headers)
            return httpx.Response(429, json=errors[case], headers=headers)
        return _success_response(path)

    hits = await _search_both(httpx.MockTransport(respond), sleep=sleep)

    assert delays == [3.0]
    sibling = _AGENTIC if limited_path == _WEB else _WEB
    assert attempts == {limited_path: 2, sibling: 1}
    assert [hit.title for hit in hits] == ["Web title", "Agentic title"]


@pytest.mark.parametrize("failed_path", [_WEB, _AGENTIC])
@pytest.mark.parametrize("failure", [408, 500, 503, httpx.ConnectError, httpx.ReadTimeout])
async def test_baiduqianfan_uses_existing_transport_and_status_retry_policy(
    failed_path: str, failure: int | type[httpx.TransportError]
) -> None:
    attempts = dict.fromkeys((_WEB, _AGENTIC), 0)

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        attempts[path] += 1
        if path != failed_path:
            return httpx.Response(403)
        if isinstance(failure, int):
            return httpx.Response(failure)
        raise failure("fixture failure", request=request)

    with pytest.raises(ExecutionFailure, match="both search branches failed"):
        await _search_both(httpx.MockTransport(respond))

    sibling = _AGENTIC if failed_path == _WEB else _WEB
    assert attempts == {failed_path: 2, sibling: 1}


@pytest.mark.parametrize("failed_path", [_WEB, _AGENTIC])
@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422])
async def test_baiduqianfan_non_retryable_http_error_does_not_retry(
    failed_path: str, status: int
) -> None:
    attempts = dict.fromkeys((_WEB, _AGENTIC), 0)

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        attempts[path] += 1
        if path == failed_path:
            return httpx.Response(status)
        return _success_response(path)

    hits = await _search_both(httpx.MockTransport(respond))

    assert attempts == {_WEB: 1, _AGENTIC: 1}
    survivor = "agentic" if failed_path == _WEB else "web"
    assert [hit.url for hit in hits] == [f"https://example.test/{survivor}"]


@pytest.mark.parametrize("first_path", [_AGENTIC, _WEB])
async def test_baiduqianfan_aggregates_two_failures_using_fixed_first_branch(
    first_path: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    first_returned = asyncio.Event()
    returned: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path != first_path:
            await first_returned.wait()
        returned.append(path)
        first_returned.set()
        if path == _WEB:
            return httpx.Response(403, content=b"private-fixture-body")
        return httpx.Response(200, json={"code": "InvalidArgument"})

    async with asyncio.timeout(1):
        with pytest.raises(ExecutionFailure) as caught:
            await _search_both(httpx.MockTransport(respond))

    assert returned == [first_path, _WEB if first_path == _AGENTIC else _AGENTIC]
    exc = caught.value
    assert exc.code == ErrorCode.ALL_PROVIDERS_FAILED
    assert exc.message == "baiduqianfan/search: both search branches failed"
    assert exc.reason == "execution_failure"
    assert exc.diagnostics["http_status"] == 403
    assert "upstream_code" not in exc.diagnostics
    assert "private-fixture-body" not in f"{exc.message} {exc.diagnostics!r}"
    assert isinstance(exc.__cause__, ExecutionFailure)
    assert exc.__cause__.diagnostics["http_status"] == 403
    assert not any(
        getattr(record, "gateway_event", None)
        in {"provider_partial_failure", "provider_completed", "provider_failed"}
        for record in caplog.records
    )
