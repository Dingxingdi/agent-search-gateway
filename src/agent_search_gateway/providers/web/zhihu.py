"""Zhihu dual-endpoint keyword search adapter."""

import asyncio
import logging
import time
from urllib.parse import urlencode

import httpx

from ...errors import ExecutionFailure
from ...observability import SecretValue, log_event
from ..contracts import KeywordSearchHit
from ..diagnostics import (
    CandidateCounts,
    best_effort_diagnostics,
    diagnostic_fields_from_failure,
    safe_diagnostic_fields,
    safe_failure_reason,
)
from .common import (
    JsonRequester,
    configured_string,
    endpoint,
    failure,
    non_empty_string,
    optional_string,
    require_list,
    require_object,
    selected_error_fields,
)

_LOGGER = logging.getLogger(__name__)
_BRANCHES = ("zhihu_search", "global_search")


def _error_diagnostics(root: dict[str, object]) -> dict[str, object]:
    return best_effort_diagnostics(
        lambda: selected_error_fields({"code": root.get("Code"), "message": root.get("Message")})
    )


def _response_diagnostics(response: httpx.Response) -> dict[str, object]:
    """Classify known envelope fields without parsing hits or changing retry handling."""
    try:
        payload = response.json()
    except ValueError:
        return safe_diagnostic_fields({"category": "decode", "semantic_result": "unknown"})
    invalid_envelope = {"category": "invalid_envelope", "semantic_result": "unknown"}
    if not isinstance(payload, dict) or type(payload.get("Code")) is not int:
        return safe_diagnostic_fields(invalid_envelope)
    if payload["Code"] != 0:
        return safe_diagnostic_fields(
            {
                "category": "provider_response",
                "semantic_result": "error",
                "response_kind": "object",
                **_error_diagnostics(payload),
            }
        )
    data = payload.get("Data")
    if not isinstance(data, dict) or not isinstance(data.get("Items"), list):
        return safe_diagnostic_fields(invalid_envelope)
    return safe_diagnostic_fields({"semantic_result": "success", "response_kind": "object"})


class ZhihuAdapter:
    def __init__(
        self,
        *,
        name: str,
        secret: SecretValue,
        http_executor: JsonRequester,
        api_url: str = "https://developer.zhihu.com",
    ) -> None:
        self.name = name
        self._api_url = configured_string(api_url, "api_url")
        self._secret = secret
        self._http = http_executor

    async def search(self, query: str) -> list[KeywordSearchHit]:
        async def run_branch(branch: str) -> list[KeywordSearchHit] | Exception:
            try:
                return await self._search_branch(query, branch)
            except Exception as exc:
                return exc

        site = asyncio.create_task(run_branch(_BRANCHES[0]))
        global_search = asyncio.create_task(run_branch(_BRANCHES[1]))
        try:
            results = await asyncio.gather(site, global_search)
        except asyncio.CancelledError:
            for task in (site, global_search):
                # gather may already have cancelled a branch that is still cleaning up.
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(site, global_search, return_exceptions=True)
            raise
        first, second = results
        if isinstance(first, Exception) and isinstance(second, Exception):
            raise failure(
                self.name,
                "search",
                "both search branches failed",
                reason_code=safe_failure_reason(first),
                diagnostics=diagnostic_fields_from_failure(first),
            ) from first
        hits: list[KeywordSearchHit] = []
        for branch, result in zip(_BRANCHES, results, strict=True):
            if isinstance(result, Exception):
                log_event(
                    _LOGGER,
                    logging.WARNING,
                    "provider_partial_failure",
                    provider=self.name,
                    stage="search",
                    branch=branch,
                    error_type=type(result).__name__,
                    reason=safe_failure_reason(result),
                    exc_info=False,
                    **diagnostic_fields_from_failure(result),
                )
            else:
                hits.extend(result)
        return hits

    async def _search_branch(self, query: str, branch: str) -> list[KeywordSearchHit]:
        def request_headers() -> dict[str, str]:
            return {
                "Authorization": f"Bearer {self._secret.reveal()}",
                "X-Request-Timestamp": str(int(time.time())),
                "Content-Type": "application/json",
            }

        url = endpoint(self._api_url, f"/api/v1/content/{branch}")
        payload = await self._http.request_json(
            "GET",
            f"{url}?{urlencode({'Query': query})}",
            stage=branch,
            headers=request_headers,
            response_diagnostics=_response_diagnostics,
        )
        root = require_object(payload, self.name, branch, "response")
        code = root.get("Code")
        if type(code) is not int or code != 0:
            raise failure(
                self.name, branch, "provider reported failure", diagnostics=_error_diagnostics(root)
            )
        data = require_object(root.get("Data"), self.name, branch, "Data")
        items = require_list(data.get("Items"), self.name, branch, "Data.Items")
        counts = CandidateCounts(len(items))
        hits: list[KeywordSearchHit] = []
        for item in items:
            if not isinstance(item, dict):
                counts.reject("invalid_record_shape")
                continue
            try:
                hit = KeywordSearchHit(
                    url=non_empty_string(item.get("Url"), self.name, branch, "item.Url"),
                    title=optional_string(item.get("Title"), self.name, branch, "item.Title"),
                    snippet=optional_string(
                        item.get("ContentText"), self.name, branch, "item.ContentText"
                    ),
                )
            except ExecutionFailure:
                url_value = item.get("Url")
                missing_url = url_value is None or (
                    isinstance(url_value, str) and not url_value.strip()
                )
                counts.reject("missing_url" if missing_url else "invalid_record")
                continue
            hits.append(hit)
            counts.accept()
        log_event(
            _LOGGER,
            logging.DEBUG,
            "provider_candidates_summary",
            provider=self.name,
            stage=branch,
            phase="adapter",
            exc_info=False,
            **counts.as_fields(),
        )
        return hits
