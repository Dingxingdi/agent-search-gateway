"""In-memory five-field URL state machine."""

import logging
from contextlib import AbstractAsyncContextManager
from dataclasses import replace

from .concurrency import PerKeyLockPool
from .models import URLRecord, URLStatus
from .observability import log_event
from .url_normalization import NormalizedURL


def _body_pair(raw_content: str, content: str) -> tuple[str, str]:
    """Discard empty candidates without changing meaningful body text."""
    return (
        raw_content if raw_content.strip() else "",
        content if content.strip() else "",
    )


class URLStore:
    """Own URL snapshots; Fetch holds serialization during synchronous stage mutations."""

    def __init__(self) -> None:
        self._records: dict[NormalizedURL, URLRecord] = {}
        self._locks: PerKeyLockPool[NormalizedURL] = PerKeyLockPool()

    def serialization(self, url: NormalizedURL) -> AbstractAsyncContextManager[None]:
        """Serialize body admission and Fetch stages for one normalized URL."""
        return self._locks.acquire(url)

    def admit(self, url: NormalizedURL, abstract: str) -> URLRecord:
        """Admit metadata synchronously without changing existing body state."""
        normalized_abstract = abstract.strip()
        if not normalized_abstract:
            raise ValueError("URL records require a non-empty abstract")
        existing = self._records.get(url)
        if existing is not None:
            return existing
        return self._commit(URLRecord(url=url, abstract=normalized_abstract))

    async def admit_candidate(
        self,
        url: NormalizedURL,
        abstract: str,
        *,
        raw_content: str = "",
        content: str = "",
    ) -> URLRecord:
        """Cache the first whole natural candidate, rereading after locking."""
        async with self.serialization(url):
            raw_content, content = _body_pair(raw_content, content)
            existing = self.admit(url, abstract)
            if existing.status is not URLStatus.NEED_FETCH or not (raw_content or content):
                return existing
            return self._commit(
                replace(
                    existing,
                    raw_content=raw_content,
                    content=content,
                    status=URLStatus.NEED_JUDGE,
                )
            )

    def accept_candidate(
        self,
        url: NormalizedURL,
        *,
        raw_content: str = "",
        content: str = "",
    ) -> URLRecord:
        """Commit a whole judged pair while Fetch holds serialization."""
        existing = self._require_stage(url, URLStatus.NEED_FETCH, URLStatus.NEED_JUDGE)
        raw_content, content = _body_pair(raw_content, content)
        if not (raw_content or content):
            raise ValueError("Accepted candidates require a non-empty body")
        if existing.status is URLStatus.NEED_JUDGE and (
            raw_content,
            content,
        ) != (existing.raw_content, existing.content):
            raise ValueError("Judge acceptance must preserve the cached candidate pair")
        return self._commit(
            replace(
                existing,
                raw_content=raw_content,
                content=content,
                status=URLStatus.NEED_SAFETY if content else URLStatus.NEED_CLEAN,
            )
        )

    def reject_candidate(self, url: NormalizedURL) -> URLRecord:
        """Discard both fields of a rejected cached candidate."""
        existing = self._require_stage(url, URLStatus.NEED_JUDGE)
        return self._commit(
            replace(existing, raw_content="", content="", status=URLStatus.NEED_FETCH)
        )

    def set_clean_content(self, url: NormalizedURL, content: str) -> URLRecord:
        existing = self._require_stage(url, URLStatus.NEED_CLEAN)
        return self._commit(replace(existing, content=content, status=URLStatus.NEED_SAFETY))

    def mark_ready(self, url: NormalizedURL) -> URLRecord:
        existing = self._require_stage(url, URLStatus.NEED_SAFETY)
        return self._commit(replace(existing, status=URLStatus.READY))

    def mark_unavailable(self, url: NormalizedURL) -> URLRecord:
        """Commit final Fetch semantic failure or explicit Safety rejection."""
        existing = self._require_stage(url, URLStatus.NEED_FETCH, URLStatus.NEED_SAFETY)
        return self._commit(replace(existing, status=URLStatus.UNAVAILABLE))

    def get(self, url: NormalizedURL) -> URLRecord | None:
        return self._records.get(url)

    def _require(self, url: NormalizedURL) -> URLRecord:
        record = self._records.get(url)
        if record is None:
            raise KeyError(str(url))
        return record

    def _require_stage(self, url: NormalizedURL, *allowed: URLStatus) -> URLRecord:
        record = self._require(url)
        if record.status not in allowed:
            raise ValueError("URL record is not at the required processing stage")
        return record

    def _commit(self, record: URLRecord) -> URLRecord:
        self._validate(record)
        previous = self._records.get(record.url)
        self._records[record.url] = record
        log_event(
            logging.getLogger(__name__),
            logging.DEBUG,
            "url_state_committed",
            previous_status=previous.status.value if previous is not None else None,
            status=record.status.value,
        )
        return record

    @staticmethod
    def _validate(record: URLRecord) -> None:
        if not isinstance(record.status, URLStatus):
            raise ValueError("URL records require a valid processing status")
        if not record.abstract.strip():
            raise ValueError("URL records require a non-empty abstract")
        has_raw = bool(record.raw_content.strip())
        has_content = bool(record.content.strip())
        if record.status is URLStatus.NEED_FETCH:
            valid = record.raw_content == record.content == ""
        elif record.status is URLStatus.NEED_JUDGE:
            valid = has_raw or has_content
        elif record.status is URLStatus.NEED_CLEAN:
            valid = has_raw and record.content == ""
        elif record.status in {URLStatus.NEED_SAFETY, URLStatus.READY}:
            valid = has_content
        else:
            valid = True
        if not valid:
            raise ValueError("URL body does not satisfy its processing status")
