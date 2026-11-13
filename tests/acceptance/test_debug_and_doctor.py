import asyncio
import json
import logging
from io import StringIO
from pathlib import Path

from agent_search_gateway.cli import build_parser, run_command
from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.observability import DebugLoggingSession, SecretValue, log_event
from agent_search_gateway.paths import RuntimePaths
from tests.doctor._support import environment_name, write_valid_config
from tests.support.acceptance import (
    DEBUG_CREDENTIAL_SENTINEL,
    DEBUG_MODEL_RESPONSE_SENTINEL,
    DEBUG_PAGE_ACCEPT_SENTINEL,
    DEBUG_PAGE_REJECT_SENTINEL,
    DEBUG_QUERY_SENTINEL,
    DebugAcceptanceRuntime,
    build_debug_acceptance_runtime,
)


async def _start_acceptance_daemon(
    paths: RuntimePaths,
    runtime: DebugAcceptanceRuntime,
    *,
    debug: bool,
    request_id: str,
) -> tuple[asyncio.Task[int], ForegroundDaemon, StringIO, StringIO]:
    parser = build_parser()
    factory_called = asyncio.Event()
    daemon_holder: list[ForegroundDaemon] = []
    stdout = StringIO()
    stderr = StringIO()

    def factory(
        received_paths: RuntimePaths,
        *,
        debug: bool,
        logging_session: DebugLoggingSession | None,
    ) -> ForegroundDaemon:
        assert received_paths == paths
        if logging_session is not None:
            traceback_secret = f"{runtime.credential_sentinel}_TRACEBACK_ONLY"
            logging_session.add_secrets(
                SecretValue(runtime.credential_sentinel),
                SecretValue(traceback_secret),
            )
            project_logger = logging.getLogger("agent_search_gateway.acceptance")
            log_event(
                project_logger,
                logging.DEBUG,
                "credential_redaction_probe",
                detail=runtime.credential_sentinel,
            )
            try:
                raise RuntimeError(traceback_secret)
            except RuntimeError:
                log_event(
                    project_logger,
                    logging.ERROR,
                    "credential_traceback_redaction_probe",
                    exc_info=True,
                )
        daemon = ForegroundDaemon(
            paths,
            runtime_factory=lambda: runtime,
            request_id_factory=lambda: request_id,
            debug=debug,
            logging_session=logging_session,
        )
        daemon_holder.append(daemon)
        factory_called.set()
        return daemon

    argv = ["start", "--debug"] if debug else ["start"]
    task = asyncio.create_task(
        run_command(
            parser.parse_args(argv),
            paths,
            daemon_factory=factory,
            stdout=stdout,
            stderr=stderr,
        )
    )
    await asyncio.wait_for(factory_called.wait(), timeout=1.0)
    daemon = daemon_holder[0]
    ready_task = asyncio.create_task(daemon.ready.wait())
    done, pending = await asyncio.wait(
        {task, ready_task},
        timeout=1.0,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if task in done and not daemon.ready.is_set():
        ready_task.cancel()
        await asyncio.gather(ready_task, return_exceptions=True)
        raise AssertionError(
            f"daemon exited before ready: code={task.result()} stderr={stderr.getvalue()!r}"
        )
    if ready_task not in done:
        for pending_task in pending:
            pending_task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        raise AssertionError("daemon did not become ready")
    return task, daemon, stdout, stderr


async def test_debug_cli_workflow_preserves_public_contract_and_correlates_trace(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    runtime = build_debug_acceptance_runtime(paths)
    start_task, _daemon, _start_stdout, start_stderr = await _start_acceptance_daemon(
        paths,
        runtime,
        debug=True,
        request_id="a1b2c3d4",
    )
    parser = build_parser()

    business_stdout = StringIO()
    business_stderr = StringIO()
    keyword_code = await run_command(
        parser.parse_args(["keyword-search", DEBUG_QUERY_SENTINEL]),
        paths,
        stdout=business_stdout,
        stderr=business_stderr,
    )
    stop_stdout = StringIO()
    stop_stderr = StringIO()
    stop_code = await run_command(
        parser.parse_args(["stop"]),
        paths,
        stdout=stop_stdout,
        stderr=stop_stderr,
    )
    start_code = await asyncio.wait_for(start_task, timeout=2.0)

    assert keyword_code == 0
    assert business_stderr.getvalue() == ""
    result_path = Path(business_stdout.getvalue().strip())
    assert result_path.name == "keyword-a1b2c3d4.jsonl"
    assert business_stdout.getvalue() == f"{result_path}\n"
    lines = result_path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert records == [
        {
            "url": "https://example.com/accepted?id=42&mode=test",
            "abstract": "Accepted abstract",
        },
        {
            "url": "https://example.com/rejected?id=43&mode=test",
            "abstract": "Rejected abstract",
        },
    ]
    assert all(set(record) == {"url", "abstract"} for record in records)
    assert stop_code == 0
    assert stop_stdout.getvalue() == "Daemon stopped.\n"
    assert stop_stderr.getvalue() == ""
    assert start_code == 0
    assert runtime.close_calls == 1

    debug_log = paths.debug_log_file.read_text(encoding="utf-8")
    debug_stderr = start_stderr.getvalue()
    assert DEBUG_CREDENTIAL_SENTINEL not in debug_log
    assert DEBUG_CREDENTIAL_SENTINEL not in debug_stderr
    assert f"{DEBUG_CREDENTIAL_SENTINEL}_TRACEBACK_ONLY" not in debug_log
    assert f"{DEBUG_CREDENTIAL_SENTINEL}_TRACEBACK_ONLY" not in debug_stderr
    assert "event=credential_redaction_probe" in debug_log
    assert "event=credential_traceback_redaction_probe" in debug_log
    assert "traceback=" in debug_log
    assert "<redacted>" in debug_log
    assert "<redacted>" in debug_stderr
    request_lines = [line for line in debug_log.splitlines() if "request=a1b2c3d4" in line]
    assert "event=session_started" in debug_log
    assert "event=session_stopped" in debug_log
    assert any("event=workflow_started" in line for line in request_lines)
    assert any("event=workflow_completed" in line for line in request_lines)
    assert any(
        "event=provider_started" in line and "provider=keyword" in line for line in request_lines
    )
    assert any(
        "event=provider_completed" in line and "provider=keyword" in line for line in request_lines
    )
    assert any("event=body_accepted" in line for line in request_lines)
    assert any(
        "event=body_rejected" in line and "reason=judge_rejected" in line for line in request_lines
    )
    assert any(
        "event=results_written" in line and str(result_path) in line for line in request_lines
    )
    assert "url=https://example.com/accepted " in debug_log
    assert "?id=42&mode=test" not in debug_log
    assert "url=https://example.com/rejected " in debug_log
    assert "?id=43&mode=test" not in debug_log

    for sentinel in (
        DEBUG_QUERY_SENTINEL,
        DEBUG_PAGE_ACCEPT_SENTINEL,
        DEBUG_PAGE_REJECT_SENTINEL,
        DEBUG_MODEL_RESPONSE_SENTINEL,
        DEBUG_CREDENTIAL_SENTINEL,
    ):
        assert sentinel not in debug_log
    assert "httpx" not in debug_log
    assert "httpcore" not in debug_log


async def test_normal_mode_equivalent_workflow_creates_no_debug_log(tmp_path: Path) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "normal.toml",
        socket_file=tmp_path / "normal.sock",
        results_dir=tmp_path / "normal-results",
    )
    runtime = build_debug_acceptance_runtime(paths)
    start_task, _daemon, start_stdout, start_stderr = await _start_acceptance_daemon(
        paths,
        runtime,
        debug=False,
        request_id="b1b2c3d4",
    )
    parser = build_parser()

    stdout = StringIO()
    stderr = StringIO()
    keyword_code = await run_command(
        parser.parse_args(["keyword-search", "normal query"]),
        paths,
        stdout=stdout,
        stderr=stderr,
    )
    stop_code = await run_command(
        parser.parse_args(["stop"]),
        paths,
        stdout=StringIO(),
        stderr=StringIO(),
    )
    start_code = await asyncio.wait_for(start_task, timeout=2.0)

    assert keyword_code == 0
    assert Path(stdout.getvalue().strip()).name == "keyword-b1b2c3d4.jsonl"
    assert stderr.getvalue() == ""
    assert stop_code == 0
    assert start_code == 0
    assert start_stdout.getvalue() == ""
    assert start_stderr.getvalue() == ""
    assert not paths.debug_log_file.exists()


async def test_doctor_cli_acceptance_is_local_aggregating_and_side_effect_free(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths.from_home(tmp_path / "doctor-home")
    write_valid_config(paths.config_file)
    parser = build_parser()
    env = {environment_name(): "opaque-runtime-value-7391"}
    config_before = paths.config_file.read_text(encoding="utf-8")

    stdout = StringIO()
    stderr = StringIO()
    code = await run_command(
        parser.parse_args(["doctor"]),
        paths,
        environ=env,
        stdout=stdout,
        stderr=stderr,
    )
    assert code == 0
    assert stderr.getvalue() == ""
    output = stdout.getvalue()
    assert "[ok] configuration valid" in output
    assert f"[ok] environment variable {environment_name()} is set" in output
    assert output.count("[ok] directory is creatable:") == 3
    assert "[info] daemon not running" in output
    assert "opaque-runtime-value-7391" not in output
    assert paths.config_file.read_text(encoding="utf-8") == config_before
    assert not paths.socket_file.parent.exists()
    assert not paths.results_dir.exists()
    assert not paths.logs_dir.exists()
    assert not paths.debug_log_file.exists()
    assert not paths.socket_file.exists()

    failed_stdout = StringIO()
    failed_stderr = StringIO()
    failed_code = await run_command(
        parser.parse_args(["doctor"]),
        paths,
        environ={},
        stdout=failed_stdout,
        stderr=failed_stderr,
    )
    assert failed_code == 1
    assert failed_stderr.getvalue() == ""
    failed_output = failed_stdout.getvalue()
    assert "[fail] configuration invalid:" in failed_output
    assert failed_output.count("[ok] directory is creatable:") == 3
    assert "[info] daemon not running" in failed_output
    assert paths.config_file.read_text(encoding="utf-8") == config_before
    assert not paths.socket_file.parent.exists()
    assert not paths.results_dir.exists()
    assert not paths.logs_dir.exists()
    assert not paths.debug_log_file.exists()
    assert not paths.socket_file.exists()
