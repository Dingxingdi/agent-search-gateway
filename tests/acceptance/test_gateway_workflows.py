import asyncio
import io
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_search_gateway.cli import build_parser, run_command
from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.errors import ErrorCode
from agent_search_gateway.models import (
    ErrorResponse,
    KeywordSearchRequest,
    LLMSearchRequest,
    PaperSearchRequest,
    ShutdownRequest,
    SuccessResponse,
    URLFetchRequest,
)
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.protocol import parse_response_frame, send_request
from agent_search_gateway.url_normalization import normalize_url
from tests.support.acceptance import (
    FilteringAcceptanceRuntime,
    build_acceptance_runtime,
    build_filtering_acceptance_runtime,
)

_DAEMON_TIMEOUT_SECONDS = 2.0


async def _start_daemon(daemon: ForegroundDaemon) -> asyncio.Task[None]:
    """Start a daemon with a bounded readiness wait and surface startup failures."""
    daemon_task = asyncio.create_task(daemon.start())
    ready_task = asyncio.create_task(daemon.ready.wait())
    done, pending = await asyncio.wait(
        {daemon_task, ready_task},
        timeout=_DAEMON_TIMEOUT_SECONDS,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if ready_task in done and daemon.ready.is_set():
        return daemon_task

    ready_task.cancel()
    await asyncio.gather(ready_task, return_exceptions=True)
    if daemon_task in done:
        await daemon_task
        raise AssertionError("daemon exited before becoming ready")

    for pending_task in pending:
        pending_task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    raise AssertionError("daemon did not become ready")


@asynccontextmanager
async def _running_daemon(daemon: ForegroundDaemon) -> AsyncIterator[asyncio.Task[None]]:
    """Run a daemon and guarantee bounded cleanup when an assertion fails."""
    daemon_task = await _start_daemon(daemon)
    try:
        yield daemon_task
    finally:
        if not daemon_task.done():
            await asyncio.wait_for(daemon.stop_for_test(), timeout=_DAEMON_TIMEOUT_SECONDS)
        await asyncio.wait_for(asyncio.shield(daemon_task), timeout=_DAEMON_TIMEOUT_SECONDS)


async def test_real_socket_workflows_match_public_contract_without_network(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    runtime = build_acceptance_runtime(paths)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with _running_daemon(daemon) as daemon_task:
        keyword = await send_request(paths.socket_file, KeywordSearchRequest("find article"))
        assert isinstance(keyword, SuccessResponse)
        keyword_path = Path(keyword.text)
        assert keyword_path.exists()
        keyword_lines = keyword_path.read_text(encoding="utf-8").splitlines()
        assert [json.loads(line) for line in keyword_lines] == [
            {"url": "https://example.com/article", "abstract": "Keyword abstract"}
        ]
        assert all(set(json.loads(line)) == {"url", "abstract"} for line in keyword_lines)

        full = await send_request(
            paths.socket_file,
            URLFetchRequest("https://example.com/article", None),
        )
        assert full == SuccessResponse("Full article content")
        assert len(runtime.fetch_provider.calls) == 1

        focused = await send_request(
            paths.socket_file,
            URLFetchRequest("https://example.com/article", "pricing"),
        )
        assert focused == SuccessResponse("Focused summary: pricing")
        assert len(runtime.fetch_provider.calls) == 1

        llm = await send_request(paths.socket_file, LLMSearchRequest("find llm result"))
        assert isinstance(llm, SuccessResponse)
        llm_lines = Path(llm.text).read_text(encoding="utf-8").splitlines()
        assert [json.loads(line) for line in llm_lines] == [
            {"url": "https://example.com/llm", "abstract": "LLM abstract"}
        ]

        paper_stdout = io.StringIO()
        paper_stderr = io.StringIO()
        paper_args = build_parser().parse_args(["paper-search", "academic topic"])
        assert (
            await run_command(
                paper_args,
                paths,
                stdout=paper_stdout,
                stderr=paper_stderr,
            )
            == 0
        )
        assert paper_stderr.getvalue() == ""
        paper_path = Path(paper_stdout.getvalue().strip())
        assert paper_path.name.startswith("paper-")
        assert paper_path.name.endswith(".jsonl")
        assert len(paper_path.stem.removeprefix("paper-")) == 8
        paper_lines = [
            json.loads(line) for line in paper_path.read_text(encoding="utf-8").splitlines()
        ]
        assert [line["title"] for line in paper_lines] == [
            "Direct Academic Paper",
            "Unique CORE Paper",
        ]
        assert all("type" not in line for line in paper_lines)
        assert paper_lines[0]["sources"] == ["openalex", "core"]
        assert paper_lines[0]["pdf_url"] == "https://repository.example/paper.pdf"
        assert paper_lines[0]["is_open_access"] is True
        assert runtime.oa_resolver.calls == ["10.1000/acceptance-shared"]

        paper_fetch = await send_request(
            paths.socket_file,
            URLFetchRequest("https://example.com/paper", None),
        )
        assert paper_fetch == SuccessResponse("Full article content")
        assert [str(url) for url in runtime.fetch_provider.calls] == [
            "https://example.com/article",
            "https://example.com/paper",
        ]
        pdf_fetch = await send_request(
            paths.socket_file,
            URLFetchRequest("https://repository.example/paper.pdf", None),
        )
        assert pdf_fetch == ErrorResponse(
            ErrorCode.URL_NOT_ADMITTED,
            "URL was not admitted by search",
        )

        mixed_stdout = io.StringIO()
        mixed_stderr = io.StringIO()
        mixed_args = build_parser().parse_args(
            ["llm-search", "find web and papers", "--scope", "all"]
        )
        assert (
            await run_command(
                mixed_args,
                paths,
                stdout=mixed_stdout,
                stderr=mixed_stderr,
            )
            == 0
        )
        assert mixed_stderr.getvalue() == ""
        mixed_path = Path(mixed_stdout.getvalue().strip())
        assert mixed_path.name.startswith("llm-")
        mixed_lines = [
            json.loads(line) for line in mixed_path.read_text(encoding="utf-8").splitlines()
        ]
        assert [line["type"] for line in mixed_lines] == ["web", "paper"]
        assert mixed_lines[0]["url"] == "https://example.com/llm"
        assert mixed_lines[1]["title"] == "LLM Academic Paper"

        assert await send_request(paths.socket_file, ShutdownRequest()) == SuccessResponse(
            "Daemon stopped."
        )
        await daemon_task
        assert not paths.socket_file.exists()

    fresh_runtime = build_acceptance_runtime(paths)
    fresh_daemon = ForegroundDaemon(paths, runtime_factory=lambda: fresh_runtime)
    async with _running_daemon(fresh_daemon) as fresh_task:
        after_restart = await send_request(
            paths.socket_file,
            URLFetchRequest("https://example.com/article", None),
        )
        assert after_restart == ErrorResponse(
            ErrorCode.URL_NOT_ADMITTED,
            "URL was not admitted by search",
        )
        assert fresh_runtime.fetch_provider.calls == []

        assert await send_request(paths.socket_file, ShutdownRequest()) == SuccessResponse(
            "Daemon stopped."
        )
        await fresh_task
        assert not paths.socket_file.exists()


async def _filtering_cli(paths: RuntimePaths, argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = await asyncio.wait_for(
        run_command(build_parser().parse_args(argv), paths, stdout=stdout, stderr=stderr),
        timeout=_DAEMON_TIMEOUT_SECONDS,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def _successful_path(outcome: tuple[int, str, str]) -> Path:
    code, stdout, stderr = outcome
    assert code == 0 and stderr == ""
    path = Path(stdout.removesuffix("\n"))
    assert path.is_absolute() and path.is_file()
    assert stdout == f"{path}\n"
    return path


def _filtering_snapshot(
    runtime: FilteringAcceptanceRuntime, paths: RuntimePaths
) -> tuple[object, ...]:
    urls = [f"https://example.com/keyword/{p.name}" for p in runtime.keyword_providers]
    urls += [f"https://example.com/paper/{p.name}" for p in runtime.academic_providers]
    urls += [f"https://example.com/llm/{name}" for name in runtime.llm_clients]
    return (
        tuple(tuple(p.calls) for p in runtime.keyword_providers),
        tuple(tuple(p.calls) for p in runtime.academic_providers),
        tuple(
            (name, repr(client.text_calls), repr(client.json_calls))
            for name, client in runtime.llm_clients.items()
        ),
        tuple(runtime.oa_resolver.calls),
        tuple(runtime.fetch_provider.calls),
        tuple((p.name, p.read_bytes()) for p in sorted(paths.results_dir.glob("*.jsonl"))),
        tuple(repr(runtime.store.get(normalize_url(url))) for url in urls),
    )


async def test_cli_filters_keyword_and_paper_without_persisting_selection(tmp_path: Path) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    runtime = build_filtering_acceptance_runtime(paths)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with _running_daemon(daemon):
        first = _successful_path(
            await _filtering_cli(paths, ["keyword-search", "selected", "--provider", " beta "])
        )
        assert [p.calls for p in runtime.keyword_providers] == [[], ["selected"]]
        assert first.read_text() == '{"url":"https://example.com/keyword/beta","abstract":"beta"}\n'
        second = _successful_path(await _filtering_cli(paths, ["keyword-search", "all"]))
        assert [p.calls for p in runtime.keyword_providers] == [["all"], ["selected", "all"]]
        assert [json.loads(line)["abstract"] for line in second.read_text().splitlines()] == [
            "alpha",
            "beta",
        ]
        paper = _successful_path(
            await _filtering_cli(paths, ["paper-search", "papers", "--provider", "arxiv"])
        )
        assert [p.calls for p in runtime.academic_providers] == [["papers"], []]
        [record] = [json.loads(line) for line in paper.read_text().splitlines()]
        assert record["sources"] == ["arxiv"] and "type" not in record
        assert runtime.oa_resolver.calls == ["10.1000/arxiv"]
    assert runtime.close_calls == 1


@pytest.mark.parametrize(
    ("flags", "indexes"),
    [
        (["--provider", "openai_main"], [0, 1]),
        (["--model", "gpt-5"], [0, 3]),
        (["--provider", "openai_main", "--model", "gpt-5"], [0]),
    ],
)
async def test_cli_llm_selectors_reach_real_search_pipeline(
    tmp_path: Path, flags: list[str], indexes: list[int]
) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    runtime = build_filtering_acceptance_runtime(paths)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with _running_daemon(daemon):
        path = _successful_path(await _filtering_cli(paths, ["llm-search", "prompt", *flags]))
        selected = [runtime.llm_invocations[index] for index in indexes]
        for name, client in runtime.llm_clients.items():
            assert [item for item, _ in client.text_calls] == [
                item for item in selected if item.provider == name
            ]
            assert all(messages[-1]["content"] == "prompt" for _, messages in client.text_calls)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert all(set(record) == {"url", "abstract"} for record in records)
        assert {record["abstract"] for record in records} == {item.provider for item in selected}
        await _filtering_cli(paths, ["llm-search", "unfiltered"])
        assert (
            sum(len(client.text_calls) for client in runtime.llm_clients.values())
            == len(selected) + 4
        )


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (
            ["keyword-search", "query", "--provider", "unknown"],
            "No enabled keyword-search provider matches 'unknown'",
        ),
        (
            ["paper-search", "query", "--provider", "unknown"],
            "No enabled paper-search provider matches 'unknown'",
        ),
        (
            ["llm-search", "prompt", "--provider", "openai_main", "--model", "deepseek-v3"],
            "No LLM search invocation matches provider 'openai_main' with model 'deepseek-v3'",
        ),
        (
            ["llm-search", "prompt", "--provider", "missing", "--model", "missing-model"],
            "No LLM search invocation matches provider 'missing' or model 'missing-model'",
        ),
        (
            ["llm-search", "prompt", "--provider", "missing", "--model", "gpt-5"],
            "No LLM search invocation matches provider 'missing'",
        ),
        (
            ["llm-search", "prompt", "--provider", "openai_main", "--model", "missing-model"],
            "No LLM search invocation matches model 'missing-model'",
        ),
    ],
)
async def test_cli_bad_selection_is_precise_and_has_no_search_side_effects(
    tmp_path: Path, argv: list[str], message: str
) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    runtime = build_filtering_acceptance_runtime(paths)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with _running_daemon(daemon):
        _successful_path(
            await _filtering_cli(paths, ["keyword-search", "baseline", "--provider", "beta"])
        )
        before = _filtering_snapshot(runtime, paths)
        assert await _filtering_cli(paths, argv) == (1, "", message + "\n")
        assert _filtering_snapshot(runtime, paths) == before


@pytest.mark.parametrize(
    ("search_request", "message"),
    [
        (KeywordSearchRequest("query", provider=" \t "), "Provider must not be empty"),
        (PaperSearchRequest("query", provider=""), "Provider must not be empty"),
        (LLMSearchRequest("prompt", provider=" "), "Provider must not be empty"),
        (LLMSearchRequest("prompt", model=" "), "Model must not be empty"),
        (
            KeywordSearchRequest("query", provider=" beta "),
            "No enabled keyword-search provider matches ' beta '",
        ),
    ],
)
async def test_direct_socket_blank_selectors_do_not_restore_full_search(
    tmp_path: Path,
    search_request: KeywordSearchRequest | PaperSearchRequest | LLMSearchRequest,
    message: str,
) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    runtime = build_filtering_acceptance_runtime(paths)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with _running_daemon(daemon):
        before = _filtering_snapshot(runtime, paths)
        assert await send_request(paths.socket_file, search_request) == ErrorResponse(
            ErrorCode.BAD_REQUEST, message
        )
        assert _filtering_snapshot(runtime, paths) == before


async def test_raw_invalid_selector_schema_never_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    runtime = build_filtering_acceptance_runtime(paths)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    dispatch = AsyncMock(wraps=daemon._dispatch)
    monkeypatch.setattr(daemon, "_dispatch", dispatch)
    payloads = [
        {"type": "keyword_search", "query": "query", "provider": None},
        {"type": "paper_search", "query": "query", "provider": 1},
        {"type": "llm_search", "prompt": "prompt", "model": False},
        {"type": "keyword_search", "query": "query", "model": "gpt-5"},
        {"type": "llm_search", "provider": "openai_main"},
        {"type": "url_fetch", "url": "https://example.com", "focus": None, "provider": "beta"},
        {"type": "shutdown", "model": "gpt-5"},
    ]
    async with _running_daemon(daemon):
        before = _filtering_snapshot(runtime, paths)
        reader, writer = await asyncio.open_unix_connection(path=paths.socket_file)
        try:
            writer.write(b"".join(json.dumps(payload).encode() + b"\n" for payload in payloads))
            await writer.drain()
            for _ in payloads:
                frame = await asyncio.wait_for(reader.readline(), timeout=_DAEMON_TIMEOUT_SECONDS)
                assert parse_response_frame(frame) == ErrorResponse(
                    ErrorCode.BAD_REQUEST, "Request fields do not match schema"
                )
        finally:
            writer.close()
            await writer.wait_closed()
        dispatch.assert_not_awaited()
        assert _filtering_snapshot(runtime, paths) == before


async def test_failing_direct_paper_search_writes_no_result_file(tmp_path: Path) -> None:
    paths = RuntimePaths(
        config_file=tmp_path / "config.toml",
        socket_file=tmp_path / "gateway.sock",
        results_dir=tmp_path / "results",
    )
    runtime = build_acceptance_runtime(paths)
    failing_provider = next(
        provider for provider in runtime.academic_providers if provider.name == "crossref"
    )
    runtime.paper_search_orchestrator.providers = (failing_provider,)
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with _running_daemon(daemon) as daemon_task:
        response = await send_request(paths.socket_file, PaperSearchRequest("failing topic"))
        assert response == ErrorResponse(
            ErrorCode.ALL_PROVIDERS_FAILED,
            "All academic search provider pipelines failed",
        )
        assert not list(paths.results_dir.glob("paper-*.jsonl"))

        assert await send_request(paths.socket_file, ShutdownRequest()) == SuccessResponse(
            "Daemon stopped."
        )
        await daemon_task
