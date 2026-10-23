from agent_search_gateway.errors import (
    UNAVAILABLE_MESSAGE,
    ConfigFailure,
    ErrorCode,
    ExecutionFailure,
    GatewayError,
    InputFailure,
    ProtocolFailure,
)


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
