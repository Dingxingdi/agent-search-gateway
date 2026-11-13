"""Shared pure helpers for built-in web provider adapters."""

from collections.abc import Mapping
from typing import Literal, Protocol

from ...errors import ErrorCode, ExecutionFailure
from ...url_normalization import NormalizedURL, normalize_url
from ..diagnostics import safe_diagnostic_fields
from ..http import ResponseDiagnosticExtractor, ResponseInspector


class JsonRequester(Protocol):
    async def request_json(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
        response_inspector: ResponseInspector | None = None,
        response_diagnostics: ResponseDiagnosticExtractor | None = None,
    ) -> object: ...


class TextRequester(Protocol):
    async def request_text(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
        response_diagnostics: ResponseDiagnosticExtractor | None = None,
    ) -> str: ...


class HttpRequester(JsonRequester, TextRequester, Protocol):
    pass


def endpoint(base_url: str, suffix: str) -> str:
    return f"{base_url.rstrip('/')}/{suffix.lstrip('/')}"


def configured_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{label} must be a non-empty string")
    return value.strip()


def failure(
    provider: str,
    stage: str,
    reason: str,
    *,
    reason_code: str | None = None,
    diagnostics: Mapping[str, object] | None = None,
) -> ExecutionFailure:
    return ExecutionFailure(
        ErrorCode.ALL_PROVIDERS_FAILED,
        f"{provider}/{stage}: {reason}",
        reason=reason_code,
        diagnostics=diagnostics,
    )


def require_object(value: object, provider: str, stage: str, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise failure(provider, stage, f"{label} must be an object")
    return value


def require_list(value: object, provider: str, stage: str, label: str) -> list[object]:
    if not isinstance(value, list):
        raise failure(provider, stage, f"{label} must be an array")
    return value


def require_string(value: object, provider: str, stage: str, label: str) -> str:
    if not isinstance(value, str):
        raise failure(provider, stage, f"{label} must be a string")
    return value


def non_empty_string(value: object, provider: str, stage: str, label: str) -> str:
    text = require_string(value, provider, stage, label)
    if not text.strip():
        raise failure(provider, stage, f"{label} must be non-empty")
    return text


def optional_string(value: object, provider: str, stage: str, label: str) -> str:
    if value is None:
        return ""
    return require_string(value, provider, stage, label)


def selected_error_fields(error: object) -> dict[str, object]:
    """Select a known root error scalar or its explicit code/message children."""
    if isinstance(error, dict):
        return safe_diagnostic_fields(
            {"upstream_code": error.get("code"), "upstream_message": error.get("message")}
        )
    return safe_diagnostic_fields({"upstream_message": error})


def candidate_rejection_reason(item: object, url_key: Literal["url", "link"]) -> str:
    """Classify an already rejected candidate using only its explicit URL field."""
    if not isinstance(item, dict):
        return "invalid_record"
    value = item.get(url_key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return "missing_url"
    return "invalid_record"


def normalized_match(candidate: object, target: NormalizedURL, provider: str, stage: str) -> bool:
    text = non_empty_string(candidate, provider, stage, "result.url")
    try:
        return normalize_url(text) == target
    except Exception as exc:
        raise failure(provider, stage, "result URL is invalid") from exc
