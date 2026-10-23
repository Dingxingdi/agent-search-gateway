"""Compact JSONL persistence for search command results."""

import json
from collections.abc import Iterable, Mapping
from contextlib import suppress
from datetime import date
from pathlib import Path
from typing import Literal

from .errors import InputFailure
from .models import PaperRecord, SearchRecord, TextSearchKind
from .request_ids import ResultKind, result_filename
from .url_normalization import normalize_url

PaperResultKind = Literal["paper", "llm", "agent"]


def _web_payload(record: SearchRecord) -> dict[str, object]:
    abstract = record.abstract.strip()
    if not abstract:
        raise ValueError("search result abstract must be non-empty")
    return {"url": str(record.url), "abstract": abstract}


def _serialize_record(record: SearchRecord) -> str:
    return json.dumps(
        _web_payload(record),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _normalized_url_or_error(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"paper result {label} must be a normalized HTTP(S) URL")
    try:
        normalized = normalize_url(value)
    except InputFailure as exc:
        raise ValueError(f"paper result {label} must be a normalized HTTP(S) URL") from exc
    if normalized != value:
        raise ValueError(f"paper result {label} must already be normalized")
    return str(normalized)


def _optional_normalized_url_or_error(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _normalized_url_or_error(value, label)


def _string_tuple(value: object, label: str, *, unique: bool = False) -> list[str]:
    if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"paper result {label} must be a tuple of strings")
    items = list(value)
    if unique and len(items) != len(set(items)):
        raise ValueError(f"paper result {label} must not contain duplicates")
    return items


def _date_value(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, date):
        raise ValueError(f"paper result {label} must be a date or null")
    return value.isoformat()


def _citation_counts(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError("paper result citation_counts must be a mapping")
    result: dict[str, int] = {}
    for source, count in value.items():
        if not isinstance(source, str) or not source:
            raise ValueError("paper result citation source must be a non-empty string")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("paper result citation count must be a non-negative integer")
        result[source] = count
    return result


def _paper_payload(record: PaperRecord) -> dict[str, object]:
    title = record.title.strip() if isinstance(record.title, str) else ""
    if not title:
        raise ValueError("paper result title must be non-empty")
    abstract = record.abstract.strip() if isinstance(record.abstract, str) else ""
    venue = record.venue.strip() if isinstance(record.venue, str) else ""
    oa_status = record.oa_status.strip() if isinstance(record.oa_status, str) else ""
    license_value = record.license.strip() if isinstance(record.license, str) else ""
    if record.is_open_access is not None and not isinstance(record.is_open_access, bool):
        raise ValueError("paper result is_open_access must be boolean or null")

    identifiers = record.identifiers
    identifier_payload = {
        "doi": identifiers.doi,
        "arxiv_id": identifiers.arxiv_id,
        "semantic_scholar_id": identifiers.semantic_scholar_id,
        "openalex_id": identifiers.openalex_id,
        "dblp_key": identifiers.dblp_key,
        "core_id": identifiers.core_id,
    }
    if any(not isinstance(value, str) for value in identifier_payload.values()):
        raise ValueError("paper result identifiers must contain strings")

    return {
        "title": title,
        "authors": _string_tuple(record.authors, "authors"),
        "abstract": abstract,
        "identifiers": identifier_payload,
        "published_date": _date_value(record.published_date, "published_date"),
        "updated_date": _date_value(record.updated_date, "updated_date"),
        "url": _normalized_url_or_error(record.url, "url"),
        "pdf_url": _optional_normalized_url_or_error(record.pdf_url, "pdf_url"),
        "venue": venue,
        "topics": _string_tuple(record.topics, "topics"),
        "citation_counts": _citation_counts(record.citation_counts),
        "is_open_access": record.is_open_access,
        "oa_status": oa_status,
        "license": license_value,
        "sources": _string_tuple(record.sources, "sources", unique=True),
    }


def _serialize_paper_record(record: PaperRecord) -> str:
    return json.dumps(
        _paper_payload(record),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _serialize_mixed_web(record: SearchRecord) -> str:
    return json.dumps(
        {"type": "web", **_web_payload(record)},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _serialize_mixed_paper(record: PaperRecord) -> str:
    return json.dumps(
        {"type": "paper", **_paper_payload(record)},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class ResultWriter:
    def __init__(self, results_dir: Path) -> None:
        self._results_dir = results_dir

    def write_results(
        self,
        kind: ResultKind,
        records: Iterable[SearchRecord],
        *,
        request_id: str,
    ) -> Path:
        filename = result_filename(kind, request_id)
        serialized = tuple(_serialize_record(record) for record in records)
        return self._write_serialized(filename, serialized)

    def write_paper_results(
        self,
        kind: PaperResultKind,
        records: Iterable[PaperRecord],
        *,
        request_id: str,
    ) -> Path:
        if kind not in {"paper", "llm", "agent"}:
            raise ValueError(f"invalid paper result kind: {kind}")
        filename = result_filename(kind, request_id)
        serialized = tuple(_serialize_paper_record(record) for record in records)
        return self._write_serialized(filename, serialized)

    def write_mixed_results(
        self,
        web_records: Iterable[SearchRecord],
        paper_records: Iterable[PaperRecord],
        *,
        request_id: str,
        kind: TextSearchKind = "llm",
    ) -> Path:
        if kind not in {"llm", "agent"}:
            raise ValueError(f"invalid mixed result kind: {kind}")
        filename = result_filename(kind, request_id)
        serialized_web = tuple(_serialize_mixed_web(record) for record in web_records)
        serialized_paper = tuple(_serialize_mixed_paper(record) for record in paper_records)
        return self._write_serialized(filename, (*serialized_web, *serialized_paper))

    def _write_serialized(self, filename: str, serialized: Iterable[str]) -> Path:
        self._results_dir.mkdir(parents=True, exist_ok=True)
        target = self._results_dir / filename
        created = False
        try:
            with target.open("x", encoding="utf-8", newline="\n") as handle:
                created = True
                for line in serialized:
                    handle.write(line)
                    handle.write("\n")
            return target.resolve()
        except BaseException:
            if created:
                with suppress(OSError):
                    target.unlink()
            raise
