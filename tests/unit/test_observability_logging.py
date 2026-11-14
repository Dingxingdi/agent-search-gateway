import asyncio
import io
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from agent_search_gateway.errors import ConfigFailure, ErrorCode
from agent_search_gateway.observability import (
    LOG_BACKUP_COUNT,
    LOG_MAX_BYTES,
    DebugLoggingSession,
    KeyValueFormatter,
    SecretRedactingFilter,
    SecretRedactor,
    SecretValue,
    configure_debug_logging,
    http_endpoint_for_log,
    log_event,
    target_url_for_log,
)
from agent_search_gateway.request_ids import bind_request_id


def _isolated_logger(name: str, stream: io.StringIO, redactor: SecretRedactor) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(KeyValueFormatter(redactor))
    logger.addHandler(handler)
    return logger


def test_formatter_emits_structured_single_line_with_request_context() -> None:
    stream = io.StringIO()
    logger = _isolated_logger("tests.observability.contract", stream, SecretRedactor())

    with bind_request_id("11111111"):
        log_event(
            logger,
            logging.DEBUG,
            "completed",
            provider="tavily",
            stage="search",
            url="https://example.com/a?id=42&lang=ja",
            hits=10,
            reason="line one\nline two\tend",
        )

    line = stream.getvalue()
    assert len(line.splitlines()) == 1
    assert line.startswith("DEBUG request=11111111 ")
    assert "provider=tavily" in line
    assert "stage=search" in line
    assert "event=completed" in line
    assert "url=https://example.com/a?id=42&lang=ja" in line
    assert "hits=10" in line
    assert 'reason="line one\\nline two\\tend"' in line

    stream.seek(0)
    stream.truncate(0)
    log_event(logger, logging.DEBUG, "session_started", pid=123)
    assert stream.getvalue().startswith("DEBUG request=- event=session_started ")


def test_log_event_skips_validation_when_level_is_disabled() -> None:
    stream = io.StringIO()
    logger = _isolated_logger("tests.observability.disabled", stream, SecretRedactor())
    logger.setLevel(logging.INFO)

    log_event(logger, logging.DEBUG, "invalid event name")
    assert stream.getvalue() == ""

    logger.setLevel(logging.DEBUG)
    with pytest.raises(ValueError, match="invalid log field name"):
        log_event(logger, logging.DEBUG, "invalid event name")


def test_log_url_helpers_strip_credentials_query_and_fragment() -> None:
    value = "https://user:password@example.com:8443/path?q=QUERY_SENTINEL#fragment"

    assert target_url_for_log(value) == "https://example.com:8443/path"
    assert value.endswith("/path?q=QUERY_SENTINEL#fragment")
    assert http_endpoint_for_log(value) == "https://example.com:8443/path"
    assert target_url_for_log("not-a-url USERINFO_SENTINEL") == "<invalid-url>"


def test_final_formatter_redacts_fields_and_exception_traceback() -> None:
    stream = io.StringIO()
    redactor = SecretRedactor(
        [SecretValue("alpha-secret"), SecretValue("beta-secret"), SecretValue("")]
    )
    logger = _isolated_logger("tests.observability.redaction", stream, redactor)

    with bind_request_id("22222222"):
        try:
            raise RuntimeError("trace alpha-secret beta-secret")
        except RuntimeError:
            log_event(
                logger,
                logging.DEBUG,
                "failed",
                detail="field alpha-secret",
                exc_info=True,
            )

    line = stream.getvalue()
    assert len(line.splitlines()) == 1
    assert "alpha-secret" not in line
    assert "beta-secret" not in line
    assert line.count("<redacted>") >= 3
    assert "traceback=" in line
    assert "RuntimeError" in line
    assert "\\n" in line


def test_secret_redactor_handles_multiple_additions_and_escaped_secret_text() -> None:
    redactor = SecretRedactor([SecretValue("token"), SecretValue("")])
    redactor.add_secrets([SecretValue("token-longer"), SecretValue("line\nbreak")])

    rendered = redactor.redact("token-longer token line\\nbreak")
    assert rendered == "<redacted> <redacted> <redacted>"


def test_existing_secret_redacting_filter_api_still_redacts_message() -> None:
    record = logging.LogRecord(
        "test",
        logging.DEBUG,
        __file__,
        1,
        "credential=%s",
        ("credential-value",),
        None,
    )
    filter_ = SecretRedactingFilter([SecretValue("credential-value")])

    assert filter_.filter(record) is True
    assert record.getMessage() == "credential=<redacted>"


def test_debug_logging_session_installs_rotating_handlers_and_restores_state(
    tmp_path: Path,
) -> None:
    project_logger = logging.getLogger("agent_search_gateway")
    original_level = project_logger.level
    original_propagate = project_logger.propagate
    original_handlers = tuple(project_logger.handlers)
    httpx_level = logging.getLogger("httpx").level
    httpcore_level = logging.getLogger("httpcore").level
    stderr = io.StringIO()
    log_file = tmp_path / "logs" / "debug.log"

    assert LOG_MAX_BYTES == 5 * 1024 * 1024
    assert LOG_BACKUP_COUNT == 3
    session = configure_debug_logging(log_file, stderr=stderr)
    assert isinstance(session, DebugLoggingSession)
    owned = [
        handler
        for handler in project_logger.handlers
        if getattr(handler, "_agent_search_gateway_debug_owned", False)
    ]
    assert len(owned) == 2
    assert (
        sum(
            isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, RotatingFileHandler)
            for handler in owned
        )
        == 1
    )
    assert sum(isinstance(handler, RotatingFileHandler) for handler in owned) == 1
    assert project_logger.level == logging.DEBUG
    assert project_logger.propagate is False
    assert logging.getLogger("httpx").level == httpx_level
    assert logging.getLogger("httpcore").level == httpcore_level

    log_event(project_logger, logging.DEBUG, "session_started", pid=1)
    session.close()
    session.close()

    assert project_logger.level == original_level
    assert project_logger.propagate == original_propagate
    assert tuple(project_logger.handlers) == original_handlers
    assert "event=session_started" in log_file.read_text(encoding="utf-8")


def test_debug_log_appends_across_restart_and_rotates_with_bounded_backups(tmp_path: Path) -> None:
    log_file = tmp_path / "debug.log"
    first_stderr = io.StringIO()
    first = configure_debug_logging(log_file, stderr=first_stderr, max_bytes=220, backup_count=3)
    project_logger = logging.getLogger("agent_search_gateway")
    try:
        log_event(project_logger, logging.DEBUG, "first_session", marker="before-restart")
    finally:
        first.close()

    second = configure_debug_logging(log_file, stderr=io.StringIO(), max_bytes=220, backup_count=3)
    try:
        log_event(project_logger, logging.DEBUG, "second_session", marker="after-restart")
        appended = log_file.read_text(encoding="utf-8")
        assert "after-restart" in appended
        assert "before-restart" in appended

        for index in range(40):
            log_event(
                project_logger,
                logging.DEBUG,
                "rotation_probe",
                index=index,
                payload="x" * 80,
            )
    finally:
        second.close()

    assert log_file.exists()
    assert (tmp_path / "debug.log.1").exists()
    assert (tmp_path / "debug.log.2").exists()
    assert (tmp_path / "debug.log.3").exists()
    assert not (tmp_path / "debug.log.4").exists()


def test_configure_close_configure_does_not_duplicate_owned_handlers(tmp_path: Path) -> None:
    project_logger = logging.getLogger("agent_search_gateway")
    baseline = tuple(project_logger.handlers)
    log_file = tmp_path / "debug.log"

    first = configure_debug_logging(log_file, stderr=io.StringIO())
    assert len(project_logger.handlers) == len(baseline) + 2
    first.close()
    assert tuple(project_logger.handlers) == baseline

    second = configure_debug_logging(log_file, stderr=io.StringIO())
    assert len(project_logger.handlers) == len(baseline) + 2
    second.close()
    assert tuple(project_logger.handlers) == baseline


@pytest.mark.parametrize(
    ("max_bytes", "backup_count"),
    [(0, LOG_BACKUP_COUNT), (LOG_MAX_BYTES, -1)],
)
def test_configure_debug_logging_rejects_invalid_rotation_limits(
    tmp_path: Path,
    max_bytes: int,
    backup_count: int,
) -> None:
    with pytest.raises(ConfigFailure) as failure:
        configure_debug_logging(
            tmp_path / "debug.log",
            stderr=io.StringIO(),
            max_bytes=max_bytes,
            backup_count=backup_count,
        )

    assert failure.value.code is ErrorCode.CONFIG_ERROR


def test_logging_setup_failure_is_config_error_and_restores_logger(tmp_path: Path) -> None:
    project_logger = logging.getLogger("agent_search_gateway")
    original_level = project_logger.level
    original_propagate = project_logger.propagate
    original_handlers = tuple(project_logger.handlers)

    def failing_factory(
        log_file: Path,
        *,
        stderr: io.StringIO,
        max_bytes: int,
        backup_count: int,
    ) -> logging.Handler:
        raise OSError("synthetic setup failure")

    with pytest.raises(ConfigFailure) as failure:
        configure_debug_logging(
            tmp_path / "debug.log",
            stderr=io.StringIO(),
            file_handler_factory=failing_factory,
        )

    assert failure.value.code is ErrorCode.CONFIG_ERROR
    assert "synthetic setup failure" not in failure.value.message
    assert project_logger.level == original_level
    assert project_logger.propagate == original_propagate
    assert tuple(project_logger.handlers) == original_handlers


def test_emit_failure_is_swallowed_and_reports_directly_to_stderr(tmp_path: Path) -> None:
    class ExplodingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise OSError("synthetic emit failure")

    def exploding_factory(
        log_file: Path,
        *,
        stderr: io.StringIO,
        max_bytes: int,
        backup_count: int,
    ) -> logging.Handler:
        return ExplodingHandler()

    stderr = io.StringIO()
    session = configure_debug_logging(
        tmp_path / "debug.log",
        stderr=stderr,
        file_handler_factory=exploding_factory,
    )
    logger = logging.getLogger("agent_search_gateway")

    log_event(logger, logging.DEBUG, "will_not_break_business", value=1)
    session.close()

    assert "debug logging sink failure" in stderr.getvalue()


def test_custom_handler_failure_does_not_change_business_return() -> None:
    class BrokenHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise RuntimeError("PRIVATE_SINK_DATA")

    logger = logging.getLogger("tests.observability.custom_failure")
    logger.handlers[:] = [BrokenHandler()]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    def business() -> str:
        log_event(logger, logging.DEBUG, "provider_response_diagnostic", response_bytes=42)
        return "business result"

    assert business() == "business result"


def test_formatter_failure_uses_safe_emergency_output_and_keeps_business_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(self: KeyValueFormatter, record: logging.LogRecord) -> str:
        raise ValueError("PRIVATE_FORMATTER_DATA")

    stderr = io.StringIO()
    session = configure_debug_logging(tmp_path / "debug.log", stderr=stderr)
    monkeypatch.setattr(KeyValueFormatter, "format", broken)
    try:

        def business() -> str:
            log_event(logging.getLogger("agent_search_gateway"), logging.DEBUG, "diagnostic")
            return "business result"

        assert business() == "business result"
    finally:
        session.close()
    output = stderr.getvalue() + capsys.readouterr().err
    assert "PRIVATE_FORMATTER_DATA" not in output
    assert "debug logging sink failure" in output


@pytest.mark.parametrize("origin", ["handler", "formatter"])
def test_logging_cancellation_is_never_swallowed(origin: str) -> None:
    class CancelFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            raise asyncio.CancelledError

    class CancelHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise asyncio.CancelledError

    logger = logging.getLogger("tests.observability.cancellation")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if origin == "handler":
        logger.handlers[:] = [CancelHandler()]
    else:
        handler = logging.StreamHandler(io.StringIO())
        handler.setFormatter(CancelFormatter())
        logger.handlers[:] = [handler]
    with pytest.raises(asyncio.CancelledError):
        log_event(logger, logging.DEBUG, "provider_response_diagnostic")
