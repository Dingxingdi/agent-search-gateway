"""HTTP executor test double for provider adapter contract tests."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    method: str
    url: str
    stage: str
    headers: Mapping[str, str] | None
    params: Mapping[str, object] | None
    json_body: object | None
    response_mode: str


class RecordingJsonExecutor:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.requests: list[RecordedRequest] = []

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        json_body: object | None = None,
    ) -> object:
        self.requests.append(
            RecordedRequest(method, url, stage, headers, params, json_body, "json")
        )
        return self._pop_response()

    async def request_text(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        json_body: object | None = None,
    ) -> str:
        self.requests.append(
            RecordedRequest(method, url, stage, headers, params, json_body, "text")
        )
        response = self._pop_response()
        if not isinstance(response, str):
            raise AssertionError("expected text response")
        return response

    def _pop_response(self) -> object:
        if not self._responses:
            raise AssertionError("unexpected HTTP request")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class RecordingTextExecutor:
    def __init__(self, responses: list[str | BaseException]) -> None:
        self._responses = list(responses)
        self.requests: list[RecordedRequest] = []

    async def request_text(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        json_body: object | None = None,
    ) -> str:
        self.requests.append(
            RecordedRequest(method, url, stage, headers, params, json_body, "text")
        )
        if not self._responses:
            raise AssertionError("unexpected HTTP request")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class RecordingHttpExecutor:
    def __init__(
        self,
        *,
        json_responses: list[object] | None = None,
        text_responses: list[str | BaseException] | None = None,
    ) -> None:
        self._json_responses = list(json_responses or [])
        self._text_responses = list(text_responses or [])
        self.requests: list[RecordedRequest] = []

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        json_body: object | None = None,
    ) -> object:
        self.requests.append(
            RecordedRequest(method, url, stage, headers, params, json_body, "json")
        )
        if not self._json_responses:
            raise AssertionError("unexpected JSON HTTP request")
        response = self._json_responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def request_text(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        json_body: object | None = None,
    ) -> str:
        self.requests.append(
            RecordedRequest(method, url, stage, headers, params, json_body, "text")
        )
        if not self._text_responses:
            raise AssertionError("unexpected text HTTP request")
        response = self._text_responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response
