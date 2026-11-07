"""Identifier-centric clustering and deterministic merge for academic papers."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date

from ..errors import InputFailure
from ..models import PaperIdentifiers, PaperRecord
from ..observability import log_event
from ..providers.contracts import PaperSearchHit
from ..url_normalization import NormalizedURL, normalize_url
from .normalization import (
    bibliographic_fingerprint,
    bibliographic_fingerprints_match,
    normalize_arxiv_id,
    normalize_doi,
    normalize_source_id,
    normalize_source_name,
    normalize_topic,
)

_LOGGER = logging.getLogger(__name__)
_GENERATED_PREFIXES = ("llm:", "agent:")


@dataclass(frozen=True, slots=True)
class _NormalizedHit:
    source: str
    source_id: str
    title: str
    authors: tuple[str, ...]
    abstract: str
    doi: str
    arxiv_id: str
    published_date: date | None
    updated_date: date | None
    url: NormalizedURL
    pdf_url: NormalizedURL | None
    venue: str
    topics: tuple[str, ...]
    citation_count: int | None
    is_open_access: bool | None
    oa_status: str
    license: str
    origin_rank: tuple[int, int]
    precedence: tuple[int, int, str, str]

    @property
    def source_key(self) -> tuple[str, str]:
        return self.source, self.source_id

    @property
    def fingerprint(self) -> tuple[str, frozenset[str], int] | None:
        return bibliographic_fingerprint(self.title, self.authors, self.published_date)


@dataclass(slots=True)
class _Cluster:
    candidates: list[_NormalizedHit]

    @property
    def dois(self) -> set[str]:
        return {candidate.doi for candidate in self.candidates if candidate.doi}

    @property
    def arxiv_ids(self) -> set[str]:
        return {candidate.arxiv_id for candidate in self.candidates if candidate.arxiv_id}

    @property
    def source_keys(self) -> set[tuple[str, str]]:
        return {candidate.source_key for candidate in self.candidates}

    @property
    def origin_rank(self) -> tuple[int, int, str, str]:
        best = min(
            self.candidates,
            key=lambda candidate: (
                *candidate.origin_rank,
                candidate.source,
                candidate.source_id,
            ),
        )
        return (*best.origin_rank, best.source, best.source_id)

    @property
    def has_global_strong_id(self) -> bool:
        return bool(self.dois or self.arxiv_ids)


class PaperAggregator:
    """Normalize candidates, resolve identity, and merge fields deterministically."""

    def __init__(self, provider_priority: Iterable[str]) -> None:
        normalized_priority = tuple(normalize_source_name(source) for source in provider_priority)
        self._priority = {
            source: index for index, source in enumerate(normalized_priority) if source
        }
        self._fallback_rank = len(self._priority)

    def aggregate(self, hits: Iterable[PaperSearchHit]) -> list[PaperRecord]:
        normalized = self._normalize_hits(list(hits))
        clusters: list[_Cluster] = []
        for candidate in sorted(normalized, key=self._processing_key):
            self._attach_candidate(clusters, candidate)
        ordered_clusters = sorted(clusters, key=lambda cluster: cluster.origin_rank)
        return [self._merge_cluster(cluster) for cluster in ordered_clusters]

    def _normalize_hits(self, hits: list[PaperSearchHit]) -> list[_NormalizedHit]:
        source_positions: dict[str, int] = {}
        normalized: list[_NormalizedHit] = []
        for hit in hits:
            source = normalize_source_name(hit.source) if isinstance(hit.source, str) else ""
            discovery_index = source_positions.get(source, 0)
            source_positions[source] = discovery_index + 1
            candidate = self._normalize_hit(hit, source, discovery_index)
            if candidate is not None:
                normalized.append(candidate)
        return normalized

    def _normalize_hit(
        self,
        hit: PaperSearchHit,
        source: str,
        discovery_index: int,
    ) -> _NormalizedHit | None:
        if not source:
            self._reject("unknown", "invalid_record_shape")
            return None
        title = normalize_topic(hit.title) if isinstance(hit.title, str) else ""
        if not title:
            self._reject(source, "missing_title")
            return None
        source_identity = normalize_source_id(source, hit.source_id)
        if source_identity is None:
            reason = "missing_source_id" if not str(hit.source_id).strip() else "invalid_identifier"
            self._reject(source, reason)
            return None
        _, source_id = source_identity

        doi_valid, canonical_doi = self._optional_identifier(hit.doi, normalize_doi)
        arxiv_valid, canonical_arxiv = self._optional_identifier(
            hit.arxiv_id,
            normalize_arxiv_id,
        )
        if not doi_valid or not arxiv_valid:
            self._reject(source, "invalid_identifier")
            return None
        if source == "crossref":
            if canonical_doi and canonical_doi != source_id:
                self._reject(source, "identifier_conflict")
                return None
            canonical_doi = source_id
        if source == "arxiv":
            if canonical_arxiv and canonical_arxiv != source_id:
                self._reject(source, "identifier_conflict")
                return None
            canonical_arxiv = source_id

        published_valid, published_date = self._date_or_none(hit.published_date)
        updated_valid, updated_date = self._date_or_none(hit.updated_date)
        if not published_valid or not updated_valid:
            self._reject(source, "invalid_date")
            return None
        try:
            url = normalize_url(hit.url)
        except InputFailure:
            self._reject(source, "invalid_landing_url")
            return None
        pdf_url = self._optional_url(hit.pdf_url)
        authors = self._strings(hit.authors)
        topics = self._topics(hit.topics)
        rank = self._source_rank(source)
        return _NormalizedHit(
            source=source,
            source_id=source_id,
            title=title,
            authors=authors,
            abstract=hit.abstract.strip() if isinstance(hit.abstract, str) else "",
            doi=canonical_doi,
            arxiv_id=canonical_arxiv,
            published_date=published_date,
            updated_date=updated_date,
            url=url,
            pdf_url=pdf_url,
            venue=normalize_topic(hit.venue) if isinstance(hit.venue, str) else "",
            topics=topics,
            citation_count=self._citation_count(hit.citation_count),
            is_open_access=hit.is_open_access if isinstance(hit.is_open_access, bool) else None,
            oa_status=normalize_topic(hit.oa_status) if isinstance(hit.oa_status, str) else "",
            license=normalize_topic(hit.license) if isinstance(hit.license, str) else "",
            origin_rank=(rank, discovery_index),
            precedence=(
                1 if source.startswith(_GENERATED_PREFIXES) else 0,
                rank,
                source,
                source_id,
            ),
        )

    @staticmethod
    def _optional_identifier(
        value: object,
        normalizer: Callable[[str], str | None],
    ) -> tuple[bool, str]:
        if value is None or value == "":
            return True, ""
        if not isinstance(value, str):
            return False, ""
        normalized = normalizer(value)
        return (normalized is not None, normalized or "")

    @staticmethod
    def _date_or_none(value: object) -> tuple[bool, date | None]:
        if value is None:
            return True, None
        if not isinstance(value, date):
            return False, None
        return True, value

    @staticmethod
    def _optional_url(value: object) -> NormalizedURL | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return normalize_url(value)
        except InputFailure:
            return None

    @staticmethod
    def _strings(values: object) -> tuple[str, ...]:
        if not isinstance(values, (tuple, list)):
            return ()
        return tuple(
            cleaned
            for value in values
            if isinstance(value, str) and (cleaned := normalize_topic(value))
        )

    @staticmethod
    def _topics(values: object) -> tuple[str, ...]:
        if not isinstance(values, (tuple, list)):
            return ()
        topics: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                continue
            normalized = normalize_topic(value)
            key = normalized.casefold()
            if normalized and key not in seen:
                seen.add(key)
                topics.append(normalized)
        return tuple(topics)

    @staticmethod
    def _citation_count(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def _source_rank(self, source: str) -> int:
        if source in self._priority:
            return self._priority[source]
        if source.startswith(_GENERATED_PREFIXES):
            return self._fallback_rank + 2
        return self._fallback_rank + 1

    @staticmethod
    def _processing_key(candidate: _NormalizedHit) -> tuple[int, int, str, str]:
        return (*candidate.origin_rank, candidate.source, candidate.source_id)

    def _attach_candidate(self, clusters: list[_Cluster], candidate: _NormalizedHit) -> None:
        doi_index, arxiv_index, source_index = self._strong_indexes(clusters)
        references: set[int] = set()
        if candidate.doi in doi_index:
            references.add(doi_index[candidate.doi])
        if candidate.arxiv_id in arxiv_index:
            references.add(arxiv_index[candidate.arxiv_id])
        if candidate.source_key in source_index:
            references.add(source_index[candidate.source_key])

        if not references and not (candidate.doi or candidate.arxiv_id):
            weak_index = self._weak_index(clusters)
            fingerprint = candidate.fingerprint
            if fingerprint is not None:
                for index in weak_index.get((fingerprint[0], fingerprint[2]), ()):
                    cluster = clusters[index]
                    if cluster.has_global_strong_id:
                        continue
                    if any(
                        bibliographic_fingerprints_match(fingerprint, existing.fingerprint)
                        for existing in cluster.candidates
                    ):
                        references.add(index)

        referenced = [clusters[index] for index in sorted(references)]
        if self._strong_conflict(referenced, candidate):
            self._reject(candidate.source, "identifier_conflict")
            return
        if not referenced:
            clusters.append(_Cluster([candidate]))
            return

        merged_candidates = [candidate]
        for cluster in referenced:
            merged_candidates.extend(cluster.candidates)
        for index in sorted(references, reverse=True):
            del clusters[index]
        clusters.append(_Cluster(merged_candidates))
        if len(referenced) > 1:
            log_event(
                _LOGGER,
                logging.DEBUG,
                "paper_clusters_merged",
                reason="bridged_identity",
                clusters=len(referenced),
            )

    @staticmethod
    def _strong_indexes(
        clusters: list[_Cluster],
    ) -> tuple[dict[str, int], dict[str, int], dict[tuple[str, str], int]]:
        doi_index: dict[str, int] = {}
        arxiv_index: dict[str, int] = {}
        source_index: dict[tuple[str, str], int] = {}
        for index, cluster in enumerate(clusters):
            for doi in cluster.dois:
                doi_index[doi] = index
            for arxiv_id in cluster.arxiv_ids:
                arxiv_index[arxiv_id] = index
            for source_key in cluster.source_keys:
                source_index[source_key] = index
        return doi_index, arxiv_index, source_index

    @staticmethod
    def _weak_index(clusters: list[_Cluster]) -> dict[tuple[str, int], list[int]]:
        index: dict[tuple[str, int], list[int]] = {}
        for cluster_index, cluster in enumerate(clusters):
            if cluster.has_global_strong_id:
                continue
            for candidate in cluster.candidates:
                fingerprint = candidate.fingerprint
                if fingerprint is not None:
                    index.setdefault((fingerprint[0], fingerprint[2]), []).append(cluster_index)
        return index

    @staticmethod
    def _strong_conflict(referenced: list[_Cluster], candidate: _NormalizedHit) -> bool:
        dois = {candidate.doi} if candidate.doi else set()
        arxiv_ids = {candidate.arxiv_id} if candidate.arxiv_id else set()
        for cluster in referenced:
            dois.update(cluster.dois)
            arxiv_ids.update(cluster.arxiv_ids)
        return len(dois) > 1 or len(arxiv_ids) > 1

    def _merge_cluster(self, cluster: _Cluster) -> PaperRecord:
        candidates = sorted(cluster.candidates, key=lambda candidate: candidate.precedence)
        title = candidates[0].title
        authors = next((candidate.authors for candidate in candidates if candidate.authors), ())
        abstract = next(
            (candidate.abstract for candidate in candidates if candidate.abstract),
            "",
        )
        published_date = next(
            (
                candidate.published_date
                for candidate in candidates
                if candidate.published_date is not None
            ),
            None,
        )
        updated_date = next(
            (
                candidate.updated_date
                for candidate in candidates
                if candidate.updated_date is not None
            ),
            None,
        )
        url = candidates[0].url
        pdf_url = next(
            (candidate.pdf_url for candidate in candidates if candidate.pdf_url is not None),
            None,
        )
        venue = next((candidate.venue for candidate in candidates if candidate.venue), "")
        is_open_access = next(
            (
                candidate.is_open_access
                for candidate in candidates
                if candidate.is_open_access is not None
            ),
            None,
        )
        oa_status = next(
            (candidate.oa_status for candidate in candidates if candidate.oa_status),
            "",
        )
        license_value = next(
            (candidate.license for candidate in candidates if candidate.license),
            "",
        )
        return PaperRecord(
            title=title,
            authors=authors,
            abstract=abstract,
            identifiers=self._identifiers(candidates),
            published_date=published_date,
            updated_date=updated_date,
            url=url,
            pdf_url=pdf_url,
            venue=venue,
            topics=self._stable_topics(candidates),
            citation_counts=self._citation_counts(candidates),
            is_open_access=is_open_access,
            oa_status=oa_status,
            license=license_value,
            sources=self._sources(candidates),
        )

    @staticmethod
    def _identifiers(candidates: list[_NormalizedHit]) -> PaperIdentifiers:
        doi = next((candidate.doi for candidate in candidates if candidate.doi), "")
        arxiv_id = next((candidate.arxiv_id for candidate in candidates if candidate.arxiv_id), "")
        native = {
            source: next(
                (candidate.source_id for candidate in candidates if candidate.source == source),
                "",
            )
            for source in ("semantic_scholar", "openalex", "dblp", "core")
        }
        return PaperIdentifiers(
            doi=doi,
            arxiv_id=arxiv_id,
            semantic_scholar_id=native["semantic_scholar"],
            openalex_id=native["openalex"],
            dblp_key=native["dblp"],
            core_id=native["core"],
        )

    @staticmethod
    def _stable_topics(candidates: list[_NormalizedHit]) -> tuple[str, ...]:
        topics: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            for topic in candidate.topics:
                key = topic.casefold()
                if key not in seen:
                    seen.add(key)
                    topics.append(topic)
        return tuple(topics)

    @staticmethod
    def _citation_counts(candidates: list[_NormalizedHit]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for candidate in candidates:
            if candidate.citation_count is not None:
                counts[candidate.source] = max(
                    counts.get(candidate.source, 0),
                    candidate.citation_count,
                )
        return counts

    @staticmethod
    def _sources(candidates: list[_NormalizedHit]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(candidate.source for candidate in candidates))

    @staticmethod
    def _reject(provider: str, reason: str) -> None:
        log_event(
            _LOGGER,
            logging.DEBUG,
            "paper_candidate_rejected",
            provider=provider,
            reason=reason,
        )
