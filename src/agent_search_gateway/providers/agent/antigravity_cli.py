"""Antigravity headless process boundary; returns only terminal response text."""

import asyncio
import errno
import json
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Sequence
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from decimal import Decimal
from tempfile import TemporaryDirectory

from ...concurrency import CapacityGate
from ...errors import ErrorCode, ExecutionFailure, ProtocolFailure
from ...input_limits import InputBudgetUnfit, PreparedInput, fit_messages_to_budget
from ...observability import log_event, log_upstream_body_preview, make_input_preview_projector
from ...rate_limit import RateLimitWaitTimeout
from ..contracts import ChatMessage
from ..diagnostics import best_effort_diagnostics, safe_diagnostic_fields

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


def _decode_terminal_response(
    stdout: bytes | bytearray, returncode: int, *, diagnostics: dict[str, object] | None = None
) -> str:
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
    if diagnostics is not None:
        diagnostics.update(
            best_effort_diagnostics(
                lambda: {
                    "upstream_status": envelope["status"],
                    "response_kind": "object",
                }
            )
        )
    if envelope["status"] != "SUCCESS":
        if diagnostics is not None:
            diagnostics.update(best_effort_diagnostics(lambda: _terminal_error_fields(envelope)))
        raise _execution_failure("terminal_status_failure")
    response = envelope.get("response")
    if not isinstance(response, str):
        raise ProtocolFailure(
            ErrorCode.PROTOCOL_ERROR, "Invalid Agent terminal envelope", reason="invalid_json"
        )
    if not response.strip():
        if diagnostics is not None:
            diagnostics["category"] = "empty_output"
        raise _execution_failure("empty_response")
    return response


def _terminal_error_fields(envelope: dict[str, object]) -> dict[str, object]:
    error = envelope.get("error")
    fields = safe_diagnostic_fields(
        {"upstream_code": error.get("code"), "upstream_message": error.get("message")}
        if isinstance(error, dict)
        else {}
    )
    fields["category"] = "provider_response" if fields else "unknown"
    return fields


@dataclass(slots=True)
class _ByteCount:
    observed: int = 0
    eof: bool = False


_STDOUT_LIMIT_BYTES = 4 * 1024 * 1024
_STDERR_LIMIT_BYTES = 256 * 1024


async def _read_stream(
    stream: asyncio.StreamReader, *, capture: bytearray | None, limit: int, counter: _ByteCount
) -> None:
    while chunk := await stream.read(64 * 1024):
        counter.observed += len(chunk)
        if capture is not None:
            capture.extend(chunk[: limit - len(capture)])
        if counter.observed > limit:
            raise _execution_failure("output_limit")
    counter.eof = True


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
    tasks: Sequence[asyncio.Task[None] | asyncio.Task[int]],
) -> str | None:
    if spawn is None:
        return None
    try:
        # Keep ownership until CPython POSIX returns the process handle. Popen is
        # synchronous; only local pipe setup yields. An asyncio deadline cannot
        # preempt a blocked OS process-creation call.
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
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(task)
        task.result()
        raise


def _system_argument_limit() -> int | None:
    if sys.platform != "linux":
        return None
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None
    if type(page_size) is not int or page_size <= 0:
        return None
    # Linux MAX_ARG_STRLEN includes the terminating NUL.
    return 32 * page_size - 1


def _prepare_prompt(messages: Sequence[ChatMessage], max_bytes: int) -> PreparedInput:
    original_bytes: int | None = None

    def encode(candidate: Sequence[ChatMessage]) -> bytes:
        nonlocal original_bytes
        content = "\n\n".join(
            f"[{message['role'].upper()}]\n{message['content']}" for message in candidate
        ).encode("utf-8")
        if original_bytes is None:
            original_bytes = len(content)
        return content

    system_limit: int | None = None
    try:
        prepared = fit_messages_to_budget(
            messages,
            max_bytes=max_bytes,
            encode=encode,
            preview_projector=make_input_preview_projector(logging.getLogger(__name__)),
        )
        system_limit = _system_argument_limit()
        if system_limit is not None and len(prepared.content) > system_limit:
            raise InputBudgetUnfit()
        if b"\0" in prepared.content:
            raise InputBudgetUnfit()
        return prepared
    except InputBudgetUnfit:
        sizes = {"limit_bytes": max_bytes}
        if system_limit is not None and system_limit < max_bytes:
            sizes["system_limit_bytes"] = system_limit
        if original_bytes is not None:
            sizes["request_body_original_bytes"] = original_bytes
        log_event(
            logging.getLogger(__name__),
            logging.DEBUG,
            "input_rejected",
            exc_info=False,
            reason="input_budget_unfit",
            **sizes,
        )
    raise _execution_failure("input_budget_unfit")


async def run_antigravity(
    model: str | None,
    messages: Sequence[ChatMessage],
    *,
    provider_name: str,
    command: str,
    timeout_seconds: float,
    pass_env: Sequence[str],
    quota: CapacityGate | None,
    acquire_rate_permit: Callable[[], Awaitable[float]] | None = None,
    prompt_max_bytes: int = 98304,
) -> str:
    prepared = _prepare_prompt(messages, prompt_max_bytes)
    prompt = prepared.content.decode("utf-8")
    timeout_argument = format(Decimal(str(timeout_seconds)), "f")
    if "." in timeout_argument:
        timeout_argument = timeout_argument.rstrip("0").rstrip(".")
    argv = [
        command,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--print-timeout",
        f"{timeout_argument}s",
    ]
    if model is not None:
        argv.extend(["--model", model])
    async with quota.lease() if quota is not None else nullcontext():
        with TemporaryDirectory(prefix="agent-search-") as directory:
            spawn: asyncio.Task[asyncio.subprocess.Process] | None = None
            spawn_attempted = False
            stdout_capture = bytearray()
            tasks: list[asyncio.Task[None] | asyncio.Task[int]] = []
            succeeded = False
            process: asyncio.subprocess.Process | None = None
            stdout_count, stderr_count = _ByteCount(), _ByteCount()
            diagnostics: dict[str, object] = {}
            failure: ExecutionFailure | None = None
            cancelled = False
            cleanup_failed = False

            def record_diagnostic() -> dict[str, object]:
                # Cleanup may acquire the handle after cancellation during shielded spawn.
                known_process = process
                if (
                    known_process is None
                    and spawn is not None
                    and spawn.done()
                    and not spawn.cancelled()
                    and spawn.exception() is None
                ):
                    known_process = spawn.result()
                reason: str | None = "success" if succeeded else "execution_failure"
                if failure is not None:
                    reason = failure.reason
                category = diagnostics.get(
                    "category", failure.reason if failure is not None else "unknown"
                )
                if cleanup_failed:
                    reason, category = "execution_failure", "unknown"
                if cancelled:
                    reason, category = "cancelled", "cancelled"
                failed = not succeeded or failure is not None or cancelled or cleanup_failed
                fields = safe_diagnostic_fields(
                    {
                        **diagnostics,
                        "category": category,
                        "semantic_result": "error" if failed else "success",
                        "exit_code": known_process.returncode
                        if known_process is not None
                        else None,
                        "stdout_bytes": stdout_count.observed,
                        "stderr_bytes": stderr_count.observed,
                    }
                )
                if failure is not None:
                    failure.diagnostics = fields
                log_event(
                    logging.getLogger(__name__),
                    logging.DEBUG,
                    "agent_process_diagnostic",
                    exc_info=False,
                    **fields,
                    provider=provider_name,
                    model=model,
                    stage="agent_process",
                    elapsed_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                    reason=reason,
                )
                return fields

            if acquire_rate_permit is not None:
                try:
                    await acquire_rate_permit()
                except RateLimitWaitTimeout:
                    raise _execution_failure("rate_limit_wait_timeout") from None

            started = asyncio.get_running_loop().time()
            try:
                async with asyncio.timeout(timeout_seconds + _HARD_TIMEOUT_MARGIN_SECONDS):
                    env = _curated_environment(pass_env)
                    spawn_attempted = True
                    spawn = asyncio.create_task(
                        asyncio.create_subprocess_exec(
                            *argv,
                            cwd=directory,
                            env=env,
                            start_new_session=True,
                            stdin=asyncio.subprocess.DEVNULL,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                    )
                    process = await asyncio.shield(spawn)
                    assert process.stdout is not None and process.stderr is not None
                    stdout = asyncio.create_task(
                        _read_stream(
                            process.stdout,
                            capture=stdout_capture,
                            limit=_STDOUT_LIMIT_BYTES,
                            counter=stdout_count,
                        )
                    )
                    stderr = asyncio.create_task(
                        _read_stream(
                            process.stderr,
                            capture=None,
                            limit=_STDERR_LIMIT_BYTES,
                            counter=stderr_count,
                        )
                    )
                    wait = asyncio.create_task(process.wait())
                    tasks.extend((stdout, stderr, wait))
                    await asyncio.gather(*tasks)
                    response = _decode_terminal_response(
                        stdout_capture, wait.result(), diagnostics=diagnostics
                    )
                    succeeded = True
                    return response
            except TimeoutError:
                failure = _execution_failure("timeout")
                raise failure from None
            except OSError as exc:
                diagnostics["category"] = "spawn"
                reason = "argument_limit" if exc.errno == errno.E2BIG else "spawn_failure"
                failure = _execution_failure(reason)
                raise failure from None
            except asyncio.CancelledError:
                cancelled = True
                raise
            except ExecutionFailure as exc:
                failure = exc
                raise
            finally:
                # Cleanup can force EOF on a pipe that was still open at interruption.
                stdout_incomplete = not stdout_count.eof
                cleanup = asyncio.create_task(_cleanup_process(spawn, tasks))
                try:
                    cleanup_reason = await _finish_cleanup(cleanup)
                    if succeeded and cleanup_reason is not None:
                        diagnostics["category"] = "unknown"
                        failure = _execution_failure(cleanup_reason)
                        raise failure
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                except Exception:
                    cleanup_failed = True
                    raise
                finally:
                    best_effort_diagnostics(record_diagnostic)
                    if spawn_attempted:
                        with suppress(Exception):
                            log_upstream_body_preview(
                                logging.getLogger(__name__),
                                provider=provider_name,
                                stage="agent_process",
                                endpoint=None,
                                attempt=1,
                                transport="agent_cli",
                                prepared_body=prepared,
                                response_body=lambda: bytes(stdout_capture),
                                response_body_bytes=stdout_count.observed,
                                response_incomplete=stdout_incomplete,
                            )
