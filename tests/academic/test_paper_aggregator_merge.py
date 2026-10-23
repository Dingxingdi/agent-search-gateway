from datetime import date
from itertools import permutations

from agent_search_gateway.academic.aggregator import PaperAggregator
from agent_search_gateway.providers.contracts import PaperSearchHit

PRIORITY = ("arxiv", "semantic_scholar", "openalex", "dblp", "crossref", "core")
DOI = "10.1000/merged"


def _candidates() -> tuple[PaperSearchHit, ...]:
    return (
        PaperSearchHit(
            source="dblp",
            source_id="conf/example/Paper24",
            title="DBLP title",
            authors=("DBLP Author",),
            doi=DOI,
            published_date=date(2024, 1, 1),
            url="https://dblp.org/rec/conf/example/Paper24",
            venue="Example Conference",
        ),
        PaperSearchHit(
            source="arxiv",
            source_id="2401.44444v2",
            title="Canonical arXiv title",
            authors=("ArXiv Author",),
            abstract="Academic abstract from arXiv",
            doi=DOI,
            arxiv_id="2401.44444v2",
            published_date=date(2024, 1, 2),
            updated_date=date(2024, 2, 3),
            url="https://arxiv.org/abs/2401.44444v2",
            pdf_url="https://arxiv.org/pdf/2401.44444v2.pdf",
            topics=("AI",),
        ),
        PaperSearchHit(
            source="openalex",
            source_id="https://openalex.org/W44",
            title="OpenAlex title",
            abstract="OpenAlex abstract",
            doi="https://doi.org/10.1000/MERGED",
            url="https://openalex.org/W44",
            topics=("Machine Learning",),
            citation_count=130,
            is_open_access=True,
            oa_status="gold",
        ),
        PaperSearchHit(
            source="crossref",
            source_id=DOI,
            title="Crossref title",
            doi=DOI,
            url="https://doi.org/10.1000/merged",
            venue="Publisher Journal",
            citation_count=91,
        ),
        PaperSearchHit(
            source="semantic_scholar",
            source_id="ABCDEF44",
            title="Semantic title",
            abstract="Semantic abstract",
            doi=DOI,
            url="https://www.semanticscholar.org/paper/ABCDEF44",
            topics=("ai", "NLP"),
            citation_count=127,
        ),
        PaperSearchHit(
            source="core",
            source_id="core-44",
            title="CORE title",
            doi=DOI,
            url="https://core.ac.uk/works/core-44",
            topics=("Open Access",),
            citation_count=12,
        ),
    )


def test_merge_is_deterministic_across_input_permutations_and_unions_identifiers() -> None:
    aggregator = PaperAggregator(PRIORITY)
    expected = aggregator.aggregate(_candidates())[0]
    for ordered in permutations(_candidates()):
        assert aggregator.aggregate(ordered) == [expected]

    assert expected.title == "Canonical arXiv title"
    assert expected.authors == ("ArXiv Author",)
    assert expected.abstract == "Academic abstract from arXiv"
    assert expected.venue == "Example Conference"
    assert expected.published_date == date(2024, 1, 2)
    assert expected.updated_date == date(2024, 2, 3)
    assert str(expected.pdf_url) == ("https://arxiv.org/pdf/2401.44444v2.pdf")
    assert expected.identifiers.doi == DOI
    assert expected.identifiers.arxiv_id == "2401.44444"
    assert expected.identifiers.semantic_scholar_id == "ABCDEF44"
    assert expected.identifiers.openalex_id == "W44"
    assert expected.identifiers.dblp_key == "conf/example/Paper24"
    assert expected.identifiers.core_id == "core-44"


def test_citations_topics_and_sources_preserve_provenance_with_stable_union() -> None:
    record = PaperAggregator(PRIORITY).aggregate(reversed(_candidates()))[0]
    assert record.citation_counts == {
        "semantic_scholar": 127,
        "openalex": 130,
        "crossref": 91,
        "core": 12,
    }
    assert record.topics == ("AI", "NLP", "Machine Learning", "Open Access")
    assert record.sources == PRIORITY


def test_academic_abstract_and_pdf_outrank_llm_and_invalid_pdf_is_discarded() -> None:
    llm = PaperSearchHit(
        source="llm:paper:test-model",
        source_id="llm-result-1",
        title="LLM title",
        abstract="LLM abstract",
        doi=DOI,
        url="https://example.test/llm",
        pdf_url="not-a-url",
    )
    academic = PaperSearchHit(
        source="openalex",
        source_id="W55",
        title="Academic title",
        abstract="Verified academic abstract",
        doi=DOI,
        url="https://openalex.org/W55",
        pdf_url="https://repository.example/paper.pdf",
    )
    record = PaperAggregator(PRIORITY).aggregate([llm, academic])[0]
    assert record.abstract == "Verified academic abstract"
    assert str(record.pdf_url) == "https://repository.example/paper.pdf"


def test_optional_malformed_metadata_does_not_overwrite_valid_values_or_invent_dates() -> None:
    first = PaperSearchHit(
        source="openalex",
        source_id="W66",
        title="Paper",
        doi="10.1000/optional",
        authors=("Author",),
        url="https://openalex.org/W66",
        citation_count=4,
    )
    malformed_optional = PaperSearchHit(
        source="core",
        source_id="core-66",
        title="Paper",
        doi="10.1000/optional",
        authors=(),
        url="https://core.ac.uk/works/core-66",
        pdf_url="ftp://invalid.example/paper.pdf",
        citation_count=-3,
    )
    record = PaperAggregator(PRIORITY).aggregate([malformed_optional, first])[0]
    assert record.authors == ("Author",)
    assert record.pdf_url is None
    assert record.published_date is None
    assert record.updated_date is None
    assert record.citation_counts == {"openalex": 4}


def test_agent_and_llm_generated_sources_keep_academic_precedence_and_priorities() -> None:
    hits = [
        PaperSearchHit(
            source=source,
            source_id="record",
            title=source,
            abstract=source,
            doi=DOI,
            url=f"https://example.test/{index}",
            citation_count=index,
        )
        for index, source in enumerate(("agent:a", "llm:l", "academic"))
    ]
    aggregator = PaperAggregator(("agent:a", "llm:l", "academic"))
    for ordered in permutations(hits):
        record = aggregator.aggregate(ordered)[0]
        assert record.title == record.abstract == "academic"
        assert record.sources == ("academic", "agent:a", "llm:l")
        assert record.citation_counts == {"academic": 2, "agent:a": 0, "llm:l": 1}
    record = PaperAggregator(("llm:l",)).aggregate(hits[:2])[0]
    assert record.title == "llm:l"
    assert record.sources == ("llm:l", "agent:a")


def test_agent_native_source_ids_remain_namespaced() -> None:
    hits = [
        PaperSearchHit(
            source=source,
            source_id="same-native-id",
            title="Paper",
            url="https://example.test/paper",
        )
        for source in ("agent:a", "agent:b", "llm:a")
    ]
    records = PaperAggregator(("llm:a", "agent:a", "agent:b")).aggregate(hits)
    assert len(records) == 3
    assert [record.sources for record in records] == [("llm:a",), ("agent:a",), ("agent:b",)]
