import asyncio
import errno
import json
import shlex
from collections.abc import Sequence
from functools import partial
from io import StringIO
from pathlib import Path

import pytest

from agent_search_gateway.concurrency import CapacityGate
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.models import AgentInvocation, SearchScope
from agent_search_gateway.observability import SecretValue, configure_debug_logging
from agent_search_gateway.providers.agent import antigravity_cli as adapter
from agent_search_gateway.providers.contracts import ChatMessage
from tests.support.agent_cli import write_fake_cli
from tests.support.agent_search import (
    RecordingRunner,
    agent_orchestrator,
    paper_markdown,
    web_markdown,
)
from tests.support.logging import structured_test_logger


async def test_agent_failure_logs_allowlisted_reason_without_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_PRIVATE", "ENV_VALUE_SENTINEL")
    command = write_fake_cli(
        tmp_path,
        """
        sys.stderr.write("STDERR_SENTINEL " + os.environ["AGENT_PRIVATE"])
        print(json.dumps({"status": "ERROR", "response": "RESPONSE_SENTINEL",
            "error": "ERROR_SENTINEL"}))
    """,
    )
    runner = partial(
        adapter.run_antigravity,
        provider_name="agy",
        command=str(command),
        timeout_seconds=5,
        pass_env=("AGENT_PRIVATE",),
        quota=CapacityGate(1),
    )
    stderr = StringIO()
    log_file = tmp_path / "debug.log"
    session = configure_debug_logging(log_file, stderr=stderr)
    try:
        search = agent_orchestrator(tmp_path / "results", [AgentInvocation("agy")], {"agy": runner})
        with pytest.raises(ExecutionFailure) as caught:
            await search.agent_search("PROMPT_SENTINEL", request_id="11111111")
    finally:
        session.close()
    combined = log_file.read_text() + stderr.getvalue() + caught.value.message
    failed = next(
        line for line in stderr.getvalue().splitlines() if "event=provider_failed" in line
    )
    for field in (
        "provider=agy",
        "model=null",
        "stage=agent_search",
        "elapsed_ms=",
        "reason=terminal_status_failure",
    ):
        assert field in failed
    for sentinel in (
        "STDERR_SENTINEL",
        "ENV_VALUE_SENTINEL",
        "RESPONSE_SENTINEL",
        "ERROR_SENTINEL",
        "PROMPT_SENTINEL",
    ):
        assert sentinel not in combined
    assert "llm_stage_" not in combined


@pytest.mark.parametrize("scope", ["web", "paper"])
@pytest.mark.parametrize("model", [None, "pinned"])
async def test_agent_success_logs_actual_configuration_without_payloads(
    tmp_path: Path,
    scope: SearchScope,
    model: str | None,
) -> None:
    logger, stream = structured_test_logger("test.agent.success")
    text = (
        paper_markdown(title="TITLE_SENTINEL")
        if scope == "paper"
        else web_markdown(abstract="BODY_SENTINEL")
    )
    search = agent_orchestrator(
        tmp_path, [AgentInvocation("agy", model)], {"agy": RecordingRunner(text)}, logger=logger
    )
    await search.agent_search("PROMPT_SENTINEL", request_id="11111111", scope=scope)
    logged = stream.getvalue()
    for event in ("provider_started", "provider_completed"):
        line = next(line for line in logged.splitlines() if f"event={event}" in line)
        assert f"model={model or 'null'}" in line
        assert f"stage={'agent_paper_search' if scope == 'paper' else 'agent_search'}" in line
    assert "llm_stage_" not in logged
    assert all(s not in logged for s in ("TITLE_SENTINEL", "BODY_SENTINEL", "PROMPT_SENTINEL"))


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("json", "invalid_json"),
        ("exit", "nonzero_exit"),
        ("empty", "empty_response"),
        ("markdown", "invalid_markdown"),
        ("limit", "output_limit"),
        ("timeout", "timeout"),
        ("spawn", "spawn_failure"),
    ],
)
async def test_agent_real_process_failure_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    reason: str,
) -> None:
    body = {
        "json": 'print("JSON_SENTINEL")',
        "exit": 'sys.stderr.write("STDERR_SENTINEL"); sys.exit(9)',
        "empty": 'print(json.dumps({"status": "SUCCESS", "response": ""}))',
        "markdown": 'print(json.dumps({"status": "SUCCESS", "response": "MARKDOWN_SENTINEL"}))',
        "limit": 'print("x" * 256)',
        "timeout": "time.sleep(60)",
        "spawn": 'print("unused")',
    }[case]
    command = write_fake_cli(tmp_path, body)
    if case == "spawn":
        command.unlink()
    if case == "limit":
        monkeypatch.setattr(adapter, "_STDOUT_LIMIT_BYTES", 128)
    if case == "timeout":
        monkeypatch.setattr(adapter, "_HARD_TIMEOUT_MARGIN_SECONDS", 0.02)
    logger, stream = structured_test_logger("test.agent.failure")
    runner = partial(
        adapter.run_antigravity,
        provider_name="agy",
        command=str(command),
        timeout_seconds=0.1,
        pass_env=(),
        quota=CapacityGate(1),
    )
    search = agent_orchestrator(
        tmp_path / "results", [AgentInvocation("agy", "pin")], {"agy": runner}, logger=logger
    )
    with pytest.raises(ExecutionFailure):
        await search.agent_search("PROMPT_SENTINEL", request_id="11111111", scope="paper")
    failed = next(
        line for line in stream.getvalue().splitlines() if "event=provider_failed" in line
    )
    assert f"reason={reason}" in failed
    assert "model=pin" in failed and "stage=agent_paper_search" in failed
    assert "SENTINEL" not in stream.getvalue()


@pytest.mark.parametrize(
    ("error_number", "reason"),
    [(errno.E2BIG, "argument_limit"), (errno.EACCES, "spawn_failure")],
)
async def test_agent_spawn_os_error_is_safe_and_releases_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
    reason: str,
) -> None:
    invocation_directories: list[Path] = []

    async def fail_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        directory = kwargs["cwd"]
        assert isinstance(directory, str)
        invocation_directories.append(Path(directory))
        assert invocation_directories[-1].is_dir()
        raise OSError(error_number, "OS_ERROR_SENTINEL", "COMMAND_SENTINEL")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    gate = CapacityGate(1)
    runner = partial(
        adapter.run_antigravity,
        provider_name="agy",
        command="COMMAND_SENTINEL",
        timeout_seconds=5,
        pass_env=(),
        quota=gate,
    )
    logger, stream = structured_test_logger("test.agent.spawn.failure")
    search = agent_orchestrator(
        tmp_path / "results", [AgentInvocation("agy")], {"agy": runner}, logger=logger
    )
    with pytest.raises(ExecutionFailure) as caught:
        await search.agent_search("PROMPT_SENTINEL", request_id="11111111")

    assert f"reason={reason}" in stream.getvalue()
    assert "SENTINEL" not in stream.getvalue() + str(caught.value)
    assert len(invocation_directories) == 1
    assert not invocation_directories[0].exists()
    assert gate.in_use == 0


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("EXCEPTION_SENTINEL"),
        ExecutionFailure(
            ErrorCode.ALL_PROVIDERS_FAILED, "MESSAGE_SENTINEL", reason="REASON_SENTINEL"
        ),
    ],
)
async def test_agent_untrusted_failure_reason_is_not_logged(
    tmp_path: Path, failure: Exception
) -> None:
    logger, stream = structured_test_logger("test.agent.unknown")
    search = agent_orchestrator(
        tmp_path, [AgentInvocation("agy")], {"agy": RecordingRunner(failure=failure)}, logger=logger
    )
    with pytest.raises(ExecutionFailure) as caught:
        await search.agent_search("PROMPT_SENTINEL", request_id="11111111")
    assert "reason=execution_failure" in stream.getvalue()
    assert "SENTINEL" not in stream.getvalue() + caught.value.message


async def test_agent_cancelled_event_preserves_cancellation(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def runner(model: str | None, messages: Sequence[ChatMessage]) -> str:
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    logger, stream = structured_test_logger("test.agent.cancel")
    search = agent_orchestrator(tmp_path, [AgentInvocation("agy")], {"agy": runner}, logger=logger)
    task = asyncio.create_task(search.agent_search("PROMPT_SENTINEL", request_id="11111111"))
    async with asyncio.timeout(1):
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "reason=cancelled" in stream.getvalue()
    assert "model=null" in stream.getvalue()
    assert "PROMPT_SENTINEL" not in stream.getvalue()


async def test_agent_terminal_diagnostic_preserves_safe_status_and_byte_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_PRIVATE", "ENV_VALUE_SENTINEL")
    envelope = {
        "status": "ERROR",
        "error": {"code": "quota_exceeded", "message": "Please retry later"},
        "response": "RESPONSE_SENTINEL",
    }
    stdout = (json.dumps(envelope) + "\n").encode()
    stderr_text = "STDERR_SENTINEL ENV_VALUE_SENTINEL"
    command = write_fake_cli(
        tmp_path,
        f"sys.stdout.buffer.write({stdout!r})\nsys.stderr.write({stderr_text!r})",
    )
    stream = StringIO()
    session = configure_debug_logging(tmp_path / "diagnostic.log", stderr=stream)
    try:
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.run_antigravity(
                "pinned-model",
                [{"role": "user", "content": "PROMPT_SENTINEL"}],
                provider_name="agy",
                command=str(command),
                timeout_seconds=5,
                pass_env=("AGENT_PRIVATE",),
                quota=CapacityGate(1),
            )
    finally:
        session.close()
    lines = [
        line for line in stream.getvalue().splitlines() if "event=agent_process_diagnostic" in line
    ]
    assert len(lines) == 1
    for field in (
        "provider=agy",
        "model=pinned-model",
        "stage=agent_process",
        "upstream_status=ERROR",
        "upstream_code=quota_exceeded",
        'upstream_message="Please retry later"',
        "reason=terminal_status_failure",
        "exit_code=0",
        "elapsed_ms=",
        f"stdout_bytes={len(stdout)}",
        f"stderr_bytes={len(stderr_text.encode())}",
    ):
        assert field in lines[0]
    assert caught.value.reason == "terminal_status_failure"
    assert caught.value.diagnostics["stdout_bytes"] == len(stdout)
    assert caught.value.diagnostics["stderr_bytes"] == len(stderr_text.encode())
    combined = stream.getvalue() + (tmp_path / "diagnostic.log").read_text()
    assert "SENTINEL" not in combined + str(caught.value) + repr(caught.value.diagnostics)
    assert "event=provider_completed" not in combined
    assert "event=provider_failed" not in combined


async def test_agent_success_diagnostic_follows_cleanup(tmp_path: Path) -> None:
    stdout = b'{"status":"SUCCESS","response":"RESPONSE_SENTINEL"}'
    command = write_fake_cli(tmp_path, f"sys.stdout.buffer.write({stdout!r})")
    stream = StringIO()
    session = configure_debug_logging(tmp_path / "success.log", stderr=stream)
    try:
        result = await adapter.run_antigravity(
            None,
            [],
            provider_name="configured-alias",
            command=str(command),
            timeout_seconds=5,
            pass_env=(),
            quota=CapacityGate(1),
        )
    finally:
        session.close()
    assert result == "RESPONSE_SENTINEL"
    lines = [
        line for line in stream.getvalue().splitlines() if "event=agent_process_diagnostic" in line
    ]
    assert len(lines) == 1
    for field in (
        "provider=configured-alias",
        "model=null",
        "stage=agent_process",
        "semantic_result=success",
        "upstream_status=SUCCESS",
        "exit_code=0",
        f"stdout_bytes={len(stdout)}",
        "stderr_bytes=0",
    ):
        assert field in lines[0]
    assert "SENTINEL" not in stream.getvalue()
    assert "event=provider_" not in stream.getvalue()


@pytest.mark.parametrize(
    ("stdout", "exit_code", "reason", "category", "status"),
    [
        (
            b'{"status":"ERROR","error":{"code":"IGNORED_SENTINEL"}}',
            7,
            "nonzero_exit",
            "nonzero_exit",
            None,
        ),
        (b"", 0, "invalid_json", "invalid_json", None),
        (b"BAD_JSON_SENTINEL", 0, "invalid_json", "invalid_json", None),
        (b'{"status":"SUCCESS"}', 0, "invalid_json", "invalid_json", "SUCCESS"),
        (b'{"status":"SUCCESS","response":""}', 0, "empty_response", "empty_output", "SUCCESS"),
    ],
)
async def test_agent_process_failure_diagnostics_preserve_priority(
    tmp_path: Path,
    stdout: bytes,
    exit_code: int,
    reason: str,
    category: str,
    status: str | None,
) -> None:
    command = write_fake_cli(
        tmp_path,
        f"sys.stdout.buffer.write({stdout!r})\n"
        f"sys.stderr.buffer.write(b'STDERR_SENTINEL')\n"
        f"sys.exit({exit_code})",
    )
    stream = StringIO()
    session = configure_debug_logging(tmp_path / "failure.log", stderr=stream)
    try:
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.run_antigravity(
                None,
                [],
                provider_name="alias",
                command=str(command),
                timeout_seconds=5,
                pass_env=(),
                quota=CapacityGate(1),
            )
    finally:
        session.close()
    assert caught.value.reason == reason
    lines = [
        line for line in stream.getvalue().splitlines() if "event=agent_process_diagnostic" in line
    ]
    assert len(lines) == 1
    for field in (
        f"reason={reason}",
        f"category={category}",
        "semantic_result=error",
        f"exit_code={exit_code}",
        f"stdout_bytes={len(stdout)}",
        "stderr_bytes=15",
    ):
        assert field in lines[0]
    assert ("upstream_status=" in lines[0]) is (status is not None)
    if status is not None:
        assert f"upstream_status={status}" in lines[0]
    assert caught.value.diagnostics["exit_code"] == exit_code
    assert "SENTINEL" not in stream.getvalue() + repr(caught.value.diagnostics)


@pytest.mark.parametrize("case", ["timeout", "output_limit"])
async def test_agent_interrupted_process_diagnostics_use_only_observed_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    stdout = b"PAYLOAD_SENTINEL"
    command = write_fake_cli(
        tmp_path,
        "sys.stderr.buffer.write(b'STDERR_SENTINEL')\n"
        "sys.stderr.flush()\n"
        f"sys.stdout.buffer.write({stdout!r})\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)",
    )
    monkeypatch.setattr(adapter, "_HARD_TIMEOUT_MARGIN_SECONDS", 0.02)
    if case == "output_limit":
        monkeypatch.setattr(adapter, "_STDOUT_LIMIT_BYTES", 8)
    stream = StringIO()
    session = configure_debug_logging(tmp_path / "interrupted.log", stderr=stream)
    gate = CapacityGate(1)
    try:
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.run_antigravity(
                None,
                [],
                provider_name="alias",
                command=str(command),
                timeout_seconds=0.2,
                pass_env=(),
                quota=gate,
            )
    finally:
        session.close()
    assert caught.value.reason == case
    lines = [
        line for line in stream.getvalue().splitlines() if "event=agent_process_diagnostic" in line
    ]
    assert len(lines) == 1
    for field in (
        f"reason={case}",
        f"category={case}",
        "semantic_result=error",
        f"stdout_bytes={len(stdout)}",
        "stderr_bytes=15",
        "exit_code=-15",
    ):
        assert field in lines[0]
    assert "upstream_status=" not in lines[0]
    assert "SENTINEL" not in stream.getvalue() + repr(caught.value.diagnostics)
    assert gate.in_use == 0


async def test_agent_spawn_diagnostic_omits_unobserved_process_facts(tmp_path: Path) -> None:
    stream = StringIO()
    session = configure_debug_logging(tmp_path / "spawn.log", stderr=stream)
    try:
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.run_antigravity(
                None,
                [],
                provider_name="alias",
                command=str(tmp_path / "missing"),
                timeout_seconds=5,
                pass_env=(),
                quota=CapacityGate(1),
            )
    finally:
        session.close()
    assert caught.value.reason == "spawn_failure"
    lines = [
        line for line in stream.getvalue().splitlines() if "event=agent_process_diagnostic" in line
    ]
    assert len(lines) == 1
    assert "category=spawn" in lines[0]
    assert "semantic_result=error" in lines[0]
    assert "exit_code=" not in lines[0]
    assert "upstream_status=" not in lines[0]
    assert "stdout_bytes=0" in lines[0] and "stderr_bytes=0" in lines[0]
    assert "exit_code" not in caught.value.diagnostics


@pytest.mark.parametrize("unexpected", [False, True])
async def test_agent_cleanup_failure_never_emits_process_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unexpected: bool,
) -> None:
    command = write_fake_cli(
        tmp_path, 'print(json.dumps({"status":"SUCCESS","response":"RESPONSE_SENTINEL"}))'
    )
    stream = StringIO()
    original_cleanup = adapter._cleanup_process
    cleaned = False

    async def failed_cleanup(
        spawn: asyncio.Task[asyncio.subprocess.Process] | None,
        tasks: Sequence[asyncio.Task[bytes] | asyncio.Task[int]],
    ) -> str | None:
        nonlocal cleaned
        assert await original_cleanup(spawn, tasks) is None
        cleaned = True
        assert "event=agent_process_diagnostic" not in stream.getvalue()
        if unexpected:
            raise RuntimeError("CLEANUP_SENTINEL")
        return "execution_failure"

    monkeypatch.setattr(adapter, "_cleanup_process", failed_cleanup)
    session = configure_debug_logging(tmp_path / "cleanup.log", stderr=stream)
    gate = CapacityGate(1)
    try:
        with pytest.raises(RuntimeError if unexpected else ExecutionFailure) as caught:
            await adapter.run_antigravity(
                None,
                [],
                provider_name="alias",
                command=str(command),
                timeout_seconds=5,
                pass_env=(),
                quota=gate,
            )
    finally:
        session.close()
    assert cleaned and gate.in_use == 0
    if isinstance(caught.value, ExecutionFailure):
        assert caught.value.reason == "execution_failure"
    lines = [
        line for line in stream.getvalue().splitlines() if "event=agent_process_diagnostic" in line
    ]
    assert len(lines) == 1
    for field in (
        "reason=execution_failure",
        "category=unknown",
        "semantic_result=error",
        "upstream_status=SUCCESS",
        "exit_code=0",
    ):
        assert field in lines[0]
    assert "semantic_result=success" not in stream.getvalue()
    assert "SENTINEL" not in stream.getvalue()


@pytest.mark.parametrize(
    "error",
    [
        "ERROR_SENTINEL",
        {"code": {"nested": "ERROR_SENTINEL"}, "message": {"text": "ERROR_SENTINEL"}},
    ],
)
async def test_agent_unknown_error_shapes_are_not_interpreted(
    tmp_path: Path,
    error: object,
) -> None:
    stdout = json.dumps({"status": "ERROR", "error": error, "response": "RESPONSE_SENTINEL"})
    command = write_fake_cli(tmp_path, f"sys.stdout.write({stdout!r})")
    stream = StringIO()
    session = configure_debug_logging(tmp_path / "unknown.log", stderr=stream)
    try:
        with pytest.raises(ExecutionFailure):
            await adapter.run_antigravity(
                None,
                [],
                provider_name="alias",
                command=str(command),
                timeout_seconds=5,
                pass_env=(),
                quota=CapacityGate(1),
            )
    finally:
        session.close()
    line = next(
        line for line in stream.getvalue().splitlines() if "event=agent_process_diagnostic" in line
    )
    assert "category=unknown" in line
    assert "upstream_status=ERROR" in line
    assert "upstream_code=" not in line and "upstream_message=" not in line
    assert "SENTINEL" not in stream.getvalue()


async def test_agent_selected_error_text_is_redacted_before_bounding(tmp_path: Path) -> None:
    credential = "[REDACTED_SECRET]\n" * 4
    status = "s" * 120 + credential + "z" * 200
    code = "c" * 120 + credential + "z" * 200
    message = (
        "m" * 500
        + credential
        + " https://example.test/path?q=QUERY_SENTINEL#FRAGMENT_SENTINEL "
        + "界" * 600
    )
    stdout = json.dumps(
        {
            "status": status,
            "error": {"code": code, "message": message},
            "response": "RESPONSE_SENTINEL",
        },
        ensure_ascii=False,
    ).encode()
    command = write_fake_cli(tmp_path, f"sys.stdout.buffer.write({stdout!r})")
    stream = StringIO()
    session = configure_debug_logging(tmp_path / "bounded.log", stderr=stream)
    session.add_secrets(SecretValue(credential))
    try:
        with pytest.raises(ExecutionFailure) as caught:
            await adapter.run_antigravity(
                None,
                [],
                provider_name="alias",
                command=str(command),
                timeout_seconds=5,
                pass_env=(),
                quota=CapacityGate(1),
            )
    finally:
        session.close()
    logged = stream.getvalue()
    assert len(logged.splitlines()) == 1
    assert "upstream_status=" + "s" * 120 in logged
    assert "upstream_code=" + "c" * 120 in logged
    assert 'upstream_message="' + "m" * 500 in logged
    assert "upstream_message_truncated=true" in logged
    assert f"upstream_message_chars={len(message)}" in logged
    assert f"upstream_message_bytes={len(message.encode())}" in logged
    assert "[REDACT" not in logged
    assert "SENTINEL" not in logged + repr(caught.value.diagnostics)
    assert "m" * 500 not in repr(caught.value.diagnostics)
    fields = dict(part.split("=", 1) for part in shlex.split(logged) if "=" in part)
    for field, limit in (
        ("upstream_status", 128),
        ("upstream_code", 128),
        ("upstream_message", 512),
    ):
        assert len(fields[field]) <= limit
