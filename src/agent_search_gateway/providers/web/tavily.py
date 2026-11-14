"""Tavily search and extract adapter."""

import logging
from collections.abc import Mapping

import httpx

from ...errors import ExecutionFailure
from ...observability import SecretValue, log_event
from ...providers.contracts import KeywordSearchHit, URLFetchCandidate
from ...url_normalization import NormalizedURL
from ..diagnostics import CandidateCounts, best_effort_diagnostics
from .common import (
    JsonRequester,
    candidate_rejection_reason,
    endpoint,
    failure,
    json_search_response_diagnostics,
    non_empty_string,
    normalized_match,
    optional_string,
    require_list,
    require_object,
)


def _search_response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    return json_search_response_diagnostics(
        response, results_key="results", include_invalid_results_kind=False
    )


def _fetch_response_diagnostics(
    response: httpx.Response, target: NormalizedURL
) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    return _fetch_payload_diagnostics(payload, target)


def _fetch_payload_diagnostics(payload: object, target: NormalizedURL) -> Mapping[str, object]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    empty_match = False
    for item in payload["results"]:
        try:
            if not isinstance(item, dict) or not normalized_match(
                item.get("url"), target, "tavily", "fetch"
            ):
                continue
        except ExecutionFailure:
            continue
        raw = item.get("raw_content")
        if isinstance(raw, str) and raw.strip():
            return {"response_kind": "object", "semantic_result": "success"}
        empty_match = True
    return {"response_kind": "object", "semantic_result": "empty" if empty_match else "no_match"}


class TavilyAdapter:
    def __init__(
        self,
        *,
        name: str,
        api_url: str,
        secret: SecretValue,
        http_executor: JsonRequester,
    ) -> None:
        self.name = name
        self._api_url = api_url
        self._secret = secret
        self._http = http_executor

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._secret.reveal()}"}

    async def search(self, query: str) -> list[KeywordSearchHit]:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/search"),
            stage="search",
            response_diagnostics=_search_response_diagnostics,
            headers=self._headers,
            json_body={"query": query, "include_raw_content": "markdown"},
        )
        root = require_object(payload, self.name, "search", "response")
        results = require_list(root.get("results"), self.name, "search", "results")
        counts = CandidateCounts(len(results))
        hits: list[KeywordSearchHit] = []
        for item in results:
            try:
                result = require_object(item, self.name, "search", "result")
                url = non_empty_string(result.get("url"), self.name, "search", "result.url")
                hits.append(
                    KeywordSearchHit(
                        url=url,
                        title=optional_string(
                            result.get("title"), self.name, "search", "result.title"
                        ),
                        snippet=optional_string(
                            result.get("content"), self.name, "search", "result.content"
                        ),
                        raw_content=optional_string(
                            result.get("raw_content"),
                            self.name,
                            "search",
                            "result.raw_content",
                        ),
                    )
                )
            except ExecutionFailure:
                counts.reject(candidate_rejection_reason(item, "url"))
                continue
            counts.accept()
        log_event(
            logging.getLogger(__name__),
            logging.DEBUG,
            "provider_candidates_summary",
            provider=self.name,
            stage="search",
            phase="adapter",
            exc_info=False,
            **counts.as_fields(),
        )
        return hits

    async def fetch(self, url: NormalizedURL) -> URLFetchCandidate:
        payload = await self._http.request_json(
            "POST",
            endpoint(self._api_url, "/extract"),
            stage="fetch",
            response_diagnostics=lambda response: _fetch_response_diagnostics(response, url),
            headers=self._headers,
            json_body={"urls": [str(url)]},
        )
        diagnostics = best_effort_diagnostics(lambda: _fetch_payload_diagnostics(payload, url))
        try:
            root = require_object(payload, self.name, "fetch", "response")
            results = require_list(root.get("results"), self.name, "fetch", "results")
        except ExecutionFailure as exc:
            reason = "invalid_results_envelope"
            raise failure(
                self.name, "fetch", reason, reason_code=reason, diagnostics=diagnostics
            ) from exc

        empty_match_seen = False
        processed = invalid_records = unmatched = empty_matches = 0
        try:
            for item in results:
                processed += 1
                try:
                    result = require_object(item, self.name, "fetch", "result")
                    if not normalized_match(result.get("url"), url, self.name, "fetch"):
                        unmatched += 1
                        continue
                except ExecutionFailure:
                    invalid_records += 1
                    continue
                raw = result.get("raw_content")
                if not isinstance(raw, str) or not raw.strip():
                    empty_match_seen = True
                    empty_matches += 1
                    continue
                return URLFetchCandidate(raw_content=raw)
            reason = "empty_raw_content" if empty_match_seen else "no_matching_result"
            raise failure(self.name, "fetch", reason, reason_code=reason, diagnostics=diagnostics)
        finally:
            log_event(
                logging.getLogger(__name__),
                logging.DEBUG,
                "provider_fetch_summary",
                provider=self.name,
                stage="fetch",
                phase="adapter",
                collection="results",
                received=len(results),
                processed=processed,
                unprocessed=len(results) - processed,
                invalid_records=invalid_records,
                unmatched=unmatched,
                empty_matches=empty_matches,
            )
