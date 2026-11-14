import json
from pathlib import Path

import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.observability import SecretValue
from agent_search_gateway.providers.academic.unpaywall import UnpaywallResolver
from agent_search_gateway.providers.http import HttpStatusFailure
from tests.support.http import RecordingJsonExecutor

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "academic" / "unpaywall"


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


async def test_unpaywall_maps_best_pdf_and_contact_param() -> None:
    executor = RecordingJsonExecutor([_fixture("oa.json")])
    resolver = UnpaywallResolver(
        executor,
        contact_email=SecretValue("[REDACTED_SECRET]"),
    )
    resolved = await resolver.resolve("https://doi.org/10.1000/UNPAYWALL.EXAMPLE")

    assert resolved is not None
    assert str(resolved.landing_url) == "https://repository.example/article"
    assert str(resolved.pdf_url) == "https://repository.example/article.pdf"
    assert resolved.is_open_access is True
    assert resolved.oa_status == "gold"
    assert resolved.license == "cc-by"
    request = executor.requests[0]
    assert request.url == "https://api.unpaywall.org/v2/10.1000%2Funpaywall.example"
    assert request.params == {"email": "[REDACTED_SECRET]"}


async def test_unpaywall_percent_encodes_reserved_doi_path_characters() -> None:
    executor = RecordingJsonExecutor([_fixture("non_oa.json")])
    resolver = UnpaywallResolver(
        executor,
        contact_email=SecretValue("[REDACTED_SECRET]"),
    )

    await resolver.resolve("10.1000/example#part?query")

    request = executor.requests[0]
    assert request.url == "https://api.unpaywall.org/v2/10.1000%2Fexample%23part%3Fquery"
    assert request.params == {"email": "[REDACTED_SECRET]"}


async def test_unpaywall_handles_landing_only_and_deterministic_alternate_fallback() -> None:
    landing_only = {
        "is_oa": True,
        "oa_status": "green",
        "best_oa_location": {
            "url_for_landing_page": "https://best.example/landing",
            "url_for_pdf": None,
            "license": "cc-by",
        },
        "oa_locations": [],
    }
    alternate = {
        "is_oa": True,
        "oa_status": "green",
        "best_oa_location": None,
        "oa_locations": [
            {"url_for_pdf": "https://z.example/paper.pdf", "url": "https://z.example"},
            {"url_for_pdf": "https://a.example/paper.pdf", "url": "https://a.example"},
        ],
    }
    executor = RecordingJsonExecutor([landing_only, alternate])
    resolver = UnpaywallResolver(
        executor,
        contact_email=SecretValue("[REDACTED_SECRET]"),
    )
    first = await resolver.resolve("10.1000/landing")
    second = await resolver.resolve("10.1000/alternate")
    assert first is not None and first.pdf_url is None
    assert str(first.landing_url) == "https://best.example/landing"
    assert second is not None
    assert str(second.pdf_url) == "https://a.example/paper.pdf"
    assert str(second.landing_url) == "https://a.example"


async def test_unpaywall_skips_invalid_best_location_for_valid_alternate() -> None:
    payload = {
        "is_oa": True,
        "oa_status": "green",
        "best_oa_location": {
            "url_for_landing_page": "not-a-url",
            "url_for_pdf": "ftp://invalid.example/paper.pdf",
        },
        "oa_locations": [
            {
                "url_for_landing_page": "https://valid.example/landing",
                "url_for_pdf": "https://valid.example/paper.pdf",
                "license": "cc-by",
            }
        ],
    }
    executor = RecordingJsonExecutor([payload])
    resolver = UnpaywallResolver(
        executor,
        contact_email=SecretValue("[REDACTED_SECRET]"),
    )

    resolved = await resolver.resolve("10.1000/alternate-valid")

    assert resolved is not None
    assert str(resolved.landing_url) == "https://valid.example/landing"
    assert str(resolved.pdf_url) == "https://valid.example/paper.pdf"
    assert resolved.license == "cc-by"


async def test_unpaywall_uses_normalized_landing_fallback_and_pdf_ranking() -> None:
    landing_fallback = {
        "is_oa": True,
        "oa_status": "green",
        "best_oa_location": {
            "url_for_landing_page": "not-a-url",
            "url": "https://best.example/generic",
            "url_for_pdf": None,
        },
        "oa_locations": [],
    }
    ranked_alternates = {
        "is_oa": True,
        "oa_status": "green",
        "best_oa_location": None,
        "oa_locations": [
            {
                "url_for_pdf": "ftp://invalid.example/paper.pdf",
                "url": "https://a.example/landing",
            },
            {
                "url_for_pdf": "https://z.example/paper.pdf",
                "url": "https://z.example/landing",
            },
        ],
    }
    executor = RecordingJsonExecutor([landing_fallback, ranked_alternates])
    resolver = UnpaywallResolver(
        executor,
        contact_email=SecretValue("[REDACTED_SECRET]"),
    )

    first = await resolver.resolve("10.1000/landing-fallback")
    second = await resolver.resolve("10.1000/pdf-ranking")

    assert first is not None
    assert str(first.landing_url) == "https://best.example/generic"
    assert first.pdf_url is None
    assert second is not None
    assert str(second.landing_url) == "https://z.example/landing"
    assert str(second.pdf_url) == "https://z.example/paper.pdf"


async def test_unpaywall_non_oa_and_404_are_normal_results() -> None:
    executor = RecordingJsonExecutor(
        [
            _fixture("non_oa.json"),
            HttpStatusFailure("unpaywall", "oa_resolve", 404),
        ]
    )
    resolver = UnpaywallResolver(
        executor,
        contact_email=SecretValue("[REDACTED_SECRET]"),
    )
    closed = await resolver.resolve("10.1000/closed.example")
    assert closed is not None
    assert closed.is_open_access is False
    assert closed.landing_url is None
    assert closed.pdf_url is None
    assert await resolver.resolve("10.1000/missing") is None


async def test_unpaywall_non_404_and_protocol_failures_propagate() -> None:
    executor = RecordingJsonExecutor(
        [
            HttpStatusFailure("unpaywall", "oa_resolve", 503),
            _fixture("malformed.json"),
        ]
    )
    resolver = UnpaywallResolver(
        executor,
        contact_email=SecretValue("[REDACTED_SECRET]"),
    )
    with pytest.raises(ExecutionFailure):
        await resolver.resolve("10.1000/status")
    with pytest.raises(ProtocolFailure) as caught:
        await resolver.resolve("10.1000/malformed")
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR


async def test_unpaywall_response_diagnostics_classify_normal_404(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor
    from agent_search_gateway.providers.http import HttpJsonExecutor

    caplog.set_level(logging.DEBUG)
    response = httpx.Response(404, text="PRIVATE_BODY", headers={"content-type": "text/plain"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        resolver = UnpaywallResolver(
            HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="unpaywall"),
            contact_email=SecretValue("PRIVATE_EMAIL@example.test"),
        )
        assert await resolver.resolve("10.1000/missing") is None
    records = [record for record in caplog.records if getattr(record, "gateway_event", None)]
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in records
        if getattr(record, "gateway_event", "") == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["semantic_result"] == "no_match"
    assert diagnostics[0]["http_status"] == 404
    assert diagnostics[0]["response_bytes"] == len(response.content)
    assert not any(
        getattr(record, "gateway_event", "")
        in {"provider_started", "provider_failed", "provider_completed"}
        for record in records
    )
    formatter = KeyValueFormatter(SecretRedactor([]))
    rendered = "\n".join(formatter.format(record) for record in records)
    assert "PRIVATE_" not in rendered
    assert "email=" not in rendered


@pytest.mark.parametrize("status", [200, 201])
async def test_unpaywall_response_diagnostics_count_location_skips_without_urls(
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    import logging

    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor
    from agent_search_gateway.providers.http import HttpJsonExecutor

    caplog.set_level(logging.DEBUG)
    response = httpx.Response(
        status,
        json={
            "is_oa": True,
            "oa_status": "green",
            "best_oa_location": {"url": "ftp://PRIVATE_BAD_URL/paper"},
            "oa_locations": [
                None,
                {"license": "PRIVATE_LICENSE"},
                {"url_for_pdf": "https://repository.test/PRIVATE_VALID_URL.pdf"},
            ],
            "error": {"message": "PRIVATE_CANDIDATE_ERROR"},
        },
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        resolver = UnpaywallResolver(
            HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="unpaywall"),
            contact_email=SecretValue("PRIVATE_EMAIL@example.test"),
        )
        result = await resolver.resolve("10.1000/locations")
    assert result is not None
    assert str(result.pdf_url) == "https://repository.test/PRIVATE_VALID_URL.pdf"
    records = [record for record in caplog.records if getattr(record, "gateway_event", None)]
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in records
        if getattr(record, "gateway_event", "") == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    fields = diagnostics[0]
    assert fields["semantic_result"] == "success"
    assert fields["phase"] == "adapter"
    assert fields["received"] == 4
    assert fields["accepted"] == 1
    assert fields["rejected"] == 3
    assert fields["rejection_counts"] == {
        "invalid_url": 1,
        "invalid_record_shape": 1,
        "missing_url": 1,
    }
    assert fields["http_status"] == status
    assert fields["response_bytes"] == len(response.content)
    assert not any(name.startswith("upstream_") for name in fields)
    formatter = KeyValueFormatter(SecretRedactor([]))
    assert "PRIVATE_" not in "\n".join(formatter.format(record) for record in records)


@pytest.mark.parametrize(
    "payload",
    [
        {"error": {"message": "PRIVATE_ENVELOPE"}},
        {"is_oa": True, "best_oa_location": "PRIVATE_LOCATION"},
        {"is_oa": True, "oa_locations": "PRIVATE_LOCATIONS"},
    ],
)
async def test_unpaywall_invalid_envelope_has_safe_diagnostics(
    caplog: pytest.LogCaptureFixture,
    payload: object,
) -> None:
    import logging

    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor
    from agent_search_gateway.providers.http import HttpJsonExecutor

    caplog.set_level(logging.DEBUG)
    response = httpx.Response(200, json=payload)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        resolver = UnpaywallResolver(
            HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="unpaywall"),
            contact_email=SecretValue("PRIVATE_EMAIL@example.test"),
        )
        with pytest.raises(ProtocolFailure) as caught:
            await resolver.resolve("10.1000/invalid")
    assert caught.value.diagnostics["category"] == "invalid_envelope"
    records = [record for record in caplog.records if getattr(record, "gateway_event", None)]
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in records
        if getattr(record, "gateway_event", "") == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["category"] == "invalid_envelope"
    assert diagnostics[0]["semantic_result"] == "unknown"
    formatter = KeyValueFormatter(SecretRedactor([]))
    assert "PRIVATE_" not in "\n".join(formatter.format(record) for record in records)


async def test_unpaywall_invalid_json_has_decode_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    import httpx

    from agent_search_gateway.models import RetryPolicy
    from agent_search_gateway.providers.http import HttpJsonExecutor

    caplog.set_level(logging.DEBUG)
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"PRIVATE_INVALID_JSON")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        resolver = UnpaywallResolver(
            HttpJsonExecutor(client, RetryPolicy(2, 0, 0, 5), provider_name="unpaywall"),
            contact_email=SecretValue("PRIVATE_EMAIL@example.test"),
        )
        with pytest.raises(ProtocolFailure) as caught:
            await resolver.resolve("10.1000/decode")
    assert calls == 1
    assert caught.value.diagnostics["category"] == "decode"
    diagnostics = [
        vars(record)["gateway_fields"]
        for record in caplog.records
        if getattr(record, "gateway_event", "") == "provider_response_diagnostic"
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["category"] == "decode"
    assert diagnostics[0]["semantic_result"] == "unknown"
