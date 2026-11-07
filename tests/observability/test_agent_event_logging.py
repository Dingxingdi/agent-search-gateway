import asyncio
import errno
from collections.abc import Sequence
from functools import partial
from io import StringIO
from pathlib import Path

import pytest

from agent_search_gateway.concurrency import CapacityGate
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from agent_search_gateway.models import AgentInvocation, SearchScope
from agent_search_gateway.observability import configure_debug_logging
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
