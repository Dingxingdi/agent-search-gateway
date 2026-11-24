"""Secret-safe structured observability primitives."""

import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, TextIO
from urllib.parse import urlsplit, urlunsplit

from . import body_preview
from .errors import ConfigFailure, ErrorCode
from .input_limits import (
    MAX_PREVIEW_SCAN_BYTES,
    PreparedInput,
    PreviewProjector,
    PreviewScanBudgetExceeded,
)
from .request_ids import current_request_id

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

FileHandlerFactory = Callable[..., logging.Handler]

_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
_PREFERRED_FIELD_ORDER = ("provider", "stage", "url", "event")
DIAGNOSTIC_TEXT_LIMITS = (
    ("upstream_code", 128),
    ("upstream_status", 128),
    ("upstream_type", 128),
    ("upstream_message", 512),
)
_EMERGENCY_MESSAGE = "agent-search-gateway debug logging sink failure\n"

CANDIDATE_REJECTION_REASONS = frozenset(
    {
        "missing_url",
        "invalid_url",
        "missing_required_field",
        "invalid_record",
        "invalid_record_shape",
        "unsupported_scheme",
        "recognized_invalid_redirect",
        "decode_error",
        "empty_abstract",
        "judge_execution_failed",
        "duplicate",
        "missing_title",
        "missing_source_id",
        "invalid_identifier",
        "identifier_conflict",
        "invalid_date",
        "invalid_landing_url",
    }
)


def safe_rejection_counts(value: object) -> dict[str, int] | None:
    """Snapshot only a bounded mapping of known reasons and nonnegative counts."""
    if not isinstance(value, Mapping) or len(value) > len(CANDIDATE_REJECTION_REASONS):
        return None
    counts: dict[str, int] = {}
    for reason, count in value.items():
        if (
            type(reason) is not str
            or reason not in CANDIDATE_REJECTION_REASONS
            or type(count) is not int
            or count < 0
        ):
            return None
        counts[reason] = count
    return counts


def _render_field(name: str, value: object) -> str:
    if name == "rejection_counts":
        counts = safe_rejection_counts(value)
        if counts is not None:
            return json.dumps(counts, sort_keys=True, separators=(",", ":"))
    return _render_scalar(value)


@dataclass(frozen=True, slots=True, repr=False)
class DiagnosticText:
    """Selected upstream scalar text, rendered only with the active redactor."""

    text: str
    max_chars: int

    def __post_init__(self) -> None:
        if type(self.text) is not str:
            raise TypeError("diagnostic text must be a string")
        if type(self.max_chars) is not int or not 1 <= self.max_chars <= 512:
            raise ValueError("diagnostic text limit must be between 1 and 512")

    def __repr__(self) -> str:
        return "<DiagnosticText>"

    __str__ = __repr__


class SecretValue:
    """A secret whose implicit string representations are always redacted."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"

    def __str__(self) -> str:
        return "<redacted>"


class SecretRedactor:
    """Redact configured secrets from fully rendered logging output."""

    def __init__(self, secrets: Iterable[SecretValue] = ()) -> None:
        self._secrets: set[str] = set()
        self._ordered_secrets: tuple[str, ...] = ()
        self.add_secrets(secrets)

    def add_secrets(self, secrets: Iterable[SecretValue]) -> None:
        for secret in secrets:
            value = secret.reveal()
            if not value:
                continue
            self._secrets.add(value)
            escaped = json.dumps(value, ensure_ascii=False)[1:-1]
            if escaped:
                self._secrets.add(escaped)
        self._ordered_secrets = tuple(sorted(self._secrets, key=len, reverse=True))

    def sensitive_spans(self, text: str) -> list[tuple[int, int]]:
        """Original ranges for each registered value with overlapping occurrences combined."""
        spans = []
        for value in self._ordered_secrets:
            start = text.find(value)
            while start >= 0:
                left, right = start, start + len(value)
                while (start := text.find(value, start + 1)) >= 0 and start < right:
                    right = start + len(value)
                spans.append((left, right))
        return spans

    def redact(self, rendered: str) -> str:
        for secret in self._ordered_secrets:
            rendered = rendered.replace(secret, "<redacted>")
        return rendered


class SecretRedactingFilter(logging.Filter):
    """Compatibility filter for existing provider tests and ad-hoc handlers."""

    def __init__(self, secrets: Iterable[SecretValue]) -> None:
        super().__init__()
        self._redactor = SecretRedactor(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redactor.redact(record.getMessage())
        record.args = ()
        return True


def normalize_log_reason(value: str, max_chars: int = 160) -> str:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    normalized = " ".join(value.split())
    if len(normalized) <= max_chars:
        return normalized
    if max_chars == 1:
        return "…"
    return f"{normalized[: max_chars - 1]}…"


def elapsed_ms(monotonic: Callable[[], float], started: float) -> int:
    """Convert a monotonic interval to a non-negative whole-millisecond duration."""

    return max(0, int((monotonic() - started) * 1000))


def target_url_for_log(value: str) -> str:
    """Render a target path without userinfo, query, or fragment."""

    return _url_for_log(value, keep_query=False, keep_fragment=False)


def http_endpoint_for_log(value: str) -> str:
    """Render an HTTP endpoint without userinfo or request-specific query data."""

    rendered = _url_for_log(value, keep_query=False, keep_fragment=False)
    parsed = urlsplit(rendered)
    # Reader-style endpoints can embed a complete target URL in the path.
    if re.search(r"https?:(?:/|%2f){2}", parsed.path, re.IGNORECASE):
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return rendered


def _url_for_log(value: str, *, keep_query: bool, keep_fragment: bool) -> str:
    try:
        parsed = urlsplit(value)
    except (ValueError, UnicodeError):
        return "<invalid-url>"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "<invalid-url>"
    _, separator, hostport = parsed.netloc.rpartition("@")
    netloc = hostport if separator else parsed.netloc
    return urlunsplit(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.query if keep_query else "",
            parsed.fragment if keep_fragment else "",
        )
    )


_DIAGNOSTIC_URL = re.compile(
    r"https?(?::|%3a)(?:/|\\+/|%2f){2}(?:<redacted>|[^\s<>\"'])+", re.IGNORECASE
)
_DIAGNOSTIC_AUTH = re.compile(
    r"\b(?:authorization|proxy-authorization|cookie|set-cookie)[\"']?\s*[:=][^\r\n]*"
    r"|\b(?:bearer|basic)\s+[^\s,;]+"
    r"|\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|signature)"
    r"[\"']?\s*[:=]\s*(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;]+)",
    re.IGNORECASE,
)


def _diagnostic_text_for_log(value: str, redactor: SecretRedactor) -> str:
    # Never decode/unescape URLs here: that could create a secret after redaction.
    value = redactor.redact(value)
    # Provider-supplied URLs can carry unregistered secrets in any component.
    value = _DIAGNOSTIC_URL.sub("<redacted-url>", value)
    value = _DIAGNOSTIC_AUTH.sub("<redacted>", value)
    value = "".join(character if character.isprintable() else " " for character in value)
    return " ".join(value.split())


def _render_string(value: str) -> str:
    if (
        value
        and value.isprintable()
        and not any(character.isspace() or character in {'"', "\\"} for character in value)
    ):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _render_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _render_string(value)
    if type(value) in (int, float):
        return repr(value)
    return _render_string(f"<{type(value).__name__}>")


def _validated_field_name(name: str) -> str:
    if _FIELD_NAME.fullmatch(name) is None:
        raise ValueError(f"invalid log field name: {name}")
    return name


class KeyValueFormatter(logging.Formatter):
    """Render structured events as deterministic, single physical lines."""

    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def redact_for_preview(self, text: str) -> str:
        return self._redactor.redact(text)

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "gateway_event", None)
        raw_fields = getattr(record, "gateway_fields", None)
        if isinstance(event, str) and isinstance(raw_fields, dict):
            fields = dict(raw_fields)
        else:
            event = "log"
            fields = {"message": record.getMessage()}

        values: dict[str, object] = {
            _validated_field_name(str(key)): value for key, value in fields.items()
        }
        for name, limit in DIAGNOSTIC_TEXT_LIMITS:
            value = values.get(name)
            if type(value) is int and name != "upstream_message":
                value = str(value)
            if type(value) is str:
                value = DiagnosticText(value, limit)
            if type(value) is DiagnosticText:
                safe = _diagnostic_text_for_log(value.text, self._redactor)
                max_chars = min(limit, value.max_chars)
                values[name] = normalize_log_reason(safe, max_chars)
                if name == "upstream_message":
                    values["upstream_message_chars"] = len(value.text)
                    values["upstream_message_bytes"] = len(value.text.encode("utf-8"))
                    values["upstream_message_truncated"] = len(safe) > max_chars
        values["event"] = event
        request_id = current_request_id() or "-"
        timestamp = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
        parts = [
            record.levelname,
            f"time={timestamp.replace('+00:00', 'Z')}",
            f"request={request_id}",
        ]
        emitted: set[str] = {"time"}
        for name in _PREFERRED_FIELD_ORDER:
            if name not in values:
                continue
            parts.append(f"{name}={_render_field(name, values[name])}")
            emitted.add(name)
        for name in sorted(values.keys() - emitted):
            parts.append(f"{name}={_render_field(name, values[name])}")

        if record.exc_info:
            traceback_text = self.formatException(record.exc_info)
            parts.append(f"traceback={_render_string(traceback_text)}")
        return self._redactor.redact(" ".join(parts))


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    exc_info: bool | BaseException = False,
    **fields: object,
) -> None:
    if not logger.isEnabledFor(level):
        return
    _validated_field_name(event)
    for name in fields:
        _validated_field_name(name)
    try:
        for name, limit in DIAGNOSTIC_TEXT_LIMITS:
            value = fields.get(name)
            if type(value) is int and name != "upstream_message":
                value = str(value)
            if type(value) is str:
                fields[name] = DiagnosticText(value, limit)
        if "rejection_counts" in fields:
            counts = safe_rejection_counts(fields["rejection_counts"])
            if counts is not None:
                fields["rejection_counts"] = counts
        logger.log(
            level,
            "",
            exc_info=exc_info,
            extra={"gateway_event": event, "gateway_fields": dict(fields)},
        )
    except Exception:
        # Logging and diagnostic preparation must not change the business result.
        pass


_BodySource = bytes | Callable[[], bytes | None] | None
_UNSAFE_PREVIEW_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_PREVIEW_LINE_BREAKS = str.maketrans({"\u0085": "\n", "\u2028": "\n", "\u2029": "\n"})


def _preview_formatters(logger: logging.Logger) -> tuple[KeyValueFormatter, ...]:
    receiving: dict[SecretRedactor, KeyValueFormatter] = {}
    current: logging.Logger | None = logger
    while current is not None:
        for handler in current.handlers:
            if handler.level > logging.DEBUG:
                continue
            while isinstance(handler, _SafeDelegatingHandler):
                handler = handler._delegate
            formatter = handler.formatter
            if isinstance(formatter, KeyValueFormatter):
                receiving.setdefault(formatter._redactor, formatter)
        current = current.parent if current.propagate else None
    return tuple(receiving.values())


def _active_scrubber(logger: logging.Logger) -> body_preview.BodyScrubber | None:
    formatters = _preview_formatters(logger)
    if not formatters:
        return None

    def redact(value: str) -> str:
        for formatter in formatters:
            value = formatter.redact_for_preview(value)
        return value

    def spans(value: str) -> Iterable[tuple[int, int]]:
        for formatter in formatters:
            yield from formatter._redactor.sensitive_spans(value)

    return body_preview.BodyScrubber(redact, spans, http_endpoint_for_log)


def make_input_preview_projector(logger: logging.Logger) -> PreviewProjector | None:
    if not logger.isEnabledFor(logging.DEBUG):
        return None
    try:
        scrubber = _active_scrubber(logger)
        return scrubber.project if scrubber else None
    except Exception:
        return None


def _preview_side(
    source: _BodySource,
    size: int | None,
    scrubber: body_preview.BodyScrubber | None,
    *,
    incomplete: bool = False,
) -> tuple[str | None, int, bool]:
    count = size if size is not None else len(source) if isinstance(source, bytes) else 0
    if count > MAX_PREVIEW_SCAN_BYTES:
        return None, count, True
    try:
        if scrubber is not None:
            raw = source() if callable(source) else source
            if raw:
                if size is None:
                    count = len(raw)
                if len(raw) > MAX_PREVIEW_SCAN_BYTES:
                    return None, count, True
                text = raw.decode("utf-8")
                if _UNSAFE_PREVIEW_CONTROL.search(text):
                    return None, count, incomplete
                if incomplete:
                    json.loads(text)
                safe = scrubber.scrub(text).translate(_PREVIEW_LINE_BREAKS)
                # Scrubbing an escaped JSON string can materialize a previously encoded control.
                if _UNSAFE_PREVIEW_CONTROL.search(safe):
                    return None, count, incomplete
                value, clipped = body_preview.limit_preview(safe, _render_string)
                # A short registered value can match inside our own replacement or a
                # JSON escape. Keep the bounded field stable for every receiving formatter.
                if next(iter(scrubber.registered_spans(_render_string(value))), None) is not None:
                    return None, count, clipped or incomplete
                return value, count, clipped or incomplete
    except PreviewScanBudgetExceeded:
        return None, count, True
    except Exception:
        pass
    return None, count, incomplete and count > 0


def log_upstream_body_preview(
    logger: logging.Logger,
    *,
    provider: str,
    stage: str,
    endpoint: str | None,
    attempt: int,
    transport: str,
    request_body: _BodySource = None,
    prepared_body: PreparedInput | None = None,
    response_body: _BodySource = None,
    request_body_sent_bytes: int | None = None,
    request_body_original_bytes: int | None = None,
    request_input_truncated: bool = False,
    response_body_bytes: int | None = None,
    response_incomplete: bool = False,
    protocol_attempt: int | None = None,
) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return
    try:
        scrubber = _active_scrubber(logger)
        request_scan_exceeded = False
        if prepared_body is not None:
            request_body_original_bytes = prepared_body.original_bytes
            request_input_truncated = prepared_body.input_truncated
            if request_body_sent_bytes is None:
                request_body_sent_bytes = len(prepared_body.content)
            request_body = prepared_body.content if not prepared_body.input_truncated else None
            if prepared_body.preview_body is not None:
                request_body = prepared_body.preview_body.content
                request_scan_exceeded = prepared_body.preview_body.scan_budget_exceeded
        request, sent, request_clipped = _preview_side(
            request_body, request_body_sent_bytes, scrubber
        )
        response, count, response_clipped = _preview_side(
            response_body, response_body_bytes, scrubber, incomplete=response_incomplete
        )
        safe_endpoint = http_endpoint_for_log(endpoint) if endpoint is not None else None
        if scrubber is not None:
            try:
                provider = scrubber.redact(provider)
                stage = scrubber.redact(stage)
                if safe_endpoint is not None:
                    safe_endpoint = scrubber.redact(safe_endpoint)
            except Exception:
                provider = stage = "<redacted>"
                safe_endpoint = None
        log_event(
            logger,
            logging.DEBUG,
            "upstream_body_preview",
            exc_info=False,
            provider=provider,
            stage=stage,
            endpoint=safe_endpoint,
            attempt=attempt,
            transport=transport,
            request_body_preview=request,
            request_body_sent_bytes=sent,
            request_body_original_bytes=(
                request_body_original_bytes if request_body_original_bytes is not None else sent
            ),
            request_input_truncated=request_input_truncated,
            request_preview_truncated=request_clipped or request_scan_exceeded,
            response_body_preview=response,
            response_body_bytes=count,
            response_preview_truncated=response_clipped,
            **({"protocol_attempt": protocol_attempt} if protocol_attempt is not None else {}),
        )
    except Exception:
        # Logging preparation cannot change a transport outcome.
        pass


def _write_emergency(stderr: TextIO) -> None:
    try:
        stderr.write(_EMERGENCY_MESSAGE)
        stderr.flush()
    except Exception:
        pass


if TYPE_CHECKING:
    _StreamHandlerBase = logging.StreamHandler[TextIO]
else:
    _StreamHandlerBase = logging.StreamHandler


class _SafeStreamHandler(_StreamHandlerBase):
    """Report formatter/stream failures without logging's unsafe exception dump."""

    def __init__(self, stderr: TextIO) -> None:
        super().__init__(stderr)
        self._emergency_stderr = stderr

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        _write_emergency(self._emergency_stderr)


class SafeRotatingFileHandler(RotatingFileHandler):
    """Rotating handler whose sink errors never escape into business code."""

    def __init__(
        self,
        filename: Path,
        *,
        stderr: TextIO,
        max_bytes: int,
        backup_count: int,
    ) -> None:
        self._emergency_stderr = stderr
        super().__init__(
            filename,
            mode="a",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        _write_emergency(self._emergency_stderr)


class _SafeDelegatingHandler(logging.Handler):
    """Contain exceptions from injected handlers used by failure-path tests."""

    def __init__(self, delegate: logging.Handler, stderr: TextIO) -> None:
        super().__init__(delegate.level)
        self._delegate = delegate
        self._stderr = stderr

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._delegate.handle(record)
        except Exception:
            _write_emergency(self._stderr)

    def close(self) -> None:
        with suppress(Exception):
            self._delegate.close()
        super().close()


class DebugLoggingSession:
    """Own one debug logging configuration and restore prior logger state."""

    def __init__(
        self,
        logger: logging.Logger,
        handlers: tuple[logging.Handler, ...],
        redactor: SecretRedactor,
        *,
        previous_level: int,
        previous_propagate: bool,
    ) -> None:
        self._logger = logger
        self._handlers = handlers
        self._redactor = redactor
        self._previous_level = previous_level
        self._previous_propagate = previous_propagate
        self._closed = False

    def add_secrets(self, *secrets: SecretValue | Iterable[SecretValue]) -> None:
        collected: list[SecretValue] = []
        for item in secrets:
            if isinstance(item, SecretValue):
                collected.append(item)
            else:
                collected.extend(item)
        self._redactor.add_secrets(collected)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for handler in self._handlers:
            self._logger.removeHandler(handler)
            with suppress(Exception):
                handler.flush()
            with suppress(Exception):
                handler.close()
        self._logger.setLevel(self._previous_level)
        self._logger.propagate = self._previous_propagate


def _default_file_handler(
    log_file: Path,
    *,
    stderr: TextIO,
    max_bytes: int,
    backup_count: int,
) -> logging.Handler:
    return SafeRotatingFileHandler(
        log_file,
        stderr=stderr,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )


def configure_debug_logging(
    log_file: Path,
    *,
    stderr: TextIO,
    max_bytes: int = LOG_MAX_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
    file_handler_factory: FileHandlerFactory | None = None,
) -> DebugLoggingSession:
    """Install project-only DEBUG handlers with rotation and final redaction."""

    logger = logging.getLogger("agent_search_gateway")
    previous_level = logger.level
    previous_propagate = logger.propagate
    handlers: list[logging.Handler] = []
    try:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if backup_count < 0:
            raise ValueError("backup_count must not be negative")
        log_file.parent.mkdir(parents=True, exist_ok=True)
        redactor = SecretRedactor()
        formatter = KeyValueFormatter(redactor)

        stderr_handler = _SafeStreamHandler(stderr)
        stderr_handler.setFormatter(formatter)
        handlers.append(stderr_handler)

        factory = file_handler_factory or _default_file_handler
        file_handler = factory(
            log_file,
            stderr=stderr,
            max_bytes=max_bytes,
            backup_count=backup_count,
        )
        if not isinstance(file_handler, logging.Handler):
            raise TypeError("file handler factory must return logging.Handler")
        file_handler.setFormatter(formatter)
        if file_handler_factory is not None:
            file_handler = _SafeDelegatingHandler(file_handler, stderr)
        handlers.append(file_handler)

        for handler in handlers:
            handler.__dict__["_agent_search_gateway_debug_owned"] = True
            logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        return DebugLoggingSession(
            logger,
            tuple(handlers),
            redactor,
            previous_level=previous_level,
            previous_propagate=previous_propagate,
        )
    except Exception as exc:
        for handler in handlers:
            logger.removeHandler(handler)
            with suppress(Exception):
                handler.close()
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
        raise ConfigFailure(ErrorCode.CONFIG_ERROR, "Failed to configure debug logging") from exc
