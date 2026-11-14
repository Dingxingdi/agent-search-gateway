import pytest

from agent_search_gateway.errors import (
    UNAVAILABLE_MESSAGE,
    ConfigFailure,
    ErrorCode,
    ExecutionFailure,
    GatewayError,
    InputFailure,
    ProtocolFailure,
)
from agent_search_gateway.models import ErrorResponse
from agent_search_gateway.protocol import encode_response


def test_error_contract_contains_exact_codes_and_unavailable_message() -> None:
    assert {code.value for code in ErrorCode} == {
        "bad_request",
        "empty_query",
        "invalid_url",
        "url_not_admitted",
        "no_keyword_search_providers",
        "no_llm_search_providers",
        "no_agent_search_providers",
        "no_url_fetch_providers",
        "no_academic_search_providers",
        "all_providers_failed",
        "llm_stage_failed",
        "protocol_error",
        "config_error",
        "daemon_shutting_down",
    }
    assert (
        UNAVAILABLE_MESSAGE
        == "URL unavailable: it may have failed content validation or been flagged as unsafe."
    )

    error = GatewayError(ErrorCode.BAD_REQUEST, "bad request")
    assert error.code is ErrorCode.BAD_REQUEST
    assert error.message == "bad request"
    assert str(error) == "bad request"

    for failure_type in (InputFailure, ExecutionFailure, ProtocolFailure, ConfigFailure):
        failure = failure_type(ErrorCode.PROTOCOL_ERROR, "failure")
        assert isinstance(failure, GatewayError)
        assert failure.code is ErrorCode.PROTOCOL_ERROR

    assert not hasattr(ErrorCode, "SEMANTIC_REJECTION")


def test_execution_failure_defaults_to_an_isolated_diagnostics_mapping() -> None:
    first = ExecutionFailure(ErrorCode.PROTOCOL_ERROR, "public message")
    second = ExecutionFailure(ErrorCode.PROTOCOL_ERROR, "public message")
    assert getattr(first, "diagnostics", None) == {}
    assert first.diagnostics is not second.diagnostics


@pytest.mark.parametrize("failure_type", [ExecutionFailure, ProtocolFailure])
def test_failure_diagnostics_are_copied_and_stay_out_of_public_frames(
    failure_type: type[ExecutionFailure],
) -> None:
    fields: dict[str, object] = {"upstream_message": "PRIVATE_DIAGNOSTIC"}
    failure = failure_type(
        ErrorCode.PROTOCOL_ERROR, "public message", reason="decode", diagnostics=fields
    )
    fields["upstream_message"] = "changed"
    assert failure.diagnostics == {"upstream_message": "PRIVATE_DIAGNOSTIC"}
    assert failure.reason == "decode"
    assert str(failure) == failure.message == "public message"
    assert repr(failure) == f"{failure_type.__name__}('public message')"
    assert failure.args == ("public message",)
    assert encode_response(ErrorResponse(failure.code, failure.message)) == (
        b'{"ok":false,"error":"protocol_error","message":"public message"}\n'
    )
