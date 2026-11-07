import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from agent_search_gateway.concurrency import ProviderQuotaManager
from agent_search_gateway.config import resolve_config
from agent_search_gateway.errors import ConfigFailure, ErrorCode, ExecutionFailure, InputFailure
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.providers.defaults import build_default_registry
from agent_search_gateway.runtime import Runtime
from tests.runtime.test_runtime_assembly import _config, _CountingAsyncClient, _environment
from tests.support.agent_cli import write_fake_cli
from tests.support.agent_search import web_markdown


def agent_config(command: str, *, model: str | None = None) -> dict[str, object]:
    raw = _config()
    raw["web_providers"] = {}
    raw["search_llm"] = {"providers": []}
    raw["agent_providers"] = {
        "primary": {"harness": "antigravity", "command": command, "max_concurrency": 1},
    }
    entry: dict[str, object] = {"provider": "primary"}
    if model is not None:
        entry["model"] = model
    raw["search_agent"] = {"providers": [entry]}
    return raw


@pytest.mark.parametrize("model", [None, "pinned-model"])
async def test_runtime_agent_search_uses_configured_cli_without_llm_transport(
    tmp_path: Path,
    model: str | None,
) -> None:
    evidence = tmp_path / "argv.json"
    command = write_fake_cli(
        tmp_path,
        f"""
        Path({str(evidence)!r}).write_text(json.dumps(sys.argv[1:]))
        print(json.dumps({{"status": "SUCCESS", "response": {web_markdown()!r}}}))
    """,
    )
    http_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        http_calls.append(request)
        return httpx.Response(500)

    resolved = resolve_config(
        agent_config(str(command), model=model), build_default_registry(), _environment()
    )
    runtime = Runtime.build(
        resolved,
        RuntimePaths.from_home(tmp_path),
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        assert not evidence.exists()
        path = Path(await runtime.search_orchestrator.agent_search("q", request_id="11111111"))
        assert (
            path.read_bytes() == b'{"url":"https://example.test/web","abstract":"Agent result"}\n'
        )
        argv = json.loads(evidence.read_text())
        assert argv[0] == "-p" and argv[2:6] == [
            "--output-format",
            "json",
            "--print-timeout",
            "300s",
        ]
        assert argv[6:] == ([] if model is None else ["--model", model])
        assert not http_calls
        assert runtime.quotas.get_agent("primary").max_observed_in_use == 1
        assert runtime.quotas.get_llm("primary").max_observed_in_use == 0
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("command", ["bin/agy", "./bin/agy", "agy"])
async def test_runtime_anchors_agent_paths_before_invocation_cwd_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    write_fake_cli(
        tmp_path / "bin",
        'print(json.dumps({"status": "SUCCESS", "response": os.getcwd()}))',
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    resolved = resolve_config(agent_config(command), build_default_registry(), _environment())
    quotas = ProviderQuotaManager(web_limits={}, llm_limits={}, agent_limits={"primary": 1})
    runners = Runtime._build_agent_runners(resolved.agent, quotas)
    later_cwd = tmp_path / "later"
    later_cwd.mkdir()
    monkeypatch.chdir(later_cwd)

    invocation_cwd = Path(await runners["primary"](None, []))

    assert invocation_cwd.is_absolute()
    assert invocation_cwd not in (tmp_path, later_cwd)
    assert invocation_cwd.name.startswith("agent-search-")
    assert not invocation_cwd.exists()
    assert quotas.get_agent("primary").in_use == 0


async def test_runtime_binds_each_alias_and_does_not_retry_or_invoke_unselected_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_OPTION_A", "a-option")
    monkeypatch.setenv("AGENT_OPTION_B", "b-option")
    evidence = tmp_path / "calls.jsonl"
    providers: dict[str, object] = {}
    for alias, timeout in [("primary", 7), ("second", 9)]:
        command = write_fake_cli(
            tmp_path,
            f"""
            with Path({str(evidence)!r}).open("a") as output:
                output.write(json.dumps({{"alias": {alias!r}, "argv": sys.argv[1:],
                    "a": os.environ.get("AGENT_OPTION_A"), "b": os.environ.get("AGENT_OPTION_B"),
                    "pid": os.getpid()}}) + "\\n")
            time.sleep(0.01)
            sys.exit(7)
        """,
            name=alias,
        )
        providers[alias] = {
            "harness": "antigravity",
            "command": str(command),
            "timeout_seconds": timeout,
            "pass_env": ["AGENT_OPTION_A" if alias == "primary" else "AGENT_OPTION_B"],
        }
    raw = agent_config("unused")
    raw["agent_providers"] = providers
    raw["search_agent"] = {
        "providers": [
            {"provider": "primary"},
            {"provider": "primary"},
            {"provider": "second", "model": "pin"},
        ]
    }
    raw["retry"] = {"max_attempts": 3}
    runtime = Runtime.build(
        resolve_config(raw, build_default_registry(), _environment()),
        RuntimePaths.from_home(tmp_path),
        http_client_factory=_CountingAsyncClient,
    )
    try:
        with pytest.raises(InputFailure):
            await runtime.search_orchestrator.agent_search(
                "q", request_id="11111111", model="absent"
            )
        assert not evidence.exists()
        with pytest.raises(ExecutionFailure):
            await runtime.search_orchestrator.agent_search(
                "q", request_id="11111111", provider="primary"
            )
        first = [json.loads(line) for line in evidence.read_text().splitlines()]
        assert len(first) == 2 and {row["alias"] for row in first} == {"primary"}
        assert len({row["pid"] for row in first}) == 2
        assert all(row["a"] == "a-option" and row["b"] is None for row in first)
        assert all(row["argv"][5] == "7s" and "--model" not in row["argv"] for row in first)
        assert runtime.quotas.get_agent("primary").max_observed_in_use == 1
        with pytest.raises(ExecutionFailure):
            await runtime.search_orchestrator.agent_search(
                "q", request_id="22222222", provider="second"
            )
        last = json.loads(evidence.read_text().splitlines()[-1])
        assert last["argv"][5:] == ["9s", "--model", "pin"]
        assert last["a"] is None and last["b"] == "b-option"
        assert len(evidence.read_text().splitlines()) == 3
        assert (
            runtime.quotas.get_agent("primary").in_use
            == runtime.quotas.get_agent("second").in_use
            == 0
        )
    finally:
        await runtime.aclose()


async def test_runtime_build_is_lazy_and_closes_only_http_resources_once(tmp_path: Path) -> None:
    clients: list[_CountingAsyncClient] = []

    def factory() -> httpx.AsyncClient:
        client = _CountingAsyncClient()
        clients.append(client)
        return client

    resolved = resolve_config(
        agent_config("/not-installed/agy"), build_default_registry(), _environment()
    )
    runtime = Runtime.build(resolved, RuntimePaths.from_home(tmp_path), http_client_factory=factory)
    await runtime.aclose()
    await runtime.aclose()
    assert clients and all(client.close_calls == 1 for client in clients)


async def test_runtime_shared_paper_priority_retains_academic_llm_then_agent(
    tmp_path: Path,
) -> None:
    from agent_search_gateway.paper_search_parser import parse_paper_markdown
    from tests.support.agent_search import paper_markdown

    raw = agent_config("/not-installed/agy")
    raw["academic_providers"] = {"crossref": {"enabled": True}}
    raw["search_llm"] = {"providers": [{"provider": "secondary", "model": "search"}]}
    runtime = Runtime.build(
        resolve_config(raw, build_default_registry(), _environment()),
        RuntimePaths.from_home(tmp_path),
        http_client_factory=_CountingAsyncClient,
    )
    try:
        [generated] = parse_paper_markdown(
            paper_markdown(), provider="primary", source_kind="agent"
        )
        llm = replace(generated, source="llm:secondary", title="LLM")
        academic = replace(
            generated, source="crossref", source_id="10.1000/shared", title="Academic"
        )
        aggregator = runtime.paper_search_orchestrator.aggregator
        assert runtime.search_orchestrator._paper_aggregator is aggregator
        [record] = aggregator.aggregate([generated, llm, academic])
        assert record.title == "Academic"
        assert record.sources == ("crossref", "llm:secondary", "agent:primary")
    finally:
        await runtime.aclose()


async def test_runtime_rejects_unknown_resolved_harness(tmp_path: Path) -> None:
    resolved = resolve_config(agent_config("agy"), build_default_registry(), _environment())
    invalid = replace(
        resolved.agent, providers=(replace(resolved.agent.providers[0], harness="unknown"),)
    )
    with pytest.raises(ConfigFailure) as caught:
        Runtime._build_agent_runners(
            invalid,
            ProviderQuotaManager(
                web_limits={},
                llm_limits={},
                agent_limits={"primary": 1},
            ),
        )
    assert caught.value.code is ErrorCode.CONFIG_ERROR
