from datetime import date

import pytest

from agent_search_gateway.errors import ParserFailure
from agent_search_gateway.paper_search_parser import parse_paper_markdown

VALID_BLOCK = """## Paper
Title: Example Paper
Authors: Alice Example; Bob Example
Abstract: Example abstract.
DOI: 10.1000/EXAMPLE
arXiv: 2401.12345v2
Published: 2024-01-02
Updated: 2024-02-03
URL: https://EXAMPLE.com/paper
PDF: https://example.com/paper.pdf
Venue: ExampleConf
Topics: Machine Learning; AI
Citations: 17
Open Access: true
OA Status: gold
License: cc-by"""


def test_parse_one_and_repeated_strict_paper_blocks() -> None:
    hits = parse_paper_markdown(VALID_BLOCK, provider="provider-a")
    assert len(hits) == 1
    hit = hits[0]
    assert hit.source == "llm:provider-a"
    assert hit.source_id == "https://example.com/paper"
    assert hit.title == "Example Paper"
    assert hit.authors == ("Alice Example", "Bob Example")
    assert hit.abstract == "Example abstract."
    assert hit.doi == "10.1000/EXAMPLE"
    assert hit.arxiv_id == "2401.12345v2"
    assert hit.published_date == date(2024, 1, 2)
    assert hit.updated_date == date(2024, 2, 3)
    assert hit.url == "https://example.com/paper"
    assert hit.pdf_url == "https://example.com/paper.pdf"
    assert hit.venue == "ExampleConf"
    assert hit.topics == ("Machine Learning", "AI")
    assert hit.citation_count == 17
    assert hit.is_open_access is True
    assert hit.oa_status == "gold"
    assert hit.license == "cc-by"

    repeated = parse_paper_markdown(f"{VALID_BLOCK}\n{VALID_BLOCK}", provider="provider-a")
    assert len(repeated) == 2


def test_optional_values_may_be_empty_without_permissive_guessing() -> None:
    block = """## Paper
Title: Metadata Only
Authors:
Abstract:
DOI:
arXiv:
Published:
Updated:
URL: https://example.com/metadata
PDF:
Venue:
Topics:
Citations:
Open Access: unknown
OA Status:
License:"""
    hit = parse_paper_markdown(block, provider="provider-b")[0]
    assert hit.authors == ()
    assert hit.abstract == ""
    assert hit.doi == ""
    assert hit.arxiv_id == ""
    assert hit.published_date is None
    assert hit.updated_date is None
    assert hit.pdf_url == ""
    assert hit.topics == ()
    assert hit.citation_count is None
    assert hit.is_open_access is None


@pytest.mark.parametrize(
    "markdown",
    [
        VALID_BLOCK.replace("License: cc-by", ""),
        VALID_BLOCK.replace("Title: Example Paper", "Title:"),
        VALID_BLOCK.replace("URL: https://EXAMPLE.com/paper", "URL: ftp://example.com/paper"),
        VALID_BLOCK.replace("Published: 2024-01-02", "Published: 2024/01/02"),
        VALID_BLOCK.replace("Citations: 17", "Citations: -1"),
        VALID_BLOCK.replace("Open Access: true", "Open Access: yes"),
        VALID_BLOCK.replace("Title: Example Paper", "Title: Example Paper\nExtra: nope"),
        "## Result\nURL: https://example.com\nAbstract: result",
        "## Result\nURL: https://example.com\nAbstract: result\n" + VALID_BLOCK,
        VALID_BLOCK + "\nFree-form trailing markdown",
    ],
)
def test_parser_rejects_missing_invalid_extra_web_and_mixed_formats(markdown: str) -> None:
    with pytest.raises(ParserFailure):
        parse_paper_markdown(markdown, provider="provider-a")


def test_parser_errors_do_not_include_raw_model_payload() -> None:
    sentinel = "RAW_MODEL_PAYLOAD_SENTINEL"
    malformed = VALID_BLOCK.replace("Citations: 17", f"Citations: {sentinel}")
    with pytest.raises(ParserFailure) as caught:
        parse_paper_markdown(malformed, provider="provider-a")
    assert sentinel not in caught.value.message


def test_paper_parser_tags_agent_source_when_requested() -> None:
    import inspect
    from dataclasses import replace

    assert "source_kind" in inspect.signature(parse_paper_markdown).parameters
    llm = parse_paper_markdown(VALID_BLOCK, provider="provider-a")[0]
    agent = parse_paper_markdown(VALID_BLOCK, provider="provider-a", source_kind="agent")[0]
    assert llm.source == "llm:provider-a"
    assert agent.source == "agent:provider-a"
    assert replace(agent, source=llm.source) == llm
