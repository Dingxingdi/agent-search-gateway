"""Unpaywall DOI resolver used only for post-deduplication OA enrichment."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote

import httpx

from ...academic.normalization import normalize_doi
from ...errors import InputFailure
from ...models import OAResolution
from ...observability import SecretValue
from ...url_normalization import NormalizedURL, normalize_url
from ..diagnostics import CandidateCounts
from ..http import HttpStatusFailure
from .common import AcademicHttpExecutor, as_list, as_mapping, join_url, protocol_failure, text

_DEFAULT_API_URL = "https://api.unpaywall.org/v2"


def _response_diagnostics(response: httpx.Response) -> Mapping[str, object]:
    if response.status_code == 404:
        return {"semantic_result": "no_match"}
    if not 200 <= response.status_code < 300:
        return {"category": "http_status", "semantic_result": "error"}
    try:
        envelope = as_mapping(response.json())
    except ValueError:
        return {"category": "decode", "semantic_result": "unknown"}
    if envelope is None or not isinstance(envelope.get("is_oa"), bool):
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    best = envelope.get("best_oa_location")
    alternates = as_list(envelope.get("oa_locations", []))
    if best is not None and not isinstance(best, dict):
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    if alternates is None:
        return {"category": "invalid_envelope", "semantic_result": "unknown"}
    locations = ([best] if best is not None else []) + alternates
    counts = CandidateCounts(len(locations))
    for value in locations:
        location = as_mapping(value)
        if location is None:
            counts.reject("invalid_record_shape")
        elif UnpaywallResolver._location_has_url(location):
            counts.accept()
        elif any(text(location.get(key)) for key in ("url_for_landing_page", "url", "url_for_pdf")):
            counts.reject("invalid_url")
        else:
            counts.reject("missing_url")
    return {
        "semantic_result": "success",
        "response_kind": "object",
        "phase": "adapter",
        **counts.as_fields(),
    }


class UnpaywallResolver:
    name = "unpaywall"

    def __init__(
        self,
        executor: AcademicHttpExecutor,
        *,
        contact_email: SecretValue,
        api_url: str = _DEFAULT_API_URL,
    ) -> None:
        self._executor = executor
        self._contact = contact_email
        self._api_url = api_url

    async def resolve(self, doi: str) -> OAResolution | None:
        canonical_doi = normalize_doi(doi)
        if canonical_doi is None:
            raise protocol_failure(
                self.name,
                "invalid DOI supplied to resolver",
                stage="oa_resolve",
            )
        reveal = self._contact.reveal
        encoded_doi = quote(canonical_doi, safe="")
        try:
            payload = await self._executor.request_json(
                "GET",
                join_url(self._api_url, encoded_doi),
                stage="oa_resolve",
                params={"email": reveal()},
                response_diagnostics=_response_diagnostics,
            )
        except HttpStatusFailure as exc:
            if exc.status_code == 404:
                return None
            raise
        return self._map_payload(payload)

    def _map_payload(self, payload: object) -> OAResolution:
        envelope = as_mapping(payload)
        raw_is_open_access = envelope.get("is_oa") if envelope is not None else None
        if envelope is None or not isinstance(raw_is_open_access, bool):
            raise protocol_failure(
                self.name,
                "response OA envelope was invalid",
                stage="oa_resolve",
                reason="invalid_envelope",
                diagnostics={"category": "invalid_envelope", "semantic_result": "unknown"},
            )
        is_open_access = raw_is_open_access
        oa_status = text(envelope.get("oa_status"))
        best_raw = envelope.get("best_oa_location")
        if best_raw is not None and not isinstance(best_raw, dict):
            raise protocol_failure(
                self.name,
                "response best OA location was invalid",
                stage="oa_resolve",
                reason="invalid_best_location",
                diagnostics={"category": "invalid_envelope", "semantic_result": "unknown"},
            )
        alternates_raw = envelope.get("oa_locations", [])
        alternates = as_list(alternates_raw)
        if alternates is None:
            raise protocol_failure(
                self.name,
                "response OA locations were invalid",
                stage="oa_resolve",
                reason="invalid_locations",
                diagnostics={"category": "invalid_envelope", "semantic_result": "unknown"},
            )
        best = as_mapping(best_raw)
        chosen = self._choose_location(best, alternates)
        landing_url = self._location_url(chosen, landing=True)
        pdf_url = self._location_url(chosen, landing=False)
        license_value = text(chosen.get("license")) if chosen is not None else ""
        return OAResolution(
            landing_url=landing_url,
            pdf_url=pdf_url,
            is_open_access=is_open_access,
            oa_status=oa_status,
            license=license_value,
        )

    def _choose_location(
        self,
        best: Mapping[str, object] | None,
        alternates: list[object],
    ) -> Mapping[str, object] | None:
        if best is not None and self._location_has_url(best):
            return best
        candidates = [
            mapping
            for value in alternates
            if (mapping := as_mapping(value)) is not None and self._location_has_url(mapping)
        ]
        candidates.sort(key=self._location_sort_key)
        return candidates[0] if candidates else best

    @classmethod
    def _location_has_url(cls, location: Mapping[str, object]) -> bool:
        return (
            cls._location_url(location, landing=True) is not None
            or cls._location_url(location, landing=False) is not None
        )

    @classmethod
    def _location_sort_key(cls, location: Mapping[str, object]) -> tuple[int, str, str]:
        pdf = cls._location_url(location, landing=False)
        landing = cls._location_url(location, landing=True)
        return (
            0 if pdf is not None else 1,
            str(pdf) if pdf is not None else "",
            str(landing) if landing is not None else "",
        )

    @classmethod
    def _location_url(
        cls,
        location: Mapping[str, object] | None,
        *,
        landing: bool,
    ) -> NormalizedURL | None:
        if location is None:
            return None
        raw_values = (
            (
                text(location.get("url_for_landing_page")),
                text(location.get("url")),
            )
            if landing
            else (text(location.get("url_for_pdf")),)
        )
        for raw in raw_values:
            if not raw:
                continue
            try:
                return normalize_url(raw)
            except InputFailure:
                continue
        return None
