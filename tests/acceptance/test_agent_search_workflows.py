"""Offline CLI -> Unix socket -> Runtime -> executable Agent acceptance."""

import asyncio
import json
from collections.abc import Sequence
from io import StringIO
from pathlib import Path
from time import monotonic

import httpx
import pytest

from agent_search_gateway.cli import build_parser, run_command
from agent_search_gateway.config import load_toml, resolve_config
from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.models import AgentInvocation
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.contracts import ChatMessage
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.runtime import Runtime
from tests.acceptance.test_gateway_workflows import _running_daemon
from tests.support.acceptance import build_acceptance_runtime
from tests.support.agent_cli import write_fake_cli
from tests.support.agent_search import agent_orchestrator, paper_markdown, web_markdown


async def build_agent_acceptance(
    directory: Path,
    entries: list[tuple[str, str | None]],
    *,
    allow_pin: bool = True,
) -> tuple[RuntimePaths, Runtime, Path, list[httpx.Request]]:
    paths = RuntimePaths(
        directory / "config.toml", directory / "gateway.sock", directory / "results"
    )
    evidence = directory / "calls.jsonl"
    provider_blocks: list[str] = []
    for alias in dict.fromkeys(alias for alias, _ in entries):
        command = write_fake_cli(
            directory,
            f"""
            argv = sys.argv[1:]
            if not {allow_pin!r} and "--model" in argv:
                sys.exit(12)
            model = argv[argv.index("--model") + 1] if "--model" in argv else "harness-default"
            paper = "## Paper" in argv[1]
            query = argv[1].split("[USER]\\n", 1)[1]
            with Path({str(evidence)!r}).open("a") as target:
                target.write(json.dumps({{"alias": {alias!r}, "model": model, "pid": os.getpid(),
                    "prompt_bytes": len(argv[1].encode()), "has_tail": query.endswith("END"),
                    "clipped": "[... truncated ...]" in query,
                    "dropped_middle": "DroppedMiddle" not in query,
                    "branch": "paper" if paper else "web"}}) + "\\n")
            if query == "failure":
                sys.exit(3)
            if query.startswith("EMPTY"):
                response = {web_markdown(abstract="")!r}
            elif query == "partial" and paper:
                response = "invalid paper"
            elif paper:
                response = {paper_markdown(pdf="https://example.test/metadata.pdf")!r}
                response = response.replace("Paper abstract", model)
            else:
                response = {web_markdown()!r}.replace("Agent result", model)
            print(json.dumps({{"status": "SUCCESS", "response": response}}))
        """,
            name=f"agy-{alias}",
        )
        provider_blocks.append(
            f'[agent_providers.{alias}]\nharness = "antigravity"\n'
            f"command = {json.dumps(str(command))}\n"
            "max_concurrency = 2\ntimeout_seconds = 5\n"
        )
    invocation_blocks = [
        "[[search_agent.providers]]\nprovider = "
        + json.dumps(alias)
        + "\n"
        + ("" if model is None else "model = " + json.dumps(model) + "\n")
        for alias, model in entries
    ]
    config = """
[web_providers.jina]
enable_search = false
enable_fetch = true
[[web_providers.jina.credentials]]
api_url = "https://fetch.example.test"

[llm_providers.fetch]
protocol = "openai"
api_endpoint = "chat_completions"
[[llm_providers.fetch.credentials]]
api_url = "https://llm.example.test"
api_key_env = "ENV_D"

[global_default_llm]
provider = "fetch"
model = "fetch-model"

[[search_llm.providers]]
provider = "fetch"
model = "lookup"
[[search_llm.providers]]
provider = "fetch"
model = "unused"

[fetch_llm.judge]
[fetch_llm.safety]
[fetch_llm.content_clean]
[fetch_llm.focus_summary]

[retry]
max_attempts = 1
""" + "\n".join(provider_blocks + invocation_blocks)
    paths.config_file.write_text(config)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "fetch.example.test":
            return httpx.Response(200, text="Full fetched article")
        payload = json.loads(request.content)
        if payload["model"] == "lookup":
            query = payload["messages"][1]["content"]
            if query.startswith("EMPTY"):
                output = web_markdown(abstract="")
            else:
                output = (
                    paper_markdown()
                    if "## Paper" in payload["messages"][0]["content"]
                    else web_markdown()
                )
            return httpx.Response(200, json={"choices": [{"message": {"content": output}}]})
        assert payload["model"] == "fetch-model"
        content = (
            json.dumps({"ok": True})
            if payload["messages"][0]["content"].startswith("Decide ")
            else "Focused fetched article"
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    runtime = await Runtime.build(
        resolve_config(
            load_toml(paths.config_file), build_default_registry(), {"ENV_D": "example"}
        ),
        paths,
        http_client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    )
    return paths, runtime, evidence, calls


async def invoke(paths: RuntimePaths, argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    code = await run_command(build_parser().parse_args(argv), paths, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


async def test_agent_cli_receives_response_after_old_socket_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise real socket timers without spending twenty minutes waiting."""
    paths = RuntimePaths(tmp_path / "config.toml", tmp_path / "gateway.sock", tmp_path / "results")
    runtime = build_acceptance_runtime(paths)
    loop = asyncio.get_running_loop()
    real_time = loop.time
    clock_offset = 0.0
    runner_done = asyncio.Event()

    async def runner(model: str | None, messages: Sequence[ChatMessage]) -> str:
        nonlocal clock_offset
        assert model is None and messages
        clock_offset = 1201.0
        # Let the old socket timeout callback run before producing a response.
        await asyncio.sleep(0.001)
        runner_done.set()
        return web_markdown()

    runtime.search_orchestrator = agent_orchestrator(
        paths.results_dir, [AgentInvocation("a")], {"a": runner}
    )

    async def exercise() -> tuple[int, str, str]:
        outcome = await invoke(paths, ["agent-search", "q", "--scope", "web"])
        await runner_done.wait()
        return outcome

    async def watchdog() -> None:
        deadline = monotonic() + 2
        while monotonic() < deadline:
            await asyncio.sleep(0.01)
        pytest.fail("Agent CLI did not finish within the real-time watchdog")

    async with _running_daemon(ForegroundDaemon(paths, runtime_factory=lambda: runtime)):
        with monkeypatch.context() as clock_patch:
            clock_patch.setattr(loop, "time", lambda: real_time() + clock_offset)
            cli_task = asyncio.create_task(exercise())
            guard = asyncio.create_task(watchdog())
            try:
                done, _ = await asyncio.wait((cli_task, guard), return_when=asyncio.FIRST_COMPLETED)
                if guard in done:
                    await guard
                code, out, err = await cli_task
            finally:
                cli_task.cancel()
                guard.cancel()
                await asyncio.gather(cli_task, guard, return_exceptions=True)

    assert clock_offset == 1201.0
    assert code == 0, err
    assert err == ""
    path = Path(out.strip())
    assert path.is_absolute() and out == f"{path}\n"
    assert path.parent == paths.results_dir and path.name.startswith("agent-")
    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"url": "https://example.test/web", "abstract": "Agent result"}
    ]


@pytest.mark.parametrize("scope", ["web", "paper", "all"])
async def test_agent_cli_socket_to_fake_cli_and_followup_fetch(tmp_path: Path, scope: str) -> None:
    paths, runtime, evidence, http_calls = await build_agent_acceptance(
        tmp_path, [("a", None)], allow_pin=False
    )
    daemon = ForegroundDaemon(paths, runtime_factory=lambda: runtime)
    async with _running_daemon(daemon):
        code, out, err = await invoke(paths, ["agent-search", "q", "--scope", scope])
        assert code == 0 and err == ""
        path = Path(out.strip())
        assert path.is_absolute() and out == str(path) + "\n"
        assert path.parent == paths.results_dir and path.name.startswith("agent-")
        assert len(path.stem.removeprefix("agent-")) == 8
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert all(row["abstract"] == "harness-default" for row in rows)
        if scope == "web":
            assert rows == [{"url": "https://example.test/web", "abstract": "harness-default"}]
        elif scope == "paper":
            assert len(rows) == 1 and "type" not in rows[0] and rows[0]["sources"] == ["agent:a"]
        else:
            assert [row["type"] for row in rows] == ["web", "paper"]
            assert rows[1]["sources"] == ["agent:a"]
        assert not http_calls
        observations = [json.loads(line) for line in evidence.read_text().splitlines()]
        assert len(observations) == (2 if scope == "all" else 1)
        assert all(row["model"] == "harness-default" for row in observations)
        for row in rows:
            assert await invoke(paths, ["url-fetch", row["url"]]) == (
                0,
                "Full fetched article\n",
                "",
            )
        assert await invoke(paths, ["url-fetch", rows[0]["url"], "focus"]) == (
            0,
            "Focused fetched article\n",
            "",
        )
        assert any(request.url.host == "llm.example.test" for request in http_calls)
        assert any(request.url.host == "fetch.example.test" for request in http_calls)
        if scope != "web":
            before = len(http_calls)
            assert await invoke(paths, ["url-fetch", "https://example.test/metadata.pdf"]) == (
                1,
                "",
                "URL was not admitted by search\n",
            )
            assert len(http_calls) == before
        gate = runtime.quotas.get_agent("a")
        assert gate is not None and gate.in_use == 0
    assert not paths.socket_file.exists()


@pytest.mark.parametrize("kind", ["llm", "agent"])
@pytest.mark.parametrize(
    "scope, empty", [("web", False), ("paper", False), ("all", False), ("web", True)]
)
async def test_large_search_preserves_selected_schema_and_empty_success(
    tmp_path: Path, kind: str, scope: str, empty: bool
) -> None:
    paths, runtime, evidence, calls = await build_agent_acceptance(
        tmp_path, [("a", "selected"), ("b", "unused")]
    )
    cap, provider, model = (
        (131072, "fetch", "lookup") if kind == "llm" else (98304, "a", "selected")
    )
    query = (
        ("EMPTY" if empty else "LOOKUP") + "x" * cap + "DroppedMiddle" + "y" * (cap // 3) + "END"
    )
    async with _running_daemon(ForegroundDaemon(paths, runtime_factory=lambda: runtime)):
        code, out, err = await invoke(
            paths,
            [f"{kind}-search", query, "--scope", scope, "--provider", provider, "--model", model],
        )
        assert code == 0 and err == ""
        rows = [json.loads(line) for line in Path(out.strip()).read_text().splitlines()]
    if empty:
        assert rows == []
    elif scope == "all":
        assert [row["type"] for row in rows] == ["web", "paper"]
        assert rows[1]["sources"] == [f"{kind}:{provider}"]
    elif scope == "paper":
        assert (
            len(rows) == 1
            and "type" not in rows[0]
            and rows[0]["sources"] == [f"{kind}:{provider}"]
        )
    else:
        assert len(rows) == 1 and set(rows[0]) == {"url", "abstract"}
    count = 2 if scope == "all" else 1
    if kind == "agent":
        observed = [json.loads(line) for line in evidence.read_text().splitlines()]
        assert len(observed) == count and not calls
        assert all(
            row["alias"] == provider
            and row["model"] == model
            and row["clipped"]
            and row["has_tail"]
            and row["dropped_middle"]
            and row["prompt_bytes"] <= cap
            for row in observed
        )
    else:
        assert len(calls) == count and not evidence.exists()
        for request in calls:
            assert len(request.content) <= cap and b"DroppedMiddle" not in request.content
            payload = json.loads(request.content)
            assert payload["model"] == model
            retained = payload["messages"][1]["content"]
            assert "[... truncated ...]" in retained and retained.endswith("END")


async def test_cancel_large_agent_search_workflow_reaps_process_and_releases_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_search_gateway.providers.agent import antigravity_cli
    from tests.doctor._support import environment_name, write_valid_config
    from tests.providers.agent.test_antigravity_lifecycle import assert_clean, hanging_cli
    from tests.support.agent_cli import force_cleanup, wait_for_json

    command, ready = hanging_cli(tmp_path, ignore_term=True)
    paths = RuntimePaths.from_home(tmp_path)
    write_valid_config(paths.config_file)
    raw = load_toml(paths.config_file)
    raw["agent_providers"] = {
        "primary": {"harness": "antigravity", "command": str(command), "max_concurrency": 1}
    }
    raw["search_agent"] = {"providers": [{"provider": "primary"}]}

    def unexpected_http(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Agent search must use its CLI")

    runtime = await Runtime.build(
        resolve_config(raw, build_default_registry(), {environment_name(): "offline"}),
        paths,
        http_client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(unexpected_http), **kwargs
        ),
    )
    monkeypatch.setattr(antigravity_cli, "_TERMINATE_GRACE_SECONDS", 0.05)
    state: dict[str, object] = {}
    task = asyncio.create_task(
        runtime.search_orchestrator.agent_search("x" * 100000, request_id="abcd1234")
    )
    try:
        state = await wait_for_json(ready)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        gate = runtime.quotas.get_agent("primary")
        assert gate is not None
        assert_clean(state, gate)
        assert not list(paths.results_dir.glob("*.jsonl"))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if state:
            force_cleanup(state)
        await runtime.aclose()


async def test_agent_default_only_model_selector_starts_no_process(tmp_path: Path) -> None:
    paths, runtime, evidence, calls = await build_agent_acceptance(
        tmp_path, [("a", None)], allow_pin=False
    )
    async with _running_daemon(ForegroundDaemon(paths, runtime_factory=lambda: runtime)):
        assert await invoke(paths, ["agent-search", "q", "--model", "harness-default"]) == (
            1,
            "",
            "No Agent search invocation matches model 'harness-default'\n",
        )
        assert not evidence.exists() and not calls and not list(paths.results_dir.iterdir())


async def test_agent_pinned_selection_duplicate_entries_and_followup_are_independent(
    tmp_path: Path,
) -> None:
    paths, runtime, evidence, calls = await build_agent_acceptance(
        tmp_path,
        [("a", None), ("a", None), ("a", "m1"), ("b", "m2")],
    )
    async with _running_daemon(ForegroundDaemon(paths, runtime_factory=lambda: runtime)):
        first = await invoke(paths, ["agent-search", "q", "--provider", "b", "--model", "m2"])
        assert first[0] == 0
        observations = [json.loads(line) for line in evidence.read_text().splitlines()]
        assert [(r["alias"], r["model"]) for r in observations] == [("b", "m2")]
        assert json.loads(Path(first[1].strip()).read_text())["abstract"] == "m2"
        outcomes = await asyncio.gather(
            invoke(paths, ["agent-search", "q", "--provider", "a"]),
            invoke(paths, ["agent-search", "q", "--model", "m2"]),
        )
        assert all(outcome[0] == 0 for outcome in outcomes)
        assert (await invoke(paths, ["agent-search", "q"]))[0] == 0
        observations = [json.loads(line) for line in evidence.read_text().splitlines()]
        assert len(observations) == len({r["pid"] for r in observations}) == 9
        assert sum(r["model"] == "harness-default" for r in observations) == 4
        assert sum(r["model"] == "m1" for r in observations) == 2
        assert sum(r["model"] == "m2" for r in observations) == 3
        assert not calls


async def test_agent_partial_success_and_all_failure_write_only_valid_results(
    tmp_path: Path,
) -> None:
    paths, runtime, _, _ = await build_agent_acceptance(tmp_path, [("a", None)])
    async with _running_daemon(ForegroundDaemon(paths, runtime_factory=lambda: runtime)):
        code, out, err = await invoke(paths, ["agent-search", "partial", "--scope", "all"])
        assert code == 0 and err == ""
        assert [
            json.loads(line)["type"] for line in Path(out.strip()).read_text().splitlines()
        ] == ["web"]
        files = set(paths.results_dir.iterdir())
        assert await invoke(paths, ["agent-search", "failure", "--scope", "all"]) == (
            1,
            "",
            "All Agent search branches failed\n",
        )
        assert set(paths.results_dir.iterdir()) == files
