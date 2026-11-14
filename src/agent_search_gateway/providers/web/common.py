"""Shared pure helpers for built-in web provider adapters."""

import logging
from collections.abc import Callable, Mapping
from typing import Literal, Protocol

import httpx

from ...errors import ErrorCode, ExecutionFailure
from ...observability import log_event
from ...url_normalization import NormalizedURL, normalize_url
from ..contracts import KeywordSearchHit, URLFetchCandidate
from ..diagnostics import CandidateCounts, best_effort_diagnostics, safe_diagnostic_fields
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


def json_search_response_diagnostics(
    response: httpx.Response,
    *,
    results_key: str,
    include_invalid_results_kind: bool = True,
) -> Mapping[str, object]:
    """Classify a known JSON search envelope without affecting response handling."""
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    if not isinstance(payload, dict):
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    if payload.get("error") is not None:
        return {
            "category": "provider_response",
            "semantic_result": "error",
            "response_kind": "object",
            **selected_error_fields(payload["error"]),
        }
    if not isinstance(payload.get(results_key), list):
        fields: dict[str, object] = {
            "category": "invalid_envelope",
            "semantic_result": "unknown",
        }
        if include_invalid_results_kind:
            fields["response_kind"] = "object"
        return fields
    return {"semantic_result": "success", "response_kind": "object"}


def organic_search_hits(
    payload: object, provider: str, *, logger: logging.Logger
) -> list[KeywordSearchHit]:
    """Parse the shared organic_results/link search schema and report local counts."""
    root = require_object(payload, provider, "search", "response")
    if root.get("error") is not None:
        raise failure(
            provider,
            "search",
            "provider reported failure",
            diagnostics=best_effort_diagnostics(lambda: selected_error_fields(root["error"])),
        )
    results = require_list(root.get("organic_results"), provider, "search", "organic_results")
    return keyword_hits_from_results(results, provider, logger=logger, url_key="link")


def keyword_hits_from_results(
    results: list[object],
    provider: str,
    *,
    logger: logging.Logger,
    url_key: Literal["url", "link"] = "url",
) -> list[KeywordSearchHit]:
    """Parse shared keyword fields while preserving adapter-local count attribution."""
    counts = CandidateCounts(len(results))
    hits: list[KeywordSearchHit] = []
    for item in results:
        try:
            result = require_object(item, provider, "search", "result")
            hits.append(
                KeywordSearchHit(
                    url=non_empty_string(
                        result.get(url_key), provider, "search", f"result.{url_key}"
                    ),
                    title=optional_string(result.get("title"), provider, "search", "result.title"),
                    snippet=optional_string(
                        result.get("snippet"), provider, "search", "result.snippet"
                    ),
                )
            )
        except ExecutionFailure:
            counts.reject(candidate_rejection_reason(item, url_key))
            continue
        counts.accept()
    log_event(
        logger,
        logging.DEBUG,
        "provider_candidates_summary",
        provider=provider,
        stage="search",
        phase="adapter",
        exc_info=False,
        **counts.as_fields(),
    )
    return hits


def parse_fetch_with_diagnostics(
    payload: object, parse: Callable[[object], URLFetchCandidate]
) -> URLFetchCandidate:
    """Retain safe envelope diagnostics on a provider's fetch parser failure."""
    try:
        return parse(payload)
    except ExecutionFailure as exc:
        exc.diagnostics = safe_diagnostic_fields(
            {"category": "invalid_envelope", "semantic_result": "error", **exc.diagnostics}
        )
        raise


def fetch_response_diagnostics(
    response: httpx.Response, parse: Callable[[object], URLFetchCandidate]
) -> Mapping[str, object]:
    """Classify a JSON fetch response with the adapter's existing parser."""
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    try:
        parse(payload)
    except ExecutionFailure as exc:
        return {"category": "invalid_envelope", "semantic_result": "error", **exc.diagnostics}
    return {"response_kind": "object", "semantic_result": "success"}
