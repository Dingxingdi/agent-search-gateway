"""Stable gateway error taxonomy and user-facing constants."""

from collections.abc import Mapping
from enum import StrEnum


class ErrorCode(StrEnum):
    BAD_REQUEST = "bad_request"
    EMPTY_QUERY = "empty_query"
    INVALID_URL = "invalid_url"
    URL_NOT_ADMITTED = "url_not_admitted"
    NO_KEYWORD_SEARCH_PROVIDERS = "no_keyword_search_providers"
    NO_LLM_SEARCH_PROVIDERS = "no_llm_search_providers"
    NO_AGENT_SEARCH_PROVIDERS = "no_agent_search_providers"
    NO_URL_FETCH_PROVIDERS = "no_url_fetch_providers"
    NO_ACADEMIC_SEARCH_PROVIDERS = "no_academic_search_providers"
    ALL_PROVIDERS_FAILED = "all_providers_failed"
    LLM_STAGE_FAILED = "llm_stage_failed"
    PROTOCOL_ERROR = "protocol_error"
    CONFIG_ERROR = "config_error"
    DAEMON_SHUTTING_DOWN = "daemon_shutting_down"


UNAVAILABLE_MESSAGE = (
    "URL unavailable: it may have failed content validation or been flagged as unsafe."
)


class GatewayError(Exception):
    """Base typed failure crossing orchestration boundaries."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason


class InputFailure(GatewayError):
    """Invalid user input or request-state combination."""


class ExecutionFailure(GatewayError):
    """Dependency or execution-path failure."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        reason: str | None = None,
        diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(code, message, reason=reason)
        self.diagnostics: Mapping[str, object] = (
            dict(diagnostics) if diagnostics is not None else {}
        )


class ProtocolFailure(ExecutionFailure):
    """Malformed local or provider protocol data."""


class ConfigFailure(GatewayError):
    """Invalid daemon startup configuration."""


class ParserFailure(ExecutionFailure):
    """Restricted parser rejected an execution result."""

    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.PROTOCOL_ERROR, message)


class DaemonUnavailable(Exception):
    """The local daemon socket is missing or refusing connections."""
