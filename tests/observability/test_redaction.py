import io
import logging
from pathlib import Path

import pytest

from agent_search_gateway.observability import (
    DiagnosticText,
    SecretValue,
    configure_debug_logging,
    log_event,
)
from tests.support.logging import structured_test_logger


def test_academic_credentials_and_contact_are_redacted_from_fields_and_traceback(
    tmp_path: Path,
) -> None:
    credential = SecretValue("CREDENTIAL_PAYLOAD_SENTINEL")
    contact = SecretValue("CONTACT_PAYLOAD_SENTINEL")
    log_file = tmp_path / "debug.log"
    session = configure_debug_logging(log_file, stderr=io.StringIO())
    session.add_secrets((credential,), (contact,))
    try:
        try:
            raise RuntimeError(f"{credential.reveal()} {contact.reveal()}")
        except RuntimeError:
            log_event(
                logging.getLogger("agent_search_gateway.observability.redaction_test"),
                logging.DEBUG,
                "academic_redaction_probe",
                detail=f"{credential.reveal()} {contact.reveal()}",
                exc_info=True,
            )
    finally:
        session.close()

    text = log_file.read_text(encoding="utf-8")
    assert credential.reveal() not in text
    assert contact.reveal() not in text
    assert text.count("<redacted>") >= 4
    assert "event=academic_redaction_probe" in text
    assert "traceback=" in text


@pytest.mark.parametrize("secret", ["BOUNDARY_SECRET_MATERIAL", "MULTILINE_SECRET\nTAIL_MATERIAL"])
def test_diagnostic_text_uses_active_redactor_before_cleanup_and_truncation(
    tmp_path: Path, secret: str
) -> None:
    value = DiagnosticText("x" * 500 + secret, 512)
    assert secret not in str(value)
    assert secret not in repr(value)
    stderr = io.StringIO()
    log_file = tmp_path / "diagnostics.log"
    session = configure_debug_logging(log_file, stderr=stderr, max_bytes=350)
    # The value predates the secret registration; rendering must use this session's redactor.
    session.add_secrets(SecretValue(secret))
    try:
        for _ in range(3):
            log_event(
                logging.getLogger("agent_search_gateway.redaction_test"),
                logging.DEBUG,
                "provider_response_diagnostic",
                upstream_message=value,
            )
    finally:
        session.close()

    outputs = [stderr.getvalue(), "".join(path.read_text() for path in tmp_path.iterdir())]
    for output in outputs:
        assert "BOUNDARY_SECRET" not in output
        assert "MULTILINE_SECRET" not in output
        assert "TAIL_MATERIAL" not in output
        assert output.count("event=provider_response_diagnostic") == 3
        assert output.count("upstream_message_truncated=false") == 3
        assert "<redacted>" in output
        assert len(output.splitlines()) == 3


@pytest.mark.parametrize(
    "url",
    [
        r"https:\/\/example.test/a?session_id=PRIVATE_QUERY#PRIVATE_FRAGMENT",
        "https%3A%2F%2Fexample.test%2Fa%3Fsession_id%3DPRIVATE_QUERY%23PRIVATE_FRAGMENT",
        "https:%2F%2Fexample.test/a?customer_id=PRIVATE_QUERY#PRIVATE_FRAGMENT",
        r"https:\/\/user:CONFIGURED_CREDENTIAL@example.test/a?session_id=PRIVATE_QUERY",
    ],
)
def test_diagnostic_text_redacts_disguised_urls_without_decoding(url: str) -> None:
    secret = SecretValue("CONFIGURED_CREDENTIAL")
    logger, stream = structured_test_logger("test.redaction.disguised_url", secrets=[secret])
    log_event(
        logger,
        logging.DEBUG,
        "provider_response_diagnostic",
        upstream_message=f"provider failed at {url}",
    )
    rendered = stream.getvalue()
    assert "PRIVATE_QUERY" not in rendered
    assert "PRIVATE_FRAGMENT" not in rendered
    assert secret.reveal() not in rendered
    assert "provider failed at" in rendered
    assert "<redacted-url>" in rendered
    assert len(rendered.splitlines()) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/reset/UNREGISTERED_URL_CREDENTIAL",
        "https://UNREGISTERED_URL_CREDENTIAL.example.test/reset",
    ],
)
def test_diagnostic_text_redacts_entire_untrusted_urls(url: str) -> None:
    logger, stream = structured_test_logger("test.redaction.untrusted_url")
    log_event(
        logger,
        logging.DEBUG,
        "provider_response_diagnostic",
        upstream_message=f"request failed at {url} please retry",
    )
    rendered = stream.getvalue()
    assert "UNREGISTERED_URL_CREDENTIAL" not in rendered
    assert "example.test" not in rendered
    assert "<redacted-url>" in rendered
    assert "request failed at" in rendered
    assert "please retry" in rendered


def test_redacted_url_credentials_do_not_hide_the_remaining_signed_query() -> None:
    secret = SecretValue("CONFIGURED_CREDENTIAL")
    logger, stream = structured_test_logger("test.redaction.url_placeholder", secrets=[secret])
    message = (
        f"https://user:{secret.reveal()}@example.test/a?"
        "X-Amz-Credential=QUERY_PRIVATE_DATA#FRAGMENT_PRIVATE_DATA"
    )
    log_event(logger, logging.DEBUG, "provider_response_diagnostic", upstream_message=message)
    rendered = stream.getvalue()
    assert secret.reveal() not in rendered
    assert "QUERY_PRIVATE_DATA" not in rendered
    assert "FRAGMENT_PRIVATE_DATA" not in rendered
    assert "<redacted-url>" in rendered
    assert "upstream_message_truncated=false" in rendered
