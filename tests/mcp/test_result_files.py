import asyncio
import errno
import os
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_search_gateway.mcp.adapter import AdapterFailure, GatewayAdapter
from agent_search_gateway.models import SearchRecord, SuccessResponse
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.request_ids import ResultKind, result_filename
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_normalization import normalize_url
from tests.mcp._support import RecordingClient, guard_result_opens


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    runtime_paths = RuntimePaths.from_home(tmp_path)
    runtime_paths.results_dir.mkdir(parents=True)
    return runtime_paths


@pytest.mark.parametrize(
    ("kind", "search", "ending"),
    [
        ("keyword", GatewayAdapter.keyword_search, "\n"),
        ("paper", GatewayAdapter.paper_search, ""),
        ("llm", GatewayAdapter.llm_search, "\r\n"),
        ("agent", GatewayAdapter.agent_search, ""),
    ],
)
@pytest.mark.parametrize("empty", [False, True], ids=["rows", "empty"])
async def test_exact_search_result(
    paths: RuntimePaths,
    kind: ResultKind,
    search: Callable[[GatewayAdapter, str], Awaitable[str]],
    ending: str,
    empty: bool,
) -> None:
    result = paths.results_dir / result_filename(kind, "89abcdef")
    decoy = paths.results_dir / result_filename(kind, "01234567")
    expected = (
        ""
        if empty
        else (
            '\ufeff  {"type":"web", "abstract":"雪"}\r\n'
            '\t{"type":"paper", "title":"研究"}  \n'
            '{"type":"web", "abstract":"café"}\t' + ending
        )
    )
    original = expected.encode("utf-8")
    result.write_bytes(original)
    decoy.write_bytes(b"another result")
    client = RecordingClient(SuccessResponse(str(result)))
    adapter = GatewayAdapter(paths, client)

    with guard_result_opens(paths.results_dir.resolve(), result.name) as opened:
        assert await search(adapter, "query") == expected

    assert len(opened) == 2
    assert len(client.calls) == 1
    assert result.read_bytes() == original
    assert decoy.read_bytes() == b"another result"


async def assert_result_rejected(paths: RuntimePaths, response_text: str) -> None:
    client = RecordingClient(SuccessResponse(response_text))
    # A second result is available, but a failed read must never search for a substitute.
    decoy = paths.results_dir / result_filename("keyword", "76543210")
    decoy.write_bytes(b"synthetic-private-marker")
    with (
        guard_result_opens(paths.results_dir.resolve(), Path(response_text).name),
        pytest.raises(AdapterFailure) as error,
    ):
        await GatewayAdapter(paths, client).keyword_search("query")
    assert error.value.code is None
    assert str(error.value) == error.value.safe_message == "Search result unavailable or invalid"
    assert len(client.calls) == 1
    assert decoy.read_bytes() == b"synthetic-private-marker"


@pytest.mark.parametrize(
    "case",
    [
        "relative",
        "traversal",
        "nul",
        "outside",
        "sibling",
        "nested",
        "kind",
        "suffix",
        "id",
        "trailing-slash",
        "trailing-dot",
    ],
)
async def test_safety_rejects_invalid_location_or_name(
    tmp_path: Path, paths: RuntimePaths, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = paths.results_dir
    (root / "nested").mkdir()
    monkeypatch.chdir(root)
    name = result_filename("keyword", "89abcdef")
    candidates = {
        "relative": name,
        "traversal": str(root / "nested" / ".." / name),
        "nul": str(root / name) + "\x00",
        "trailing-slash": str(root / name) + "/",
        "trailing-dot": str(root / name) + "/.",
        "outside": str(tmp_path / name),
        "sibling": str(root.with_name(root.name + "-old") / name),
        "nested": str(root / "nested" / name),
        "kind": str(root / result_filename("paper", "89abcdef")),
        "suffix": str(root / "keyword-89abcdef.txt"),
        "id": str(root / "keyword-89abcdeF.jsonl"),
    }
    candidate = candidates[case]
    if case != "nul":
        file = Path(candidate)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"sample")

    await assert_result_rejected(paths, candidate)

    if case != "nul":
        assert Path(candidate).read_bytes() == b"sample"


async def test_safety_accepts_writer_file_under_resolved_root(tmp_path: Path) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    actual = tmp_path / "stored-results"
    actual.mkdir()
    paths.results_dir.parent.mkdir(parents=True)
    paths.results_dir.symlink_to(actual, target_is_directory=True)
    record = SearchRecord(normalize_url("https://example.org/"), "研究" * 50_000)
    result = ResultWriter(paths.results_dir).write_results(
        "keyword", [record], request_id="89abcdef"
    )
    original = result.read_bytes()
    client = RecordingClient(SuccessResponse(str(result)))
    assert result.parent == actual.resolve()
    assert await GatewayAdapter(paths, client).keyword_search("query") == original.decode("utf-8")
    assert len(client.calls) == 1
    assert result.read_bytes() == original


@pytest.mark.parametrize("kind", ["directory", "fifo", "internal-symlink", "external-symlink"])
async def test_safety_rejects_nonregular_result(
    tmp_path: Path, paths: RuntimePaths, kind: str
) -> None:
    root = paths.results_dir
    result = root / result_filename("keyword", "89abcdef")
    if kind == "directory":
        result.mkdir()
    elif kind == "fifo":
        os.mkfifo(result)
    else:
        target_dir = root if kind == "internal-symlink" else tmp_path
        target = target_dir / "sample"
        target.write_bytes(b"sample")
        result.symlink_to(target)
    before = result.lstat()

    await assert_result_rejected(paths, str(result))

    assert os.path.samestat(result.lstat(), before)
    if result.is_symlink():
        assert result.readlink() == target
        assert target.read_bytes() == b"sample"


@pytest.mark.parametrize("error_number", [errno.ENOENT, errno.EACCES, errno.ELOOP])
async def test_race_child_open_failure_is_safe(paths: RuntimePaths, error_number: int) -> None:
    result = paths.results_dir / result_filename("keyword", "89abcdef")
    result.write_bytes(b"sample")
    real_open = os.open

    def fail_child_open(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if dir_fd is not None:
            raise OSError(error_number, "synthetic-private-marker", str(result))
        return real_open(path, flags, mode)

    with patch("os.open", side_effect=fail_child_open):
        await assert_result_rejected(paths, str(result))
    assert result.read_bytes() == b"sample"


@pytest.mark.parametrize("changed_object", ["regular", "fifo", "directory"])
async def test_race_rejects_changed_descriptor_before_read(
    tmp_path: Path, paths: RuntimePaths, changed_object: str
) -> None:
    result = paths.results_dir / result_filename("keyword", "89abcdef")
    result.write_bytes(b"original")
    other = tmp_path / "other"
    if changed_object == "regular":
        other.write_bytes(b"sample")
    elif changed_object == "fifo":
        os.mkfifo(other)
    else:
        other.mkdir()

    # Inject the descriptor a changed directory entry would yield, without timing a rename.
    real_open = os.open
    other_fd = real_open(other, os.O_RDONLY | os.O_NONBLOCK)
    try:

        def changed_open(
            path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
        ) -> int:
            if dir_fd is not None:
                return os.dup(other_fd)
            return real_open(path, flags, mode)

        with (
            patch("os.open", side_effect=changed_open),
            patch("os.read", side_effect=AssertionError("read from a changed object")),
        ):
            await assert_result_rejected(paths, str(result))
    finally:
        os.close(other_fd)
    assert result.read_bytes() == b"original"
    assert other.exists()
    if changed_object == "regular":
        assert other.read_bytes() == b"sample"


@pytest.mark.parametrize("contents", [None, b"sample\xff"], ids=["missing", "invalid-utf8"])
async def test_safety_unavailable_result(paths: RuntimePaths, contents: bytes | None) -> None:
    result = paths.results_dir / result_filename("keyword", "89abcdef")
    if contents is not None:
        result.write_bytes(contents)

    await assert_result_rejected(paths, str(result))

    if contents is None:
        assert not result.exists()
    else:
        assert result.read_bytes() == contents


async def test_safety_read_failure_discards_partial_data(paths: RuntimePaths) -> None:
    result = paths.results_dir / result_filename("keyword", "89abcdef")
    contents = b"synthetic-private-marker"
    result.write_bytes(contents)
    real_read = os.read
    partial = bytearray()

    def fail_after_prefix(fd: int, size: int) -> bytes:
        if partial:
            raise OSError(errno.EIO, "synthetic-private-marker", str(result))
        chunk = real_read(fd, min(size, 4))
        partial.extend(chunk)
        return chunk

    with patch("os.read", side_effect=fail_after_prefix):
        await assert_result_rejected(paths, str(result))
    assert partial == contents[:4]
    assert result.read_bytes() == contents


@pytest.mark.parametrize("cancel", [False, True], ids=["finish", "cancel"])
async def test_offload_and_resource_cleanup(paths: RuntimePaths, cancel: bool) -> None:
    result = paths.results_dir / result_filename("keyword", "89abcdef")
    result.write_bytes(b"worker data")
    client = RecordingClient(SuccessResponse(str(result)))
    adapter = GatewayAdapter(paths, client)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    closed = asyncio.Event()
    release = threading.Event()
    real_read = os.read
    real_close = os.close

    def blocked_read(fd: int, size: int) -> bytes:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "the event loop did not release the worker"
        return real_read(fd, size)

    with guard_result_opens(paths.results_dir.resolve(), result.name) as opened:

        def tracked_close(fd: int) -> None:
            real_close(fd)
            if fd == opened[0]:
                loop.call_soon_threadsafe(closed.set)

        with (
            patch("os.read", side_effect=blocked_read),
            patch("os.close", side_effect=tracked_close),
        ):
            search = asyncio.create_task(adapter.keyword_search("query"))
            try:
                await asyncio.wait_for(started.wait(), 3)
                # Fetch gets the same path-looking text and must not open a second file.
                fetch = await asyncio.wait_for(adapter.url_fetch("https://example.org"), 3)
                assert fetch == str(result)
                assert not search.done()
                if cancel:
                    search.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await search
            finally:
                release.set()
            if not cancel:
                assert await asyncio.wait_for(search, 3) == "worker data"
            await asyncio.wait_for(closed.wait(), 3)
    assert len(opened) == len(client.calls) == 2
    assert result.read_bytes() == b"worker data"
