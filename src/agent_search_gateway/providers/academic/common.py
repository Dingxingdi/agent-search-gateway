"""Small boundary helpers shared by academic provider adapters."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date
from typing import Any, Protocol

from ...errors import ErrorCode, ProtocolFailure
from ...observability import log_event
from ..http import ResponseDiagnosticExtractor

_LOGGER = logging.getLogger(__name__)


class AcademicHttpExecutor(Protocol):
    async def request_json(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: object | None = None,
        response_diagnostics: ResponseDiagnosticExtractor | None = None,
    ) -> object: ...

    async def request_text(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: object | None = None,
        response_diagnostics: ResponseDiagnosticExtractor | None = None,
    ) -> str: ...


def protocol_failure(
    provider: str,
    detail: str,
    *,
    stage: str = "paper_search",
    reason: str | None = None,
) -> ProtocolFailure:
    return ProtocolFailure(
        ErrorCode.PROTOCOL_ERROR,
        f"{provider}/{stage}: {detail}",
        reason=reason,
    )


def reject_item(provider: str, reason: str = "invalid_record_shape") -> None:
    log_event(
        _LOGGER,
        logging.DEBUG,
        "paper_candidate_rejected",
        provider=provider,
        reason=reason,
    )


def as_mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, dict) else None


def as_list(value: object) -> list[object] | None:
    return value if isinstance(value, list) else None


def text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def parse_iso_date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"
