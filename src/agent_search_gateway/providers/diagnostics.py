"""Small, allowlisted provider diagnostics; never inspect arbitrary payloads."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ..errors import ExecutionFailure
from ..observability import (
    CANDIDATE_REJECTION_REASONS,
    DIAGNOSTIC_TEXT_LIMITS,
    DiagnosticText,
    safe_rejection_counts,
)

_COUNT_FIELDS = frozenset(
    {
        "response_bytes",
        "stdout_bytes",
        "stderr_bytes",
        "received",
        "accepted",
        "rejected",
        "rewritten",
        "processed",
        "unprocessed",
    }
)
_CLASSIFICATIONS = {
    "category": frozenset(
        {
            "unknown",
            "provider_response",
            "transport",
            "http_status",
            "decode",
            "invalid_envelope",
            "protocol",
            "timeout",
            "cancelled",
            "spawn",
            "nonzero_exit",
            "invalid_json",
            "empty_output",
            "output_limit",
        }
    ),
    "semantic_result": frozenset(
        {"success", "error", "unknown", "empty", "no_match", "accepted", "semantic_failure"}
    ),
    "response_kind": frozenset({"object", "array", "json", "xml", "text", "empty", "unknown"}),
    "phase": frozenset({"adapter", "staging"}),
}
_MEDIA_TYPE = re.compile(r"[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+\Z")
_FAILURE_FIELDS = frozenset(
    {
        "status",
        "http_status",
        "response_bytes",
        "content_type",
        "upstream_status",
        "upstream_code",
        "upstream_message",
        "semantic_result",
        "response_kind",
        "exit_code",
        "stdout_bytes",
        "stderr_bytes",
    }
)


def safe_diagnostic_fields(fields: Mapping[str, object]) -> dict[str, object]:
    """Select validated response diagnostics; event identity stays with the caller."""
    try:
        return _select_diagnostic_fields(fields)
    except Exception:
        return {}


def _select_diagnostic_fields(fields: Mapping[str, object]) -> dict[str, object]:
    selected: dict[str, object] = {}
    for name in _COUNT_FIELDS:
        value = fields.get(name)
        if type(value) is int and value >= 0:
            selected[name] = value
    for name in ("status", "http_status"):
        value = fields.get(name)
        if type(value) is int and 100 <= value <= 599:
            selected[name] = value
    value = fields.get("exit_code")
    if type(value) is int:
        selected["exit_code"] = value
    for name, allowed in _CLASSIFICATIONS.items():
        value = fields.get(name)
        if type(value) is str and value in allowed:
            selected[name] = value
    value = fields.get("content_type")
    if type(value) is str:
        media_type = value.partition(";")[0].strip()
        if len(media_type) <= 127 and _MEDIA_TYPE.fullmatch(media_type):
            selected["content_type"] = media_type.lower()
    for name, limit in DIAGNOSTIC_TEXT_LIMITS:
        value = fields.get(name)
        if type(value) is DiagnosticText:
            selected[name] = DiagnosticText(value.text, min(limit, value.max_chars))
        elif type(value) is str:
            selected[name] = DiagnosticText(value, limit)
        elif type(value) is int and name != "upstream_message":
            selected[name] = DiagnosticText(str(value), limit)
    counts = safe_rejection_counts(fields.get("rejection_counts"))
    if counts is not None:
        selected["rejection_counts"] = counts
    return selected


def best_effort_diagnostics(
    extractor: Callable[[], Mapping[str, object]],
) -> dict[str, object]:
    """Contain ordinary diagnostic-only failures, while allowing cancellation through."""
    try:
        return safe_diagnostic_fields(extractor())
    except Exception:
        return {}


def diagnostic_fields_from_failure(exc: BaseException) -> dict[str, object]:
    """Read known failure metadata; category and other terminal-owner fields stay out."""
    if not isinstance(exc, ExecutionFailure):
        return {}
    fields = best_effort_diagnostics(lambda: exc.diagnostics)
    return {name: value for name, value in fields.items() if name in _FAILURE_FIELDS}


@dataclass(slots=True)
class CandidateCounts:
    """One response's local candidate partition, including unfinished strict parses."""

    received: int
    accepted: int = field(default=0, init=False)
    rewritten: int = field(default=0, init=False)
    _rejection_counts: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.received) is not int or self.received < 0:
            raise ValueError("received must be a nonnegative integer")

    @property
    def rejected(self) -> int:
        return sum(self._rejection_counts.values())

    @property
    def processed(self) -> int:
        return self.accepted + self.rejected

    @property
    def unprocessed(self) -> int:
        return self.received - self.processed

    @property
    def rejection_counts(self) -> dict[str, int]:
        return dict(self._rejection_counts)

    def accept(self, *, rewritten: bool = False) -> None:
        self._check_remaining()
        if type(rewritten) is not bool:
            raise ValueError("rewritten must be a boolean")
        self.accepted += 1
        self.rewritten += int(rewritten)

    def reject(self, reason: str) -> None:
        self._check_remaining()
        if type(reason) is not str or reason not in CANDIDATE_REJECTION_REASONS:
            raise ValueError("unknown candidate rejection reason")
        self._rejection_counts[reason] = self._rejection_counts.get(reason, 0) + 1

    def _check_remaining(self) -> None:
        if self.processed >= self.received:
            raise ValueError("all received candidates have already been processed")

    def as_fields(self) -> dict[str, object]:
        return {
            "received": self.received,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "rejection_counts": self.rejection_counts,
            "rewritten": self.rewritten,
            "processed": self.processed,
            "unprocessed": self.unprocessed,
        }
