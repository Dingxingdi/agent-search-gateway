import json
from datetime import date
from pathlib import Path

import pytest

from agent_search_gateway.models import PaperIdentifiers, PaperRecord, SearchRecord
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_normalization import NormalizedURL, normalize_url


def _paper(**overrides: object) -> PaperRecord:
    values: dict[str, object] = {
        "title": "Example Paper",
        "authors": ("A. Author",),
        "abstract": "",
        "identifiers": PaperIdentifiers(
            doi="10.1000/example",
            arxiv_id="2401.12345",
            semantic_scholar_id="ABCDEF12",
            openalex_id="W123",
            dblp_key="conf/example/Paper24",
            core_id="core-1",
        ),
        "published_date": date(2026, 1, 2),
        "updated_date": None,
        "url": normalize_url("https://doi.org/10.1000/example"),
        "pdf_url": normalize_url("https://example.test/paper.pdf"),
        "venue": "ExampleConf",
        "topics": ("Machine Learning",),
        "citation_counts": {"openalex": 12},
        "is_open_access": True,
        "oa_status": "gold",
        "license": "cc-by",
        "sources": ("openalex", "crossref"),
    }
    values.update(overrides)
    return PaperRecord(**values)  # type: ignore[arg-type]


def test_result_writer_uses_exact_request_id_and_public_jsonl_schema(tmp_path: Path) -> None:
    results_dir = tmp_path / "nested" / "results"
    writer = ResultWriter(results_dir)
    records = [
        SearchRecord(normalize_url("https://EXAMPLE.com/a"), "Résumé 日本語"),
        SearchRecord(normalize_url("https://example.com/b"), "Second"),
    ]

    keyword = writer.write_results("keyword", records, request_id="a1b2c3d4")
    empty = writer.write_results("llm", [], request_id="11111111")

    assert keyword.is_absolute()
    assert empty.is_absolute()
    assert keyword.name == "keyword-a1b2c3d4.jsonl"
    assert empty.name == "llm-11111111.jsonl"
    assert empty.read_text(encoding="utf-8") == ""

    lines = keyword.read_text(encoding="utf-8").splitlines()
    assert lines[0] == '{"url":"https://example.com/a","abstract":"Résumé 日本語"}'
    assert [set(json.loads(line)) for line in lines] == [{"url", "abstract"}, {"url", "abstract"}]
    assert all("request_id" not in json.loads(line) for line in lines)


def test_result_writer_never_switches_request_id_on_collision(tmp_path: Path) -> None:
    writer = ResultWriter(tmp_path)
    target = tmp_path / "keyword-a1b2c3d4.jsonl"
    target.write_text("original\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        writer.write_results("keyword", [], request_id="a1b2c3d4")

    assert target.read_text(encoding="utf-8") == "original\n"
    assert list(tmp_path.glob("keyword-*.jsonl")) == [target]


def test_result_writer_serializes_before_creating_target(tmp_path: Path) -> None:
    writer = ResultWriter(tmp_path)

    with pytest.raises(ValueError):
        writer.write_results(
            "keyword",
            [SearchRecord(normalize_url("https://example.com"), " ")],
            request_id="a1b2c3d4",
        )

    assert not (tmp_path / "keyword-a1b2c3d4.jsonl").exists()


def test_result_writer_rejects_invalid_request_id_without_creating_directory(
    tmp_path: Path,
) -> None:
    results_dir = tmp_path / "missing" / "results"
    writer = ResultWriter(results_dir)

    with pytest.raises(ValueError, match="request ID"):
        writer.write_results("keyword", [], request_id="bad")

    assert not results_dir.exists()


def test_paper_writer_uses_explicit_compact_schema_and_paper_filename(tmp_path: Path) -> None:
    writer = ResultWriter(tmp_path)
    target = writer.write_paper_results("paper", [_paper()], request_id="1234abcd")

    assert target.name == "paper-1234abcd.jsonl"
    assert target.read_text(encoding="utf-8") == (
        '{"title":"Example Paper","authors":["A. Author"],"abstract":"",'
        '"identifiers":{"doi":"10.1000/example","arxiv_id":"2401.12345",'
        '"semantic_scholar_id":"ABCDEF12","openalex_id":"W123",'
        '"dblp_key":"conf/example/Paper24","core_id":"core-1"},'
        '"published_date":"2026-01-02","updated_date":null,'
        '"url":"https://doi.org/10.1000/example",'
        '"pdf_url":"https://example.test/paper.pdf","venue":"ExampleConf",'
        '"topics":["Machine Learning"],"citation_counts":{"openalex":12},'
        '"is_open_access":true,"oa_status":"gold","license":"cc-by",'
        '"sources":["openalex","crossref"]}\n'
    )


def test_mixed_writer_adds_type_only_at_sink_and_preserves_order(tmp_path: Path) -> None:
    writer = ResultWriter(tmp_path)
    web = SearchRecord(normalize_url("https://example.test/web"), "Web summary")
    target = writer.write_mixed_results([web], [_paper()], request_id="abcddcba")
    payloads = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]

    assert target.name == "llm-abcddcba.jsonl"
    assert payloads[0] == {
        "type": "web",
        "url": "https://example.test/web",
        "abstract": "Web summary",
    }
    assert payloads[1]["type"] == "paper"
    assert payloads[1]["title"] == "Example Paper"


def test_paper_writer_validates_all_records_before_creating_target(tmp_path: Path) -> None:
    writer = ResultWriter(tmp_path)
    invalid_records = [
        _paper(title=" "),
        _paper(url=NormalizedURL("HTTPS://EXAMPLE.test/not-normalized")),
        _paper(citation_counts={"openalex": -1}),
        _paper(sources=("openalex", "openalex")),
        _paper(published_date="2026-01-02"),
    ]
    for index, invalid in enumerate(invalid_records):
        request_id = f"0000000{index}"
        with pytest.raises(ValueError):
            writer.write_paper_results("paper", [invalid], request_id=request_id)
        assert not (tmp_path / f"paper-{request_id}.jsonl").exists()


def test_agent_writers_preserve_existing_row_bytes_and_mixed_default(tmp_path: Path) -> None:
    writer = ResultWriter(tmp_path)
    web = [SearchRecord(normalize_url("https://example.test/web"), "Résumé")]
    papers = [_paper(sources=("agent:primary",))]
    agent = writer.write_results("agent", web, request_id="11111111")
    old = writer.write_results("llm", web, request_id="11111111")
    assert agent.is_absolute()
    assert agent.name == "agent-11111111.jsonl"
    assert agent.read_bytes() == old.read_bytes()
    agent_paper = writer.write_paper_results("agent", papers, request_id="22222222")
    old_paper = writer.write_paper_results("llm", papers, request_id="22222222")
    assert agent_paper.read_bytes() == old_paper.read_bytes()
    assert "type" not in json.loads(agent_paper.read_text())
    agent_mixed = writer.write_mixed_results(web, papers, request_id="33333333", kind="agent")
    old_mixed = writer.write_mixed_results(web, papers, request_id="33333333")
    assert old_mixed.name == "llm-33333333.jsonl"
    assert agent_mixed.name == "agent-33333333.jsonl"
    assert agent_mixed.read_bytes() == old_mixed.read_bytes()
    assert [json.loads(row)["type"] for row in agent_mixed.read_text().splitlines()] == [
        "web",
        "paper",
    ]
    with pytest.raises(FileExistsError):
        writer.write_results("agent", [], request_id="11111111")
    assert agent.read_bytes() == old.read_bytes()


def test_agent_serialization_and_partial_write_leave_no_file(tmp_path: Path) -> None:
    from collections.abc import Iterator

    writer = ResultWriter(tmp_path)
    with pytest.raises(ValueError):
        writer.write_results(
            "agent",
            [SearchRecord(normalize_url("https://example.test"), "")],
            request_id="11111111",
        )
    assert not (tmp_path / "agent-11111111.jsonl").exists()

    def broken_rows() -> Iterator[str]:
        yield "one valid row"
        raise OSError("simulated write failure")

    with pytest.raises(OSError):
        writer._write_serialized("agent-22222222.jsonl", broken_rows())
    assert not (tmp_path / "agent-22222222.jsonl").exists()
