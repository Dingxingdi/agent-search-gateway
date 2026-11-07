"""Offline CLI -> Unix socket -> Runtime -> executable Agent acceptance."""

import asyncio
import json
from io import StringIO
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.cli import build_parser, run_command
from agent_search_gateway.config import load_toml, resolve_config
from agent_search_gateway.daemon import ForegroundDaemon
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.runtime import Runtime
from tests.acceptance.test_gateway_workflows import _running_daemon
from tests.support.agent_cli import write_fake_cli
from tests.support.agent_search import paper_markdown, web_markdown


def build_agent_acceptance(
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
                    "branch": "paper" if paper else "web"}}) + "\\n")
            if query == "failure":
                sys.exit(3)
            if query == "partial" and paper:
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
api_url = "https://fetch.example.test"

[llm_providers.fetch]
protocol = "openai"
api_endpoint = "chat_completions"
api_url = "https://llm.example.test"
api_key_env = "ENV_D"

[global_default_llm]
provider = "fetch"
model = "fetch-model"

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
        assert payload["model"] == "fetch-model"
        content = (
            json.dumps({"ok": True})
            if payload["messages"][0]["content"].startswith("Decide ")
            else "Focused fetched article"
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    runtime = Runtime.build(
        resolve_config(
            load_toml(paths.config_file), build_default_registry(), {"ENV_D": "[REDACTED_SECRET]"}
        ),
        paths,
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return paths, runtime, evidence, calls


async def invoke(paths: RuntimePaths, argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    code = await run_command(build_parser().parse_args(argv), paths, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize("scope", ["web", "paper", "all"])
async def test_agent_cli_socket_to_fake_cli_and_followup_fetch(tmp_path: Path, scope: str) -> None:
    paths, runtime, evidence, http_calls = build_agent_acceptance(
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
        assert runtime.quotas.get_agent("a").in_use == 0
    assert not paths.socket_file.exists()


async def test_agent_default_only_model_selector_starts_no_process(tmp_path: Path) -> None:
    paths, runtime, evidence, calls = build_agent_acceptance(
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
    paths, runtime, evidence, calls = build_agent_acceptance(
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
    paths, runtime, _, _ = build_agent_acceptance(tmp_path, [("a", None)])
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
