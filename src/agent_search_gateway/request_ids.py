"""Request identifiers used for workflow correlation and search result names."""

import re
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Literal

RequestIdFactory = Callable[[], str]
ResultKind = Literal["keyword", "llm", "paper", "agent"]

_RESULT_KINDS: tuple[ResultKind, ...] = ("keyword", "llm", "paper", "agent")
_REQUEST_ID_PATTERN = re.compile(r"[0-9a-f]{8}")
_REQUEST_ID: ContextVar[str | None] = ContextVar("agent_search_gateway_request_id", default=None)


def validate_request_id(value: str) -> str:
    if _REQUEST_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("request ID must be exactly 8 lowercase hexadecimal characters")
    return value


def generate_request_id() -> str:
    return secrets.token_hex(4)


def current_request_id() -> str | None:
    return _REQUEST_ID.get()


@contextmanager
def bind_request_id(request_id: str) -> Iterator[None]:
    token = _REQUEST_ID.set(validate_request_id(request_id))
    try:
        yield
    finally:
        _REQUEST_ID.reset(token)


def result_filename(kind: ResultKind, request_id: str) -> str:
    if kind not in _RESULT_KINDS:
        raise ValueError(f"invalid result kind: {kind}")
    return f"{kind}-{validate_request_id(request_id)}.jsonl"


class RequestIdRegistry:
    def __init__(
        self,
        results_dir: Path,
        *,
        factory: RequestIdFactory = generate_request_id,
        max_attempts: int = 256,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self._results_dir = results_dir
        self._factory = factory
        self._max_attempts = max_attempts
        self._active: set[str] = set()

    @contextmanager
    def reserve(self, *, may_write_search_result: bool) -> Iterator[str]:
        request_id = self._select_available(may_write_search_result=may_write_search_result)
        self._active.add(request_id)
        try:
            yield request_id
        finally:
            self._active.remove(request_id)

    def _select_available(self, *, may_write_search_result: bool) -> str:
        for _ in range(self._max_attempts):
            candidate = self._next_candidate()
            if candidate in self._active:
                continue
            if may_write_search_result and self._has_result_collision(candidate):
                continue
            return candidate
        raise RuntimeError("unable to reserve an available request ID")

    def _next_candidate(self) -> str:
        try:
            candidate = self._factory()
        except Exception as exc:
            raise RuntimeError("request ID factory failed") from exc
        try:
            return validate_request_id(candidate)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("invalid request ID returned by factory") from exc

    def _has_result_collision(self, request_id: str) -> bool:
        return any(
            (self._results_dir / result_filename(kind, request_id)).exists()
            for kind in _RESULT_KINDS
        )
