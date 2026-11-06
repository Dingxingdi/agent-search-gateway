"""Antigravity headless process boundary; returns only terminal response text."""

import asyncio
import json
import logging
import os
import signal
from collections.abc import Sequence
from tempfile import TemporaryDirectory

from ...concurrency import CapacityGate
from ...errors import ErrorCode, ExecutionFailure, ProtocolFailure
from ...observability import log_event
from ..contracts import ChatMessage

_BASE_ENVIRONMENT = (
    "PATH",
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LC_NUMERIC",
    "LC_TIME",
    "LC_COLLATE",
    "LC_MONETARY",
    "LC_MESSAGES",
    "LC_PAPER",
    "LC_NAME",
    "LC_ADDRESS",
    "LC_TELEPHONE",
    "LC_MEASUREMENT",
    "LC_IDENTIFICATION",
)


def _curated_environment(pass_env: Sequence[str]) -> dict[str, str]:
    return {
        name: os.environ[name] for name in (*_BASE_ENVIRONMENT, *pass_env) if name in os.environ
    }


def _execution_failure(reason: str) -> ExecutionFailure:
    return ExecutionFailure(
        ErrorCode.ALL_PROVIDERS_FAILED, "Agent invocation failed", reason=reason
    )


def _reject_constant(value: str) -> None:
    raise ValueError("Nonstandard JSON constant")


def _decode_terminal_response(stdout: bytes, returncode: int) -> str:
    if returncode != 0:
        raise _execution_failure("nonzero_exit")
    try:
        envelope: object = json.loads(stdout.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise ProtocolFailure(
            ErrorCode.PROTOCOL_ERROR, "Invalid Agent terminal envelope", reason="invalid_json"
        ) from None
    if not isinstance(envelope, dict) or not isinstance(envelope.get("status"), str):
        raise ProtocolFailure(
            ErrorCode.PROTOCOL_ERROR, "Invalid Agent terminal envelope", reason="invalid_json"
        )
    if envelope["status"] != "SUCCESS":
        raise _execution_failure("terminal_status_failure")
    response = envelope.get("response")
    if not isinstance(response, str):
        raise ProtocolFailure(
            ErrorCode.PROTOCOL_ERROR, "Invalid Agent terminal envelope", reason="invalid_json"
        )
    if not response.strip():
        raise _execution_failure("empty_response")
    return response


_STDOUT_LIMIT_BYTES = 4 * 1024 * 1024
_STDERR_LIMIT_BYTES = 256 * 1024


async def _read_stream(stream: asyncio.StreamReader, *, capture: bool, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := await stream.read(64 * 1024):
        size += len(chunk)
        if size > limit:
            raise _execution_failure("output_limit")
        if capture:
            chunks.append(chunk)
    return b"".join(chunks)


_HARD_TIMEOUT_MARGIN_SECONDS = 5.0
_TERMINATE_GRACE_SECONDS = 2.0
_DRAIN_TIMEOUT_SECONDS = 2.0
_REAP_TIMEOUT_SECONDS = 2.0


def _signal_group(pid: int, sig: signal.Signals) -> bool:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def _group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # An inconclusive probe must not bypass bounded termination and reaping.
        return True
    return True


async def _discard_remaining(stream: asyncio.StreamReader | None) -> None:
    if stream is not None:
        while await stream.read(64 * 1024):
            pass


async def _drain_and_reap(process: asyncio.subprocess.Process, timeout: float) -> bool:
    try:
        async with asyncio.timeout(timeout):
            results = await asyncio.gather(
                _discard_remaining(process.stdout),
                _discard_remaining(process.stderr),
                process.wait(),
                return_exceptions=True,
            )
    except TimeoutError:
        return False
    return not any(isinstance(result, BaseException) for result in results)


async def _cleanup_process(
    spawn: asyncio.Task[asyncio.subprocess.Process] | None,
    tasks: Sequence[asyncio.Task[bytes] | asyncio.Task[int]],
) -> str | None:
    if spawn is None:
        return None
    try:
        process = await spawn
    except (Exception, asyncio.CancelledError):
        return None
    # A signalling error must not skip reader cancellation or bounded pipe cleanup.
    failed = not _signal_group(process.pid, signal.SIGTERM)
    deadline = asyncio.get_running_loop().time() + _TERMINATE_GRACE_SECONDS
    while _group_exists(process.pid):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            killed = _signal_group(process.pid, signal.SIGKILL)
            failed = failed or not killed
            break
        await asyncio.sleep(min(0.02, remaining))
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    drained = await _drain_and_reap(process, _DRAIN_TIMEOUT_SECONDS)
    failed = failed or not drained
    try:
        # Process has no public close API. CPython's transport closes inherited
        # pipes and attempts to kill the leader; pipe EOF can otherwise wait forever.
        process._transport.close()  # type: ignore[attr-defined]
    except OSError:
        failed = True
    if not drained:
        reaped = await _drain_and_reap(process, _REAP_TIMEOUT_SECONDS)
        failed = failed or not reaped
    if failed:
        log_event(
            logging.getLogger(__name__),
            logging.DEBUG,
            "agent_cleanup_failed",
            stage="agent_process_cleanup",
            reason="execution_failure",
        )
        return "execution_failure"
    return None


async def _finish_cleanup(task: asyncio.Task[str | None]) -> str | None:
    # Retain and await the shielded task even when shutdown cancels us twice.
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    reason = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return reason


async def run_antigravity(
    model: str | None,
    messages: Sequence[ChatMessage],
    *,
    command: str,
    timeout_seconds: float,
    pass_env: Sequence[str],
    quota: CapacityGate,
) -> str:
    prompt = "\n\n".join(
        f"[{message['role'].upper()}]\n{message['content']}" for message in messages
    )
    argv = [
        command,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--print-timeout",
        f"{timeout_seconds:g}s",
    ]
    if model is not None:
        argv.extend(["--model", model])
    async with quota.lease():
        with TemporaryDirectory(prefix="agent-search-") as directory:
            spawn: asyncio.Task[asyncio.subprocess.Process] | None = None
            tasks: list[asyncio.Task[bytes] | asyncio.Task[int]] = []
            succeeded = False
            try:
                async with asyncio.timeout(timeout_seconds + _HARD_TIMEOUT_MARGIN_SECONDS):
                    spawn = asyncio.create_task(
                        asyncio.create_subprocess_exec(
                            *argv,
                            cwd=directory,
                            env=_curated_environment(pass_env),
                            start_new_session=True,
                            stdin=asyncio.subprocess.DEVNULL,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                    )
                    process = await asyncio.shield(spawn)
                    assert process.stdout is not None and process.stderr is not None
                    stdout = asyncio.create_task(
                        _read_stream(process.stdout, capture=True, limit=_STDOUT_LIMIT_BYTES)
                    )
                    stderr = asyncio.create_task(
                        _read_stream(process.stderr, capture=False, limit=_STDERR_LIMIT_BYTES)
                    )
                    wait = asyncio.create_task(process.wait())
                    tasks.extend((stdout, stderr, wait))
                    await asyncio.gather(*tasks)
                    response = _decode_terminal_response(stdout.result(), wait.result())
                    succeeded = True
                    return response
            except TimeoutError:
                raise _execution_failure("timeout") from None
            except OSError:
                raise _execution_failure("spawn_failure") from None
            finally:
                cleanup = asyncio.create_task(_cleanup_process(spawn, tasks))
                cleanup_reason = await _finish_cleanup(cleanup)
                if succeeded and cleanup_reason is not None:
                    raise _execution_failure(cleanup_reason)
