"""Strict restricted-format parser for LLM academic paper search results."""

from __future__ import annotations

from datetime import date

from .errors import InputFailure, ParserFailure
from .models import TextSearchKind
from .providers.contracts import PaperSearchHit
from .url_normalization import normalize_url

_HEADING = "## Paper"
_FIELDS = (
    "Title",
    "Authors",
    "Abstract",
    "DOI",
    "arXiv",
    "Published",
    "Updated",
    "URL",
    "PDF",
    "Venue",
    "Topics",
    "Citations",
    "Open Access",
    "OA Status",
    "License",
)
_BLOCK_LINES = 1 + len(_FIELDS)


def parse_paper_markdown(
    markdown: str, *, provider: str, source_kind: TextSearchKind = "llm"
) -> list[PaperSearchHit]:
    """Parse only exact repeated ``## Paper`` blocks into provider candidates."""

    if not isinstance(markdown, str) or not markdown.strip():
        raise ParserFailure("LLM paper response contained no Paper blocks")
    provider_name = provider.strip() if isinstance(provider, str) else ""
    if not provider_name:
        raise ParserFailure("LLM paper provider name is invalid")

    lines = markdown.strip().splitlines()
    if len(lines) % _BLOCK_LINES != 0:
        raise ParserFailure("LLM paper response does not match the required block grammar")

    hits: list[PaperSearchHit] = []
    for start in range(0, len(lines), _BLOCK_LINES):
        block = lines[start : start + _BLOCK_LINES]
        hits.append(_parse_block(block, provider_name, source_kind))
    return hits


def _parse_block(lines: list[str], provider: str, source_kind: TextSearchKind) -> PaperSearchHit:
    if len(lines) != _BLOCK_LINES or lines[0] != _HEADING:
        raise ParserFailure("Paper block heading is invalid")

    values: dict[str, str] = {}
    for index, field in enumerate(_FIELDS, start=1):
        prefix = f"{field}:"
        line = lines[index]
        if not line.startswith(prefix):
            raise ParserFailure("Paper block fields do not match the required grammar")
        values[field] = line[len(prefix) :].strip()

    title = values["Title"]
    if not title:
        raise ParserFailure("Paper block title is empty")
    url = _parse_required_url(values["URL"])
    pdf_url = _parse_optional_url(values["PDF"])
    published = _parse_optional_date(values["Published"], "published date")
    updated = _parse_optional_date(values["Updated"], "updated date")
    citations = _parse_optional_citations(values["Citations"])
    is_open_access = _parse_open_access(values["Open Access"])

    return PaperSearchHit(
        source=f"{source_kind}:{provider}",
        source_id=str(url),
        title=title,
        authors=_split_semicolon(values["Authors"]),
        abstract=values["Abstract"],
        doi=values["DOI"],
        arxiv_id=values["arXiv"],
        published_date=published,
        updated_date=updated,
        url=str(url),
        pdf_url=str(pdf_url) if pdf_url is not None else "",
        venue=values["Venue"],
        topics=_split_semicolon(values["Topics"]),
        citation_count=citations,
        is_open_access=is_open_access,
        oa_status=values["OA Status"],
        license=values["License"],
    )


def _split_semicolon(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    items = tuple(item.strip() for item in value.split(";") if item.strip())
    if not items:
        return ()
    return items


def _parse_required_url(value: str) -> str:
    if not value:
        raise ParserFailure("Paper block URL is empty")
    try:
        return str(normalize_url(value))
    except InputFailure as exc:
        raise ParserFailure("Paper block URL is invalid") from exc


def _parse_optional_url(value: str) -> str | None:
    if not value:
        return None
    try:
        return str(normalize_url(value))
    except InputFailure as exc:
        raise ParserFailure("Paper block PDF URL is invalid") from exc


def _parse_optional_date(value: str, label: str) -> date | None:
    if not value:
        return None
    if len(value) != 10:
        raise ParserFailure(f"Paper block {label} is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ParserFailure(f"Paper block {label} is invalid") from exc
    if parsed.isoformat() != value:
        raise ParserFailure(f"Paper block {label} is invalid")
    return parsed


def _parse_optional_citations(value: str) -> int | None:
    if not value:
        return None
    if not value.isascii() or not value.isdecimal():
        raise ParserFailure("Paper block citations value is invalid")
    return int(value)


def _parse_open_access(value: str) -> bool | None:
    normalized = value.casefold()
    if normalized in {"", "unknown"}:
        return None
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ParserFailure("Paper block open-access value is invalid")
