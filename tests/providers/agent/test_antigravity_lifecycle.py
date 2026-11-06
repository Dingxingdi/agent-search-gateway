"""Real POSIX process lifetime tests; no credentials or network."""

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from agent_search_gateway.concurrency import CapacityGate
from agent_search_gateway.errors import ExecutionFailure
from agent_search_gateway.providers.agent import antigravity_cli as adapter
from tests.support.agent_cli import force_cleanup, process_is_alive, wait_for_json, write_fake_cli


def hanging_cli(
    directory: Path,
    *,
    ignore_term: bool = False,
    leader_exit: bool = False,
    hold_pipes: bool = True,
    terminal: str | None = None,
) -> tuple[Path, Path]:
    ready = directory / "ready.json"
    child_ready = directory / "child-ready"
    child_code = (
        "import os, signal, time; from pathlib import Path; "
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_term else "")
        + f"Path({str(child_ready)!r}).write_text('ready'); time.sleep(60)"
    )
    streams = "" if hold_pipes else ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
    command = write_fake_cli(
        directory,
        f"""
        child = subprocess.Popen([sys.executable, "-c", {child_code!r}]{streams})
        while not Path({str(child_ready)!r}).exists():
            time.sleep(0.005)
        state = {{"pid": os.getpid(), "children": [child.pid], "cwd": os.getcwd()}}
        target = Path({str(ready)!r})
        pending = target.with_suffix(".tmp")
        pending.write_text(json.dumps(state))
        pending.replace(target)
        if {ignore_term!r}:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if {terminal!r} is not None:
            print({terminal!r}, flush=True)
        if {leader_exit!r}:
            sys.exit(1 if {terminal!r} == "nonzero" else 0)
        time.sleep(60)
    """,
    )
    return command, ready


def assert_clean(state: dict[str, object], gate: CapacityGate) -> None:
    pid, children, directory = state["pid"], state["children"], state["cwd"]
    assert isinstance(pid, int) and isinstance(children, list) and isinstance(directory, str)
    assert not process_is_alive(pid)
    assert all(not process_is_alive(child) for child in children)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    assert not Path(directory).exists()
    assert gate.in_use == 0


async def test_cancellation_cleans_grandchildren_in_the_owned_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter, "_TERMINATE_GRACE_SECONDS", 0.05)
    family = tmp_path / "family.json"
    ready = tmp_path / "ready.json"
    grandchild_code = (
        "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
    )
    child = write_fake_cli(
        tmp_path,
        f"""
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        grandchild = subprocess.Popen([sys.executable, "-c", {grandchild_code!r}])
        Path({str(family)!r}).write_text(json.dumps([os.getpid(), grandchild.pid]))
        time.sleep(60)
    """,
        name="child",
    )
    command = write_fake_cli(
        tmp_path,
        f"""
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen([{str(child)!r}])
        while not Path({str(family)!r}).exists():
            time.sleep(0.005)
        children = json.loads(Path({str(family)!r}).read_text())
        Path({str(ready)!r}).write_text(json.dumps(
            {{"pid": os.getpid(), "children": children, "cwd": os.getcwd()}}))
        time.sleep(60)
    """,
    )
    gate = CapacityGate(1)
    task = asyncio.create_task(
        adapter.run_antigravity(
            None,
            [],
            command=str(command),
            timeout_seconds=10,
            pass_env=(),
            quota=gate,
        )
    )
    state = await wait_for_json(ready)
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        children = state["children"]
        assert isinstance(children, list) and len(children) == 2
        assert_clean(state, gate)
    finally:
        force_cleanup(state)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("ignore_term", [False, True])
async def test_timeout_terminates_process_group_before_releasing_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ignore_term: bool,
) -> None:
    monkeypatch.setattr(adapter, "_HARD_TIMEOUT_MARGIN_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(adapter, "_TERMINATE_GRACE_SECONDS", 0.05, raising=False)
    command, ready = hanging_cli(tmp_path, ignore_term=ignore_term)
    gate = CapacityGate(1)
    task = asyncio.create_task(
        adapter.run_antigravity(
            None,
            [],
            command=str(command),
            timeout_seconds=0.3,
            pass_env=(),
            quota=gate,
        )
    )
    state = await wait_for_json(ready)
    try:
        with pytest.raises(ExecutionFailure) as caught:
            await asyncio.wait_for(task, 2)
        assert caught.value.reason == "timeout"
        assert_clean(state, gate)
        async with gate.lease():
            assert gate.in_use == 1
    finally:
        force_cleanup(state)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancellation_and_second_cancel_wait_for_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter, "_TERMINATE_GRACE_SECONDS", 0.1, raising=False)
    command, ready = hanging_cli(tmp_path, ignore_term=True)
    gate = CapacityGate(1)
    task = asyncio.create_task(
        adapter.run_antigravity(
            None,
            [],
            command=str(command),
            timeout_seconds=10,
            pass_env=(),
            quota=gate,
        )
    )
    state = await wait_for_json(ready)
    try:
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert_clean(state, gate)
    finally:
        force_cleanup(state)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_leader_exit_with_inherited_pipe_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter, "_HARD_TIMEOUT_MARGIN_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(adapter, "_TERMINATE_GRACE_SECONDS", 0.05, raising=False)
    command, ready = hanging_cli(tmp_path, ignore_term=True, leader_exit=True)
    gate = CapacityGate(1)
    task = asyncio.create_task(
        adapter.run_antigravity(
            None,
            [],
            command=str(command),
            timeout_seconds=0.3,
            pass_env=(),
            quota=gate,
        )
    )
    state = await wait_for_json(ready)
    try:
        with pytest.raises(ExecutionFailure) as caught:
            await asyncio.wait_for(task, 2)
        assert caught.value.reason == "timeout"
        assert_clean(state, gate)
    finally:
        force_cleanup(state)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    ("terminal", "reason"),
    [
        ('{"status":"SUCCESS","response":"ok"}', None),
        ('{"status":"ERROR","response":"private-payload"}', "terminal_status_failure"),
        ("bad-json", "invalid_json"),
        ("nonzero", "nonzero_exit"),
    ],
)
async def test_terminal_exit_cleans_remaining_descendants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
    reason: str | None,
) -> None:
    monkeypatch.setattr(adapter, "_TERMINATE_GRACE_SECONDS", 0.05, raising=False)
    command, ready = hanging_cli(
        tmp_path,
        ignore_term=True,
        leader_exit=True,
        hold_pipes=False,
        terminal=terminal,
    )
    gate = CapacityGate(1)
    task = asyncio.create_task(
        adapter.run_antigravity(
            None,
            [],
            command=str(command),
            timeout_seconds=1,
            pass_env=(),
            quota=gate,
        )
    )
    state = await wait_for_json(ready)
    try:
        if reason is None:
            assert await asyncio.wait_for(task, 2) == "ok"
        else:
            with pytest.raises(ExecutionFailure) as caught:
                await asyncio.wait_for(task, 2)
            assert caught.value.reason == reason
            assert "private-payload" not in str(caught.value)
        assert_clean(state, gate)
    finally:
        force_cleanup(state)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("deny_group_probe", [False, True])
async def test_cancel_while_spawn_is_returning_keeps_process_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deny_group_probe: bool,
) -> None:
    monkeypatch.setattr(adapter, "_TERMINATE_GRACE_SECONDS", 0.05, raising=False)
    command, ready = hanging_cli(tmp_path, ignore_term=deny_group_probe)
    original = asyncio.create_subprocess_exec
    created, release = asyncio.Event(), asyncio.Event()

    async def held_spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original(*args, **kwargs)
        created.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", held_spawn)
    gate = CapacityGate(1)
    task = asyncio.create_task(
        adapter.run_antigravity(
            None,
            [],
            command=str(command),
            timeout_seconds=1,
            pass_env=(),
            quota=gate,
        )
    )
    state = await wait_for_json(ready)
    signals: list[int] = []
    original_killpg = os.killpg

    def denied_probe(pid: int, sig: int) -> None:
        if pid == state["pid"]:
            signals.append(sig)
            if sig == 0:
                raise PermissionError("process-group probe denied")
        original_killpg(pid, sig)

    if deny_group_probe:
        monkeypatch.setattr(os, "killpg", denied_probe)
    try:
        await created.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert_clean(state, gate)
        if deny_group_probe:
            assert 0 in signals
            assert signal.SIGTERM in signals
            assert signal.SIGKILL in signals
    finally:
        release.set()
        force_cleanup(state)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancel_queued_invocation_never_spawns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        pytest.fail("A cancelled quota waiter must not spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    gate = CapacityGate(1)
    async with gate.lease():
        task = asyncio.create_task(
            adapter.run_antigravity(
                None,
                [],
                command="unused",
                timeout_seconds=1,
                pass_env=(),
                quota=gate,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gate.in_use == 1
    assert gate.in_use == 0


@pytest.mark.parametrize("exists", [False, True])
async def test_spawn_failure_is_safe_and_releases_capacity(tmp_path: Path, exists: bool) -> None:
    command = tmp_path / "private-executable"
    if exists:
        command.write_text("not executable")
    gate = CapacityGate(1)
    with pytest.raises(ExecutionFailure) as caught:
        await adapter.run_antigravity(
            None,
            [],
            command=str(command),
            timeout_seconds=1,
            pass_env=(),
            quota=gate,
        )
    assert caught.value.reason == "spawn_failure"
    assert str(command) not in str(caught.value)
    assert gate.in_use == 0


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_stdout_over_limit_fails_and_reaps_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: str,
) -> None:
    monkeypatch.setattr(adapter, "_HARD_TIMEOUT_MARGIN_SECONDS", 0.05)
    monkeypatch.setattr(adapter, "_TERMINATE_GRACE_SECONDS", 0.05)
    ready = tmp_path / "ready.json"
    command = write_fake_cli(
        tmp_path,
        f"""
        Path({str(ready)!r}).write_text(json.dumps({{
            "pid": os.getpid(), "children": [], "cwd": os.getcwd(),
        }}))
        if {stream!r} == "stdout":
            print(json.dumps({{"status": "SUCCESS", "response": "partial"}}), flush=True)
        output = sys.stdout.buffer if {stream!r} == "stdout" else sys.stderr.buffer
        while True:
            output.write(b"x" * 65536)
            output.flush()
    """,
    )
    gate = CapacityGate(1)
    task = asyncio.create_task(
        adapter.run_antigravity(
            None,
            [],
            command=str(command),
            timeout_seconds=0.5,
            pass_env=(),
            quota=gate,
        )
    )
    state = await wait_for_json(ready)
    try:
        with pytest.raises(ExecutionFailure) as caught:
            await asyncio.wait_for(task, 3)
        assert caught.value.reason == "output_limit"
        assert_clean(state, gate)
    finally:
        force_cleanup(state)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("excess", [0, 1])
async def test_output_limits_count_bytes_and_include_exact_boundary(
    tmp_path: Path,
    stream: str,
    excess: int,
) -> None:
    command = write_fake_cli(
        tmp_path,
        f"""
        payload = json.dumps({{"status": "SUCCESS", "response": "é"}}, ensure_ascii=False).encode()
        if {stream!r} == "stderr":
            sys.stderr.buffer.write(b"x" * (256 * 1024 + {excess}))
            sys.stderr.buffer.flush()
        else:
            payload += b" " * (4 * 1024 * 1024 + {excess} - len(payload))
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
    """,
    )
    operation = adapter.run_antigravity(
        None,
        [],
        command=str(command),
        timeout_seconds=2,
        pass_env=(),
        quota=CapacityGate(1),
    )
    async with asyncio.timeout(4):
        if excess:
            with pytest.raises(ExecutionFailure) as caught:
                await operation
            assert caught.value.reason == "output_limit"
        else:
            assert await operation == "é"


async def test_interleaved_pipes_are_drained_without_deadlock(tmp_path: Path) -> None:
    command = write_fake_cli(
        tmp_path,
        """
        payload = json.dumps({"status": "SUCCESS", "response": "done"}).encode()
        for _ in range(64):
            sys.stderr.buffer.write(b"x" * 4096)
            sys.stderr.buffer.flush()
            sys.stdout.buffer.write(b" " * 4096)
            sys.stdout.buffer.flush()
        sys.stdout.buffer.write(payload)
    """,
    )
    async with asyncio.timeout(4):
        assert (
            await adapter.run_antigravity(
                None,
                [],
                command=str(command),
                timeout_seconds=2,
                pass_env=(),
                quota=CapacityGate(1),
            )
            == "done"
        )
