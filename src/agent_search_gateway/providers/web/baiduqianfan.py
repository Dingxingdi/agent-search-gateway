"""Baidu Qianfan Web Search and Agentic Search keyword adapter."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

from ...errors import ExecutionFailure, InputFailure
from ...observability import SecretValue, log_event
from ...url_normalization import normalize_url
from ..contracts import KeywordSearchHit
from ..diagnostics import CandidateCounts, diagnostic_fields_from_failure, safe_failure_reason
from ..http import ProviderResponseFailure
from .common import (
    JsonRequester,
    candidate_rejection_reason,
    configured_string,
    endpoint,
    failure,
    non_empty_string,
    optional_string,
    require_list,
    require_object,
)


def _response_diagnostics(response: httpx.Response) -> dict[str, object]:
    """Preserve known QPS errors while the HTTP executor handles Retry-After."""
    if response.status_code != 429:
        return {}
    try:
        payload = response.json()
    except (ValueError, RecursionError):
        return {}
    if isinstance(payload, dict) and payload.get("code") == "RATE_LIMIT_SEARCH_QPS":
        return {"upstream_code": "RATE_LIMIT_SEARCH_QPS"}
    return {}


class BaiduQianfanAdapter:
    def __init__(
        self,
        *,
        name: str,
        secret: SecretValue,
        http_executor: JsonRequester,
        api_url: str = "https://qianfan.baidubce.com",
    ) -> None:
        self.name = name
        self._api_url = configured_string(api_url, "api_url")
        self._secret = secret
        self._http = http_executor

    async def search(self, query: str) -> list[KeywordSearchHit]:
        async def run_branch(
            search_branch: Callable[[str], Awaitable[list[KeywordSearchHit]]],
        ) -> list[KeywordSearchHit] | Exception:
            try:
                return await search_branch(query)
            except Exception as exc:
                return exc

        web = asyncio.create_task(run_branch(self._search_web))
        agentic = asyncio.create_task(run_branch(self._search_agentic))
        tasks = (web, agentic)
        try:
            for finished in asyncio.as_completed(tasks):
                await finished
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done() and not task.cancelling():
                    task.cancel()
            cleanup = asyncio.gather(*tasks, return_exceptions=True)
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Repeated caller cancellation must let branch finalizers finish.
                    continue
            raise
        results = web.result(), agentic.result()
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
        for branch, result in zip(("web_search", "agentic_search"), results, strict=True):
            if isinstance(result, Exception):
                log_event(
                    logging.getLogger(__name__),
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

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._secret.reveal()}"}

    async def _request(self, stage: str, body: dict[str, object]) -> dict[str, object]:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, f"/v2/ai_search/{stage}"),
            stage=stage,
            headers=self._headers,
            json_body=body,
            response_inspector=lambda response: self._inspect_response(stage, response),
            response_diagnostics=_response_diagnostics,
        )
        return require_object(payload, self.name, stage, "response")

    def _inspect_response(
        self, stage: str, response: httpx.Response
    ) -> ProviderResponseFailure | None:
        if response.status_code not in (200, 429):
            return None
        try:
            payload = response.json()
        except (ValueError, RecursionError):
            return None
        if not isinstance(payload, dict) or "code" not in payload:
            return None
        code = payload["code"]
        reason = "business_error"
        diagnostics: dict[str, object] = {}
        if response.status_code == 429:
            if code not in ("QUOTA_USER_DAILY_FREE", "BILLING_INSUFFICIENT_BALANCE"):
                return None
            diagnostics["upstream_code"] = code
        elif type(code) not in (int, str) or (isinstance(code, str) and not code.strip()):
            reason = "invalid_envelope"
        elif code in ("InvalidArgument", "InternalError", 216003):
            diagnostics["upstream_code"] = code
        return ProviderResponseFailure(
            self.name,
            stage,
            status_code=response.status_code,
            retryable=False,
            safe_reason=reason,
            diagnostics=diagnostics,
        )

    async def _search_web(self, query: str) -> list[KeywordSearchHit]:
        stage = "web_search"
        root = await self._request(
            stage,
            {
                "messages": [{"role": "user", "content": query}],
                "search_source": "baidu_search_v2",
                "edition": "standard",
                "resource_type_filter": [
                    {"type": "web", "top_k": 20},
                    {"type": "image", "top_k": 10},
                    {"type": "video", "top_k": 10},
                ],
            },
        )
        references = require_list(root.get("references"), self.name, stage, "references")
        counts = CandidateCounts(len(references))
        hits: list[KeywordSearchHit] = []
        for item in references:
            url_source = item
            try:
                record = require_object(item, self.name, stage, "result")
                kind = record.get("type")
                url_source = self._web_url_source(record, kind)
                label = {"image": "Image", "video": "Video"}.get(str(kind), "")
                hits.append(self._hit(record, url=url_source.get("url"), stage=stage, label=label))
            except (ExecutionFailure, InputFailure):
                counts.reject(candidate_rejection_reason(url_source, "url"))
                continue
            counts.accept()
        self._log_counts(stage, counts)
        return hits

    def _log_counts(self, stage: str, counts: CandidateCounts) -> None:
        log_event(
            logging.getLogger(__name__),
            logging.DEBUG,
            "provider_candidates_summary",
            provider=self.name,
            stage=stage,
            phase="adapter",
            exc_info=False,
            **counts.as_fields(),
        )

    def _web_url_source(self, record: dict[str, object], kind: object) -> dict[str, object]:
        url = record.get("url")
        if (url is None or (isinstance(url, str) and not url.strip())) and kind in (
            "image",
            "video",
        ):
            return require_object(record.get(str(kind)), self.name, "web_search", f"result.{kind}")
        return record

    async def _search_agentic(self, query: str) -> list[KeywordSearchHit]:
        stage = "agentic_search"
        root = await self._request(stage, {"query": query, "count": 10})
        groups: list[tuple[str, list[object]]] = []
        for container, key, label in (
            ("web_page_result", "web_pages", ""),
            ("image_result", "images", "Image"),
            ("video_result", "videos", "Video"),
            ("card_result", "cards", ""),
        ):
            if container not in root:
                continue
            group = require_object(root[container], self.name, stage, container)
            if key in ("images", "videos", "cards") and key in group and group[key] is None:
                groups.append((label, []))
                continue
            items = require_list(group.get(key), self.name, stage, key)
            if container != "card_result":
                groups.append((label, items))
        if not groups and "card_result" not in root:
            raise failure(self.name, stage, "response is missing a result container")

        counts = CandidateCounts(sum(len(items) for _, items in groups))
        hits: list[KeywordSearchHit] = []
        for label, items in groups:
            for item in items:
                try:
                    hits.append(self._agentic_hit(item, label))
                except (ExecutionFailure, InputFailure):
                    counts.reject(candidate_rejection_reason(item, "url"))
                    continue
                counts.accept()
        self._log_counts(stage, counts)
        return hits

    def _agentic_hit(self, item: object, label: str) -> KeywordSearchHit:
        stage = "agentic_search"
        record = require_object(item, self.name, stage, "result")
        raw_content = ""
        if not label:
            raw_content = optional_string(record.get("content"), self.name, stage, "result.content")
        return self._hit(
            record, url=record.get("url"), stage=stage, label=label, raw_content=raw_content
        )

    def _hit(
        self,
        record: dict[str, object],
        *,
        url: object,
        stage: str,
        label: str,
        raw_content: str = "",
    ) -> KeywordSearchHit:
        original_url = non_empty_string(url, self.name, stage, "result.url")
        normalize_url(original_url)
        title = optional_string(record.get("title"), self.name, stage, "result.title")
        snippet = optional_string(record.get("snippet"), self.name, stage, "result.snippet")
        return KeywordSearchHit(
            url=original_url,
            title=label if label and not title.strip() else title,
            snippet=label if label and not snippet.strip() else snippet,
            raw_content=raw_content,
        )
