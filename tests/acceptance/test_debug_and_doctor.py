import asyncio
import json
import logging
from io import StringIO
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from agent_search_gateway.cli import build_parser, run_command
from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.llm.stages import LLMStages
from agent_search_gateway.models import LLMInvocation, RetryPolicy
from agent_search_gateway.observability import (
    DebugLoggingSession,
    SecretValue,
    configure_debug_logging,
    log_event,
)
from agent_search_gateway.orchestrators.search import SearchOrchestrator
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.contracts import KeywordSearchHit
from agent_search_gateway.providers.http import HttpJsonExecutor
from agent_search_gateway.providers.web.tavily import TavilyAdapter
from agent_search_gateway.result_writer import ResultWriter
from agent_search_gateway.url_normalization import normalize_url
from agent_search_gateway.url_store import URLStore
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
from tests.support.fakes import FakeKeywordSearchProvider, FakeLLMClient


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


@pytest.mark.parametrize("debug", [True, False])
async def test_concurrent_workflows_isolate_terminal_counts_and_private_rotated_logs(
    tmp_path: Path,
    debug: bool,
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    queries = {"alpha": "QUERY_ALPHA_SENTINEL", "beta": "QUERY_BETA_SENTINEL"}
    attempts: dict[tuple[str, str], int] = {}
    entered: set[str] = set()
    both_started = asyncio.Event()
    credential = SecretValue("[REDACTED_SECRET]")

    async def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        label = next(label for label, value in queries.items() if value == query)
        provider = request.url.host.split(".")[0]
        key = (provider, label)
        attempt = attempts[key] = attempts.get(key, 0) + 1
        if provider == "good" and attempt == 1:
            entered.add(label)
            if len(entered) == 2:
                both_started.set()
            await both_started.wait()
        statuses = [429, 200] if label == "alpha" else [429, 502, 200]
        status = statuses[attempt - 1] if provider == "good" else 429
        if status != 200:
            return httpx.Response(
                status,
                json={
                    "error": {
                        "code": f"limited_{label}",
                        "type": "quota",
                        "message": "Retry "
                        + credential.reveal()
                        + " https://private.test/error?query=URL_SENTINEL"
                        + " safe " * 130,
                    }
                },
            )
        count = 2 if label == "alpha" else 3
        invalid: list[object] = (
            [{"title": "PAYLOAD_SENTINEL"}] * 3 if label == "alpha" else ["PAYLOAD_SENTINEL", 99]
        )
        return httpx.Response(
            200,
            json={
                "results": [
                    *[
                        {"url": f"https://example.test/{label}/{i}", "content": "BODY_SENTINEL"}
                        for i in range(count)
                    ],
                    *invalid,
                ]
            },
        )

    async def sleep(delay: float) -> None:
        await asyncio.sleep(0)

    stderr = StringIO()
    session = (
        configure_debug_logging(
            paths.debug_log_file,
            stderr=stderr,
            max_bytes=1600,
            backup_count=100,
        )
        if debug
        else None
    )
    if session is not None:
        session.add_secrets(credential)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        providers = [
            TavilyAdapter(
                name=name,
                api_url=f"https://{name}.example.test",
                secret=credential,
                http_executor=HttpJsonExecutor(
                    client,
                    RetryPolicy(3, 0.01, 2, 1),
                    provider_name=name,
                    sleep=sleep,
                ),
            )
            for name in ("good", "bad")
        ]
        invocation = LLMInvocation("judge", "model")
        stages = LLMStages(
            {"judge": FakeLLMClient("judge")},
            judge=invocation,
            safety=invocation,
            content_clean=invocation,
            focus_summary=invocation,
        )
        runtime = build_debug_acceptance_runtime(paths)
        runtime.search_orchestrator = SearchOrchestrator(
            keyword_providers=providers,
            llm_invocations=(),
            quotas=ProviderQuotaManager(web_limits={"good": 2, "bad": 2}, llm_limits={}),
            stages=stages,
            store=URLStore(),
            result_writer=ResultWriter(paths.results_dir),
        )
        ids = iter(("11111111", "22222222"))
        daemon = ForegroundDaemon(
            paths,
            runtime_factory=lambda: runtime,
            request_id_factory=lambda: next(ids),
            debug=debug,
            logging_session=session,
        )
        task = asyncio.create_task(daemon.start())
        outputs = {label: (StringIO(), StringIO()) for label in queries}
        try:
            await asyncio.wait_for(daemon.ready.wait(), 1)
            async with asyncio.timeout(2):
                codes = await asyncio.gather(
                    *(
                        run_command(
                            build_parser().parse_args(["keyword-search", query]),
                            paths,
                            stdout=outputs[label][0],
                            stderr=outputs[label][1],
                        )
                        for label, query in queries.items()
                    )
                )
        finally:
            await daemon.stop_for_test()
            await task
            if session is not None:
                session.close()

    assert codes == [0, 0]
    request_ids: dict[str, str] = {}
    for label, (stdout, business_stderr) in outputs.items():
        assert business_stderr.getvalue() == ""
        result_path = Path(stdout.getvalue().strip())
        assert stdout.getvalue() == f"{result_path}\n"
        request_ids[label] = result_path.stem.removeprefix("keyword-")
        records = [json.loads(line) for line in result_path.read_text().splitlines()]
        assert len(records) == (2 if label == "alpha" else 3)
        assert all(set(record) == {"url", "abstract"} for record in records)
        assert all(f"/{label}/" in record["url"] for record in records)
    assert len(set(request_ids.values())) == 2
    if not debug:
        assert not paths.debug_log_file.exists()
        assert stderr.getvalue() == ""
        return

    files = list(paths.debug_log_file.parent.glob(paths.debug_log_file.name + "*"))
    assert len(files) > 1
    file_log = "\n".join(path.read_text() for path in files)
    for logged in (file_log, stderr.getvalue()):
        assert "SENTINEL" not in logged
        assert credential.reveal() not in logged
        assert "<redacted>" in logged
        assert "upstream_message_truncated=true" in logged
        assert "traceback=" not in logged
        assert "event=workflow_failed " not in logged
        for label, request_id in request_ids.items():
            lines = [line for line in logged.splitlines() if f"request={request_id} " in line]
            assert sum("event=workflow_completed " in line for line in lines) == 1
            assert sum("event=provider_started" in line for line in lines) == 2
            completed = [line for line in lines if "event=provider_completed " in line]
            failed = [line for line in lines if "event=provider_failed " in line]
            assert len(completed) == len(failed) == 1
            assert "provider=good " in completed[0]
            assert "provider=bad " in failed[0]
            assert f"upstream_code=limited_{label}" in failed[0]
            assert f"limited_{'beta' if label == 'alpha' else 'alpha'}" not in "\n".join(lines)
            assert sum("event=http_failed " in line for line in lines) == 1
            good_lines = [line for line in lines if "provider=good " in line]
            assert sum("event=http_retrying " in line for line in good_lines) == (
                1 if label == "alpha" else 2
            )
            summaries = [
                line
                for line in good_lines
                if "event=provider_candidates_summary " in line and "phase=adapter" in line
            ]
            assert len(summaries) == 1
            assert "phase=adapter" in summaries[0]
            assert "received=5" in summaries[0]
            assert f"accepted={2 if label == 'alpha' else 3}" in summaries[0]
            assert f"rejected={3 if label == 'alpha' else 2}" in summaries[0]
            assert ("missing_url" if label == "alpha" else "invalid_record") in summaries[0]
            staging = [
                line
                for line in good_lines
                if "event=provider_candidates_summary " in line and "phase=staging" in line
            ]
            assert len(staging) == 1
            assert f"received={2 if label == 'alpha' else 3}" in staging[0]
            assert f"accepted={2 if label == 'alpha' else 3}" in staging[0]
            assert "rejected=0" in staging[0] and "unprocessed=0" in staging[0]


async def test_keyword_redirect_body_rejection_logs_omit_signed_target_query(
    tmp_path: Path,
) -> None:
    target = "https://example.org/article?signature=[REDACTED_SECRET]&token=[REDACTED_SECRET]"
    wrapper = "https://www.google.com/goto?url=" + quote(target, safe="")
    invocation = LLMInvocation("judge", "model", {})
    stages = LLMStages(
        {"judge": FakeLLMClient("judge", json_result={"ok": False})},
        judge=invocation,
        safety=invocation,
        content_clean=invocation,
        focus_summary=invocation,
    )
    store = URLStore()
    provider = FakeKeywordSearchProvider(
        "web",
        [
            KeywordSearchHit(wrapper, snippet="Retained abstract", raw_content="Rejected body"),
            KeywordSearchHit(target, snippet="Later abstract", raw_content="Rejected body"),
            KeywordSearchHit(
                "https://example.org/empty?signature=[REDACTED_SECRET]",
                snippet="",
            ),
        ],
    )
    log_file = tmp_path / "debug.log"
    session = configure_debug_logging(log_file, stderr=StringIO())
    try:
        search = SearchOrchestrator(
            keyword_providers=[provider],
            llm_invocations=(),
            quotas=ProviderQuotaManager(web_limits={"web": 1}, llm_limits={}),
            stages=stages,
            store=store,
            result_writer=ResultWriter(tmp_path / "results"),
        )
        path = Path(await search.keyword_search("query", request_id="ac000001"))
    finally:
        session.close()

    assert json.loads(path.read_text()) == {"url": target, "abstract": "Retained abstract"}
    snapshot = store.get(normalize_url(target))
    assert snapshot is not None and snapshot.raw_content == snapshot.content == ""
    assert store.get(normalize_url(wrapper)) is None
    logged = log_file.read_text()
    lines = logged.splitlines()
    for event, reason in [
        ("candidate_rejected", "empty_abstract"),
        ("candidate_rejected", "duplicate"),
        ("body_rejected", "judge_rejected"),
    ]:
        assert any(f"event={event}" in line and f"reason={reason}" in line for line in lines)
    summary = [line for line in lines if "event=provider_candidates_summary" in line]
    assert len(summary) == 1
    assert "accepted=2" in summary[0] and "rejected=1" in summary[0] and "rewritten=1" in summary[0]
    assert "phase=staging" in summary[0]
    assert "[REDACTED_SECRET]" not in logged
    assert "signature=" not in logged and "token=" not in logged
    assert "Rejected body" not in logged and "Retained abstract" not in logged
