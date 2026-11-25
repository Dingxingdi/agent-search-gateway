import asyncio
from dataclasses import FrozenInstanceError, fields
from enum import StrEnum

import pytest

from agent_search_gateway.models import URLRecord, URLStatus
from agent_search_gateway.url_normalization import NormalizedURL, normalize_url
from agent_search_gateway.url_store import URLStore


def test_url_record_preserves_exactly_five_frozen_fields() -> None:
    assert [field.name for field in fields(URLRecord)] == [
        "url",
        "raw_content",
        "content",
        "abstract",
        "status",
    ]
    url = normalize_url("https://EXAMPLE.com/page")
    snapshot = URLRecord(url)
    assert snapshot.status is URLStatus.NEED_FETCH
    assert not hasattr(snapshot, "__dict__")
    assert not hasattr(snapshot, "available")
    with pytest.raises(FrozenInstanceError):
        snapshot.content = "mutated"  # type: ignore[misc]
    assert issubclass(URLStatus, StrEnum)
    assert {status.name: status.value for status in URLStatus} == {
        "NEED_FETCH": "need_fetch",
        "NEED_JUDGE": "need_judge",
        "NEED_CLEAN": "need_clean",
        "NEED_SAFETY": "need_safety",
        "READY": "ready",
        "UNAVAILABLE": "unavailable",
    }


async def test_candidate_pair_is_atomic_and_first_abstract_wins() -> None:
    store = URLStore()
    url = normalize_url("https://EXAMPLE.com/page")
    first = await store.admit_candidate(url, " First abstract ", raw_content=" raw-A\n")
    second = await store.admit_candidate(url, "Second abstract", content="content-B")
    assert first.raw_content == " raw-A\n"
    assert first.content == ""
    # B's content must never be combined with A's raw body.
    assert (second.raw_content, second.content) == (" raw-A\n", "")
    assert second.abstract == "First abstract"
    assert second.status is URLStatus.NEED_JUDGE
    assert store.get(url) == second


@pytest.mark.parametrize(
    ("raw_content", "content", "status"),
    [
        (" raw\n", "", URLStatus.NEED_JUDGE),
        ("", " content\n", URLStatus.NEED_JUDGE),
        (" raw\n", " content\n", URLStatus.NEED_JUDGE),
        ("\n ", "\t", URLStatus.NEED_FETCH),
        (" raw\n", "\t", URLStatus.NEED_JUDGE),
        ("\n ", " content\n", URLStatus.NEED_JUDGE),
    ],
)
async def test_candidate_meaningfulness_preserves_nonempty_text(
    raw_content: str, content: str, status: URLStatus
) -> None:
    store = URLStore()
    url = normalize_url("https://example.com/page")
    record = await store.admit_candidate(
        url, "Abstract", raw_content=raw_content, content=content
    )
    assert record.raw_content == (raw_content if raw_content.strip() else "")
    assert record.content == (content if content.strip() else "")
    assert record.status is status


async def _store_at(status: URLStatus) -> tuple[URLStore, URLRecord]:
    store = URLStore()
    url = normalize_url("https://example.com/page")
    record = store.admit(url, "First abstract")
    async with store.serialization(url):
        if status in {URLStatus.NEED_CLEAN, URLStatus.NEED_SAFETY, URLStatus.READY}:
            record = store.accept_candidate(url, raw_content="raw-A")
            assert record.status is URLStatus.NEED_CLEAN
            if status in {URLStatus.NEED_SAFETY, URLStatus.READY}:
                record = store.set_clean_content(url, "content-A")
                assert record.status is URLStatus.NEED_SAFETY
            if status is URLStatus.READY:
                record = store.mark_ready(url)
        elif status is URLStatus.UNAVAILABLE:
            record = store.mark_unavailable(url)
    if status is URLStatus.NEED_JUDGE:
        record = await store.admit_candidate(url, "Later abstract", raw_content="raw-A")
    assert record.status is status
    return store, record


@pytest.mark.parametrize("status", list(URLStatus))
async def test_metadata_admission_preserves_every_existing_status(status: URLStatus) -> None:
    store, before = await _store_at(status)
    after = store.admit(before.url, " Second abstract ")
    assert after == before
    assert after.abstract == "First abstract"
    assert store.get(before.url) == before


@pytest.mark.parametrize("status", list(URLStatus))
async def test_search_candidate_preserves_selected_and_terminal_states(status: URLStatus) -> None:
    store, before = await _store_at(status)
    after = await store.admit_candidate(
        before.url, "Second abstract", raw_content="raw-B", content="content-B"
    )
    if status is URLStatus.NEED_FETCH:
        assert after.status is URLStatus.NEED_JUDGE
        assert (after.raw_content, after.content) == ("raw-B", "content-B")
        assert before.raw_content == before.content == ""
        assert before.status is URLStatus.NEED_FETCH
    else:
        assert after == before
    assert after.abstract == "First abstract"


@pytest.mark.parametrize("from_search", [False, True])
@pytest.mark.parametrize(
    ("raw_content", "content", "expected"),
    [
        (" raw-A\n", "", URLStatus.NEED_CLEAN),
        ("", " content-A\n", URLStatus.NEED_SAFETY),
        (" raw-A\n", " content-A\n", URLStatus.NEED_SAFETY),
    ],
)
async def test_accept_candidate_commits_judged_whole_pair(
    from_search: bool, raw_content: str, content: str, expected: URLStatus
) -> None:
    store = URLStore()
    url = normalize_url("https://example.com/page")
    before = store.admit(url, "Abstract")
    if from_search:
        before = await store.admit_candidate(
            url, "Later abstract", raw_content=raw_content, content=content
        )
    async with store.serialization(url):
        after = store.accept_candidate(url, raw_content=raw_content, content=content)
    assert after.status is expected
    assert (after.raw_content, after.content) == (raw_content, content)
    assert store.get(url) == after
    assert before.status is (URLStatus.NEED_JUDGE if from_search else URLStatus.NEED_FETCH)


async def test_rejected_pair_clears_both_fields_and_accepts_new_search_candidate() -> None:
    store = URLStore()
    url = normalize_url("https://example.com/page")
    before = await store.admit_candidate(
        url, "Abstract", raw_content="raw-A", content="content-A"
    )
    async with store.serialization(url):
        rejected = store.reject_candidate(url)
    assert rejected.status is URLStatus.NEED_FETCH
    assert rejected.raw_content == rejected.content == ""
    assert (before.raw_content, before.content) == ("raw-A", "content-A")
    after = await store.admit_candidate(url, "Other abstract", content="content-B")
    assert (after.raw_content, after.content) == ("", "content-B")
    assert after.status is URLStatus.NEED_JUDGE
    assert after.abstract == "Abstract"


async def test_clean_then_ready_preserves_body_and_immutable_stage_snapshots() -> None:
    store, raw = await _store_at(URLStatus.NEED_CLEAN)
    async with store.serialization(raw.url):
        clean = store.set_clean_content(raw.url, " content-A\n")
        ready = store.mark_ready(raw.url)
    assert clean.status is URLStatus.NEED_SAFETY
    assert ready.status is URLStatus.READY
    assert ready.content == clean.content == " content-A\n"
    assert ready.raw_content == clean.raw_content == raw.raw_content == "raw-A"
    assert raw.status is URLStatus.NEED_CLEAN
    assert raw.content == ""
    assert store.get(raw.url) == ready


@pytest.mark.parametrize("status", [URLStatus.NEED_FETCH, URLStatus.NEED_SAFETY])
async def test_only_final_fetch_or_safety_rejection_marks_unavailable(status: URLStatus) -> None:
    store, before = await _store_at(status)
    async with store.serialization(before.url):
        terminal = store.mark_unavailable(before.url)
    assert terminal.status is URLStatus.UNAVAILABLE
    assert (terminal.raw_content, terminal.content) == (before.raw_content, before.content)


_ALLOWED_STAGES = {
    "accept_candidate": {URLStatus.NEED_FETCH, URLStatus.NEED_JUDGE},
    "reject_candidate": {URLStatus.NEED_JUDGE},
    "set_clean_content": {URLStatus.NEED_CLEAN},
    "mark_ready": {URLStatus.NEED_SAFETY},
    "mark_unavailable": {URLStatus.NEED_FETCH, URLStatus.NEED_SAFETY},
}


def _apply(store: URLStore, url: NormalizedURL, operation: str) -> URLRecord:
    if operation == "accept_candidate":
        return store.accept_candidate(url, raw_content="raw-A")
    if operation == "reject_candidate":
        return store.reject_candidate(url)
    if operation == "set_clean_content":
        return store.set_clean_content(url, "content-A")
    if operation == "mark_ready":
        return store.mark_ready(url)
    if operation == "mark_unavailable":
        return store.mark_unavailable(url)
    raise AssertionError(operation)


@pytest.mark.parametrize(
    ("status", "operation"),
    [
        (status, operation)
        for operation, allowed in _ALLOWED_STAGES.items()
        for status in URLStatus
        if status not in allowed
    ],
)
async def test_incorrect_stage_leaves_record_unchanged(status: URLStatus, operation: str) -> None:
    store, before = await _store_at(status)
    async with store.serialization(before.url):
        with pytest.raises(ValueError):
            _apply(store, before.url, operation)
    assert store.get(before.url) == before


@pytest.mark.parametrize("operation", list(_ALLOWED_STAGES))
async def test_stage_operations_require_admission(operation: str) -> None:
    store = URLStore()
    url = normalize_url("https://example.com/missing")
    async with store.serialization(url):
        with pytest.raises(KeyError):
            _apply(store, url, operation)
    assert store.get(url) is None


@pytest.mark.parametrize(
    ("status", "raw_content", "content"),
    [
        (URLStatus.NEED_FETCH, "raw", ""),
        (URLStatus.NEED_FETCH, "", "content"),
        (URLStatus.NEED_JUDGE, "", ""),
        (URLStatus.NEED_JUDGE, " \n", "\t"),
        (URLStatus.NEED_CLEAN, "", ""),
        (URLStatus.NEED_CLEAN, "raw", "content"),
        (URLStatus.NEED_CLEAN, "raw", " "),
        (URLStatus.NEED_SAFETY, "raw", ""),
        (URLStatus.READY, "", ""),
        (URLStatus.READY, "raw", " \n"),
    ],
)
def test_store_rejects_invalid_body_status_combinations(
    status: URLStatus, raw_content: str, content: str
) -> None:
    store = URLStore()
    url = normalize_url("https://example.com/page")
    before = store.admit(url, "Abstract")
    invalid = URLRecord(
        url, raw_content=raw_content, content=content, abstract="Abstract", status=status
    )
    # Exercise the private atomic commit guard, without adding an arbitrary public setter.
    with pytest.raises(ValueError):
        store._commit(invalid)
    assert store.get(url) == before


@pytest.mark.parametrize("status", [URLStatus.NEED_FETCH, URLStatus.NEED_JUDGE])
async def test_empty_accepted_candidate_does_not_commit(status: URLStatus) -> None:
    store, before = await _store_at(status)
    async with store.serialization(before.url):
        with pytest.raises(ValueError):
            store.accept_candidate(before.url, raw_content=" \n", content="\t")
    assert store.get(before.url) == before


async def test_cannot_replace_cached_candidate_during_judge_acceptance() -> None:
    store, before = await _store_at(URLStatus.NEED_JUDGE)
    async with store.serialization(before.url):
        with pytest.raises(ValueError):
            store.accept_candidate(before.url, raw_content="raw-A", content="content-B")
    assert store.get(before.url) == before


@pytest.mark.parametrize("content", ["", " \n\t"])
async def test_invalid_clean_output_leaves_need_clean_unchanged(content: str) -> None:
    store, before = await _store_at(URLStatus.NEED_CLEAN)
    async with store.serialization(before.url):
        with pytest.raises(ValueError):
            store.set_clean_content(before.url, content)
    assert store.get(before.url) == before


async def test_abstract_validation_and_normalized_url_contract() -> None:
    store = URLStore()
    url = normalize_url(" https://EXAMPLE.com/page ")
    assert str(url) == "https://example.com/page"
    with pytest.raises(ValueError):
        store.admit(url, " \n")
    assert store.get(url) is None
    with pytest.raises(ValueError):
        await store.admit_candidate(url, "\t", raw_content="raw-A")
    assert store.get(url) is None
    before = store.admit(url, " First abstract ")
    assert before.url == url
    assert before.abstract == "First abstract"
    for abstract in ["", "\n "]:
        with pytest.raises(ValueError):
            store.admit(url, abstract)
        with pytest.raises(ValueError):
            await store.admit_candidate(url, abstract, content="content-B")
    assert store.get(url) == before


async def test_concurrent_admissions_keep_first_complete_candidate_pair() -> None:
    store = URLStore()
    url = normalize_url("https://example.com/page")
    started_a = asyncio.Event()
    started_b = asyncio.Event()

    async def admit_a() -> URLRecord:
        started_a.set()
        return await store.admit_candidate(url, "Abstract A", raw_content="raw-A")

    async def admit_b() -> URLRecord:
        started_b.set()
        return await store.admit_candidate(url, "Abstract B", content="content-B")

    async with store.serialization(url):
        task_a = asyncio.create_task(admit_a())
        await started_a.wait()
        task_b = asyncio.create_task(admit_b())
        await started_b.wait()
        assert not task_a.done()
        assert not task_b.done()
        assert store.get(url) is None

    first, second = await asyncio.gather(task_a, task_b)
    assert first == second
    assert (second.raw_content, second.content) == ("raw-A", "")
    assert second.abstract == "Abstract A"
    assert second.status is URLStatus.NEED_JUDGE


async def test_waiting_admission_rereads_completed_fetch_state() -> None:
    store = URLStore()
    url = normalize_url("https://example.com/page")
    store.admit(url, "First abstract")
    started = asyncio.Event()

    async def search_admission() -> URLRecord:
        started.set()
        return await store.admit_candidate(
            url, "Search abstract", raw_content="raw-B", content="content-B"
        )

    async with store.serialization(url):
        waiting = asyncio.create_task(search_admission())
        await started.wait()
        assert not waiting.done()
        store.accept_candidate(url, raw_content="raw-A")
        store.set_clean_content(url, "content-A")
        ready = store.mark_ready(url)

    assert await waiting == ready
    assert store.get(url) == ready


async def test_serialization_is_per_url_and_cancelled_waiter_does_not_poison_lock() -> None:
    store = URLStore()
    url = normalize_url("https://example.com/page")
    other_url = normalize_url("https://example.com/other")
    started = asyncio.Event()

    async def wait_for_admission() -> URLRecord:
        started.set()
        return await store.admit_candidate(url, "Cancelled abstract", raw_content="raw-A")

    async with store.serialization(url):
        waiting = asyncio.create_task(wait_for_admission())
        await started.wait()
        assert not waiting.done()
        other = await asyncio.wait_for(
            store.admit_candidate(other_url, "Other abstract", content="content-B"),
            timeout=1,
        )
        assert other.status is URLStatus.NEED_JUDGE
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert store.get(url) is None

    recovered = await asyncio.wait_for(
        store.admit_candidate(url, "New abstract", content="content-C"), timeout=1
    )
    assert (recovered.raw_content, recovered.content) == ("", "content-C")
    assert recovered.status is URLStatus.NEED_JUDGE
