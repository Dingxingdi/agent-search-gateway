from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest

from agent_search_gateway.errors import ErrorCode, ExecutionFailure, ProtocolFailure
from agent_search_gateway.models import RetryPolicy
from agent_search_gateway.providers.academic.arxiv import ArxivProvider
from agent_search_gateway.providers.http import HttpJsonExecutor
from tests.support.academic_diagnostics import (
    academic_response,
    assert_candidate_counts,
    assert_private_payload_absent,
    assert_response_classification,
)
from tests.support.http import RecordingJsonExecutor

FIXTURES = Path(__file__).parents[2] / "fixtures" / "providers" / "academic" / "arxiv"


_DEFAULT_FIELDS = ("ti", "abs")
_GRAPH_TRANSFORMERS = "(ti:graph OR abs:graph) AND (ti:transformers OR abs:transformers)"


@pytest.mark.parametrize(
    ("query", "fields", "expected"),
    [
        ("graph", _DEFAULT_FIELDS, "(ti:graph OR abs:graph)"),
        ("graph transformers", _DEFAULT_FIELDS, _GRAPH_TRANSFORMERS),
        (
            '"graph neural networks"',
            _DEFAULT_FIELDS,
            '(ti:"graph neural networks" OR abs:"graph neural networks")',
        ),
        (
            '"graph neural networks" molecular',
            _DEFAULT_FIELDS,
            '(ti:"graph neural networks" OR abs:"graph neural networks")'
            " AND (ti:molecular OR abs:molecular)",
        ),
        ("graph transformers", ("ti",), "(ti:graph) AND (ti:transformers)"),
        ('"graph   neural"  molecular', ("abs",), '(abs:"graph   neural") AND (abs:molecular)'),
        (
            " \t图谱\nstate-of-the-art ",
            ("abs", "ti"),
            "(abs:图谱 OR ti:图谱) AND (abs:state-of-the-art OR ti:state-of-the-art)",
        ),
        ('"graph\tmodel"', ("ti",), '(ti:"graph\tmodel")'),
        ("graph\x0b\x0c\x1ftransformers", _DEFAULT_FIELDS, _GRAPH_TRANSFORMERS),
        ("图谱🪐", ("ti",), "(ti:图谱🪐)"),
        ('"graph \\alpha model"', ("ti",), '(ti:"graph \\alpha model")'),
        (r"TeX\alpha", ("ti",), r"(ti:TeX\alpha)"),
        ("end\\", ("ti",), "(ti:end\\)"),
        (" \\\\ ", ("ti",), "(ti:\\\\)"),
        ('"graph model\\"', ("ti",), '(ti:"graph model\\ ")'),
        ("au:x\\\\", ("abs",), '(abs:"au:x\\\\ ")'),
        ("(q)\\", ("ti",), '(ti:"(q)\\ ")'),
        ("AND OR ANDNOT NOT", ("ti",), '(ti:"AND") AND (ti:"OR") AND (ti:"ANDNOT") AND (ti:"NOT")'),
        ("au:example", _DEFAULT_FIELDS, '(ti:"au:example" OR abs:"au:example")'),
        ("(graph)", ("abs",), '(abs:"(graph)")'),
        ("50% %22", ("abs",), '(abs:"50%") AND (abs:"%22")'),
        ("graph?", _DEFAULT_FIELDS, '(ti:"graph?" OR abs:"graph?")'),
        ("A*", ("ti",), '(ti:"A*")'),
        ("a?b", ("abs",), '(abs:"a?b")'),
        ("a*b", ("ti",), '(ti:"a*b")'),
        ("&&", ("ti",), '(ti:"&&")'),
        ("||", ("abs",), '(abs:"||")'),
        ("a&&b", ("ti",), '(ti:"a&&b")'),
        ("a||b", ("abs",), '(abs:"a||b")'),
        ('"graph? A* a&&b a||b"', ("ti",), '(ti:"graph? A* a&&b a||b")'),
        ("graph?\\", ("ti",), '(ti:"graph?\\ ")'),
        ("a&&b\\", ("abs",), '(abs:"a&&b\\ ")'),
        ('"submittedDate:[2020 to 2021]"', ("ti",), '(ti:"submittedDate:[2020 to 2021]")'),
        ('"submittedDate:[20 TO 21]"', ("ti",), '(ti:"submittedDate:[20 TO 21]")'),
        ("LastUpdatedDate:example", ("ti",), '(ti:"LastUpdatedDate:example")'),
        ('"" graph ""', _DEFAULT_FIELDS, "(ti:graph OR abs:graph)"),
    ],
)
async def test_arxiv_query_compiler(query: str, fields: tuple[str, ...], expected: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="arxiv")
        await ArxivProvider(executor, query_fields=fields).search(query)

    [request] = requests
    assert request.method == "GET"
    assert request.url.params["search_query"] == expected


@pytest.mark.parametrize("query", ["", "\t \n", '""', ' "" \t "" '])
async def test_arxiv_query_compiler_no_searchable_terms(query: str) -> None:
    executor = RecordingJsonExecutor([])
    with pytest.raises(ProtocolFailure) as caught:
        await ArxivProvider(executor).search(query)
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR
    assert executor.requests == []


@pytest.mark.parametrize(
    "query",
    [
        'a"b',
        'a""b',
        '"open',
        'end"',
        r"a\"b",
        '""open',
        '"graph""model"',
        '" edge"',
        '"edge\t"',
        '"line\nbreak"',
        '"carriage\rreturn"',
        "lastUpdatedDate:example",
        '"prefixlastUpdatedDate:suffix"',
        '"submittedDate:[2020 TO 2021]"',
        '"prefix submittedDate:[20-0 TO ２０２１] suffix"',
        "unpaired\ud800",
        "graph\x00model",
        "graph\x01model",
        "graph\x08model",
        '"graph\x0bmodel"',
        '"graph\x0cmodel"',
        "graph\x0emodel",
        '"graph\x1fmodel"',
        "graph\ufffemodel",
        '"graph\uffffmodel"',
    ],
)
async def test_arxiv_unrepresentable_literal_fails_before_http(query: str) -> None:
    executor = RecordingJsonExecutor([])
    with pytest.raises(ProtocolFailure) as caught:
        await ArxivProvider(executor).search(query)
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR
    assert caught.value.message == "arxiv/paper_search: query contains an unrepresentable literal"
    assert executor.requests == []


async def test_arxiv_rejects_oversized_encoded_query_before_transport() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>')

    query = " ".join(["a", "b", "c"] * 900)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="arxiv")
        with pytest.raises(ProtocolFailure) as caught:
            await ArxivProvider(executor).search(query)

    assert caught.value.code is ErrorCode.PROTOCOL_ERROR
    assert caught.value.message == "arxiv/paper_search: request URL could not be constructed"
    assert requests == []


@pytest.mark.parametrize("empty_response", [False, True])
async def test_arxiv_uses_configured_search_options(empty_response: bool) -> None:
    requests: list[httpx.Request] = []
    body = (
        '<feed xmlns="http://www.w3.org/2005/Atom"/>'
        if empty_response
        else (FIXTURES / "search.xml").read_text()
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(client, RetryPolicy(1, 0, 0, 5), provider_name="arxiv")
        provider = ArxivProvider(
            executor,
            query_fields=["abs"],
            max_results=25,
            sort_by="submittedDate",
            sort_order="ascending",
        )
        hits = await provider.search('图谱 "neural networks" 50% %22')

    assert len(hits) == (0 if empty_response else 1)
    [request] = requests
    assert request.method == "GET"
    assert request.url.path == "/api/query"
    expected = {
        "search_query": '(abs:图谱) AND (abs:"neural networks") AND (abs:"50%") AND (abs:"%22")',
        "max_results": "25",
        "sortBy": "submittedDate",
        "sortOrder": "ascending",
    }
    assert dict(request.url.params) == expected
    assert request.url.query == urlencode(expected).encode("ascii")


async def test_arxiv_preserves_upstream_results_without_client_sort_or_truncation() -> None:
    body = """<feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>https://arxiv.org/abs/2402.00002</id><title>Z first</title>
        <published>2024-02-02</published></entry>
      <entry><id>https://arxiv.org/abs/2401.00001</id><title>A second</title>
        <published>2024-01-01</published></entry>
    </feed>"""
    executor = RecordingJsonExecutor([body])

    hits = await ArxivProvider(
        executor, max_results=1, sort_by="submittedDate", sort_order="ascending"
    ).search("graph")

    assert [hit.source_id for hit in hits] == ["2402.00002", "2401.00001"]
    assert [hit.title for hit in hits] == ["Z first", "A second"]
    [request] = executor.requests
    assert request.params is not None
    assert request.params["max_results"] == 1


@pytest.mark.parametrize("custom_options", [False, True])
async def test_runtime_arxiv_options_reach_real_http_request(
    tmp_path: Path, custom_options: bool
) -> None:
    from agent_search_gateway.config import resolve_config
    from agent_search_gateway.paths import RuntimePaths
    from agent_search_gateway.providers.academic.defaults import (
        build_default_academic_registry,
        build_default_oa_resolver_registry,
    )
    from agent_search_gateway.providers.defaults import build_default_registry
    from agent_search_gateway.runtime import Runtime
    from tests.runtime.test_runtime_assembly import _config, _environment

    web_registry = build_default_registry()
    academic_registry = build_default_academic_registry()
    resolver_registry = build_default_oa_resolver_registry()
    raw = _config()
    raw["web_providers"] = {}
    options: dict[str, object] = (
        {
            "query_fields": ["abs"],
            "max_results": 25,
            "sort_by": "submittedDate",
            "sort_order": "ascending",
        }
        if custom_options
        else {}
    )
    raw["academic_providers"] = {
        "arxiv": {
            "enabled": True,
            "credentials": [{"api_url": "https://arxiv.example.test/query"}],
            **options,
        }
    }
    resolved = resolve_config(
        raw,
        web_registry,
        _environment(),
        academic_registry=academic_registry,
        oa_resolver_registry=resolver_registry,
    )
    requests: list[httpx.Request] = []
    body = (FIXTURES / "search.xml").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=body)

    runtime = await Runtime.build(
        resolved,
        RuntimePaths.from_home(tmp_path),
        registry=web_registry,
        academic_registry=academic_registry,
        oa_resolver_registry=resolver_registry,
        http_client_factory=lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        [provider] = runtime.academic_search_providers
        assert provider.name == "arxiv"
        hits = await provider.search('图谱 "neural networks" 50% %22')
    finally:
        await runtime.aclose()

    assert [hit.source_id for hit in hits] == ["2401.12345"]
    [request] = requests
    assert request.method == "GET"
    assert request.url.host == "arxiv.example.test"
    assert request.url.path == "/query"
    expected = {
        "search_query": (
            '(abs:图谱) AND (abs:"neural networks") AND (abs:"50%") AND (abs:"%22")'
            if custom_options
            else '(ti:图谱 OR abs:图谱) AND (ti:"neural networks" OR abs:"neural networks")'
            ' AND (ti:"50%" OR abs:"50%") AND (ti:"%22" OR abs:"%22")'
        ),
        "max_results": "25" if custom_options else "10",
        "sortBy": "submittedDate" if custom_options else "relevance",
        "sortOrder": "ascending" if custom_options else "descending",
    }
    assert dict(request.url.params) == expected
    assert request.url.query == urlencode(expected).encode("ascii")


async def test_arxiv_honors_shared_retry_after() -> None:

    requests: list[httpx.Request] = []
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "5"}, request=request)
        return httpx.Response(200, text=(FIXTURES / "search.xml").read_text(), request=request)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = HttpJsonExecutor(
            client,
            RetryPolicy(2, 0.25, 2.0, 30.0),
            provider_name="arxiv",
            sleep=sleep,
        )
        hits = await ArxivProvider(executor).search("graph transformers")

    assert len(requests) == 2
    assert requests[0].url == requests[1].url
    assert requests[0].url.params["search_query"] == _GRAPH_TRANSFORMERS
    assert delays == [5.0]
    assert len(hits) == 1
    assert hits[0].arxiv_id == "2401.12345"
    assert hits[0].title == "Example arXiv Paper"
    assert hits[0].authors == ("Alice Example", "Bob Example")


async def test_arxiv_maps_atom_feed_and_isolates_malformed_entry() -> None:
    executor = RecordingJsonExecutor([(FIXTURES / "search.xml").read_text()])
    provider = ArxivProvider(executor)

    hits = await provider.search("graph transformers")

    assert len(hits) == 1
    hit = hits[0]
    assert hit.source == "arxiv"
    assert hit.source_id == "2401.12345"
    assert hit.arxiv_id == "2401.12345"
    assert hit.title == "Example arXiv Paper"
    assert hit.authors == ("Alice Example", "Bob Example")
    assert hit.abstract == "Example abstract."
    assert hit.doi == "10.1000/arxiv.example"
    assert hit.published_date == date(2024, 1, 2)
    assert hit.updated_date == date(2024, 2, 3)
    assert hit.topics == ("cs.AI", "cs.LG")
    assert hit.url == "https://arxiv.org/abs/2401.12345v2"
    assert hit.pdf_url == "https://arxiv.org/pdf/2401.12345v2.pdf"

    [request] = executor.requests
    assert request.method == "GET"
    assert request.stage == "paper_search"
    assert request.response_mode == "text"
    assert request.headers is None
    assert request.params == {
        "search_query": _GRAPH_TRANSFORMERS,
        "max_results": 10,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }


async def test_arxiv_malformed_xml_envelope_is_protocol_failure() -> None:
    executor = RecordingJsonExecutor([(FIXTURES / "malformed.xml").read_text()])
    with pytest.raises(ProtocolFailure) as caught:
        await ArxivProvider(executor).search("query")
    assert caught.value.code is ErrorCode.PROTOCOL_ERROR


async def test_arxiv_http_failure_propagates_without_adapter_retry() -> None:
    failure = ExecutionFailure(ErrorCode.ALL_PROVIDERS_FAILED, "transport")
    executor = RecordingJsonExecutor([failure])
    with pytest.raises(ExecutionFailure):
        await ArxivProvider(executor).search("query")
    assert len(executor.requests) == 1


@pytest.mark.parametrize(
    "case", ["valid", "mixed", "all_invalid", "empty", "decode", "wrong_root", "wrong_namespace"]
)
async def test_xml_provider_reports_response_and_candidate_diagnostics(
    caplog: pytest.LogCaptureFixture, case: str
) -> None:
    import logging
    import xml.etree.ElementTree as ET

    caplog.set_level(logging.DEBUG)
    body = (
        (FIXTURES / "search.xml").read_text().replace("Example abstract.", "PRIVATE_XML_BODY café")
    )
    root = ET.fromstring(body)
    entries = root.findall("{http://www.w3.org/2005/Atom}entry")
    if case == "valid":
        root.remove(entries[1])
    elif case == "all_invalid":
        root.remove(entries[0])
    elif case == "empty":
        for entry in entries:
            root.remove(entry)
    body = ET.tostring(root, encoding="unicode")
    if case == "decode":
        body = (FIXTURES / "malformed.xml").read_text() + "PRIVATE_XML_BODY"
    elif case == "wrong_root":
        body = "<html><body>PRIVATE_HTML_BODY captcha validation</body></html>"
    elif case == "wrong_namespace":
        body = '<feed xmlns="urn:wrong"><entry>PRIVATE_XML_BODY</entry></feed>'
    invalid_envelope = case in ("decode", "wrong_root", "wrong_namespace")
    received = {"valid": 1, "mixed": 2, "all_invalid": 1, "empty": 0}.get(case, 0)
    accepted = int(case in ("valid", "mixed"))
    rejected = received - accepted
    response = httpx.Response(
        200,
        content=body.encode("utf-16"),
        headers={"content-type": "application/atom+xml; charset=utf-16"},
    )
    async with academic_response("arxiv", response=response) as executor:
        provider = ArxivProvider(executor)
        if invalid_envelope:
            with pytest.raises(ProtocolFailure) as caught:
                await provider.search("query")
            assert caught.value.code is ErrorCode.PROTOCOL_ERROR
            assert caught.value.message == (
                "arxiv/paper_search: response was not valid Atom XML"
                if case == "decode"
                else "arxiv/paper_search: response Atom envelope was invalid"
            )
        else:
            assert len(await provider.search("query")) == accepted

    records = assert_response_classification(
        caplog,
        response,
        case,
        response_kind="unknown" if case == "decode" else "xml",
        invalid_envelope=invalid_envelope,
        content_type="application/atom+xml",
    )
    assert_candidate_counts(
        records,
        "arxiv",
        received,
        accepted,
        {"missing_required_field": rejected} if rejected else {},
        summary_expected=not invalid_envelope,
    )
    assert_private_payload_absent(records)


@pytest.mark.parametrize("custom_options", [False, True])
async def test_arxiv_registry_constructs_resolved_search_options(custom_options: bool) -> None:
    from agent_search_gateway.config import resolve_academic_provider_config
    from agent_search_gateway.providers.academic.defaults import build_default_academic_registry

    registry = build_default_academic_registry()
    options: dict[str, object] = (
        {
            "api_url": "https://arxiv.example.test/query",
            "query_fields": ["abs", "ti"],
            "max_results": 100,
            "sort_by": "lastUpdatedDate",
            "sort_order": "ascending",
        }
        if custom_options
        else {}
    )
    [config] = resolve_academic_provider_config(
        {"academic_providers": {"arxiv": {"enabled": True, **options}}}, registry, {}
    ).providers
    executor = RecordingJsonExecutor([(FIXTURES / "search.xml").read_text()])
    provider = registry.require("arxiv").factory(executor=executor, **config.options)
    assert isinstance(provider, ArxivProvider)
    assert provider._query_fields == (("abs", "ti") if custom_options else ("ti", "abs"))
    assert provider._max_results == (100 if custom_options else 10)
    assert provider._sort_by == ("lastUpdatedDate" if custom_options else "relevance")
    assert provider._sort_order == ("ascending" if custom_options else "descending")

    assert len(await provider.search("graph transformers")) == 1
    request = executor.requests[0]
    assert request.url == (
        "https://arxiv.example.test/query"
        if custom_options
        else "https://export.arxiv.org/api/query"
    )
    assert request.params == {
        "search_query": (
            "(abs:graph OR ti:graph) AND (abs:transformers OR ti:transformers)"
            if custom_options
            else _GRAPH_TRANSFORMERS
        ),
        "max_results": 100 if custom_options else 10,
        "sortBy": "lastUpdatedDate" if custom_options else "relevance",
        "sortOrder": "ascending" if custom_options else "descending",
    }


@pytest.mark.parametrize("enabled", [False, True])
def test_arxiv_resolved_options_snapshot_lists_before_factory(enabled: bool) -> None:
    from agent_search_gateway.config import resolve_academic_provider_config
    from agent_search_gateway.providers.academic.defaults import build_default_academic_registry

    fields = ["abs", "ti"]
    table = {"enabled": enabled, "query_fields": fields}
    registry = build_default_academic_registry()
    [config] = resolve_academic_provider_config(
        {"academic_providers": {"arxiv": table}}, registry, {}
    ).providers
    assert table["query_fields"] == ["abs", "ti"]
    fields.clear()

    provider = registry.require("arxiv").factory(
        executor=RecordingJsonExecutor([]), **config.options
    )

    assert isinstance(provider, ArxivProvider)
    assert provider._query_fields == ("abs", "ti")


def test_arxiv_constructor_snapshots_mutable_query_fields() -> None:
    fields = ["abs", "ti"]
    provider = ArxivProvider(RecordingJsonExecutor([]), query_fields=fields)
    fields[:] = ["ti"]
    assert provider._query_fields == ("abs", "ti")
