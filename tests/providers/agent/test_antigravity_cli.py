"""Exercise terminal envelopes independently of the external CLI."""

import importlib.util
import json
from pathlib import Path

import pytest

from agent_search_gateway.concurrency import CapacityGate
from agent_search_gateway.errors import ErrorCode, ExecutionFailure
from tests.support.agent_cli import write_fake_cli


def _decode(output: bytes, returncode: int = 0) -> str:
    assert importlib.util.find_spec("agent_search_gateway.providers.agent"), (
        "Antigravity adapter must be available"
    )
    from agent_search_gateway.providers.agent.antigravity_cli import _decode_terminal_response

    return _decode_terminal_response(output, returncode)


def test_terminal_envelope_returns_only_success_response() -> None:
    markdown = "  ## Result\nURL: https://example.test\nAbstract: Result\n"
    output = json.dumps(
        {
            "status": "SUCCESS",
            "response": markdown,
            "conversation_id": "never-save",
            "usage": {"tokens": 10},
        }
    ).encode()
    assert _decode(output) == markdown
    assert _decode(b'{"status":"SUCCESS","response":"invalid markdown"}') == "invalid markdown"


@pytest.mark.parametrize(
    ("output", "returncode", "reason"),
    [
        (b'{"status":"SUCCESS","response":"ok"}', 1, "nonzero_exit"),
        *(
            (
                json.dumps({"status": status, "response": "raw-secret"}).encode(),
                0,
                "terminal_status_failure",
            )
            for status in [
                "ERROR",
                "CANCELED",
                "INTERRUPTED",
                "INVALID",
                "WAITING",
                "RUNNING",
                "other",
            ]
        ),
        (b"not json", 0, "invalid_json"),
        (b"\xff", 0, "invalid_json"),
        (b"[]", 0, "invalid_json"),
        (b'{"response":"ok"}', 0, "invalid_json"),
        (b'{"status":null,"response":"ok"}', 0, "invalid_json"),
        (b'{"status":"SUCCESS"}', 0, "invalid_json"),
        (b'{"status":"SUCCESS","response":null}', 0, "invalid_json"),
        (b'{"status":"SUCCESS","response":1}', 0, "invalid_json"),
        (b'{"status":"SUCCESS","response":"ok"} {}', 0, "invalid_json"),
        (b'{"status":"SUCCESS","response":"ok","extra":NaN}', 0, "invalid_json"),
        (b'{"status":"SUCCESS","response":"ok","extra":Infinity}', 0, "invalid_json"),
        (b'{"status":"SUCCESS","response":"  "}', 0, "empty_response"),
    ],
)
def test_terminal_envelope_rejects_unsafe_outcomes(
    output: bytes,
    returncode: int,
    reason: str,
) -> None:
    with pytest.raises(ExecutionFailure) as caught:
        _decode(output, returncode)
    assert caught.value.reason == reason
    assert caught.value.code is (
        ErrorCode.PROTOCOL_ERROR if reason == "invalid_json" else ErrorCode.ALL_PROVIDERS_FAILED
    )
    assert "raw-secret" not in str(caught.value)


async def test_runner_spawns_one_fresh_process_and_returns_markdown(tmp_path: Path) -> None:
    from agent_search_gateway.providers.agent import antigravity_cli as adapter

    assert hasattr(adapter, "run_antigravity"), "Adapter must implement the asynchronous runner"
    control = tmp_path / "calls.jsonl"
    marker = tmp_path / "injection-must-not-run"
    command = write_fake_cli(
        tmp_path,
        f"""
        with open({str(control)!r}, "a") as handle:
            handle.write(json.dumps({{
                "pid": os.getpid(), "cwd": os.getcwd(), "argv": sys.argv[1:],
                "mode": os.stat(".").st_mode & 0o777, "stdin": sys.stdin.read(),
            }}) + "\\n")
        model = "saved-default"
        if "--model" in sys.argv:
            model = sys.argv[sys.argv.index("--model") + 1]
        print(json.dumps({{"status": "SUCCESS", "response": model}}))
    """,
    )
    prompt = f'quotes " ; touch {marker}; $(touch {marker})\nsecond line'
    messages = (
        {"role": "system", "content": "Existing system prompt"},
        {"role": "user", "content": prompt},
    )
    quota = CapacityGate(1)
    for model in [None, "explicit-model"]:
        result = await adapter.run_antigravity(
            model,
            messages,
            provider_name="agy",
            command=str(command),
            timeout_seconds=0.5,
            pass_env=(),
            quota=quota,
        )
        assert result == (model or "saved-default")
    calls = [json.loads(line) for line in control.read_text().splitlines()]
    assert len({call["pid"] for call in calls}) == 2
    assert len({call["cwd"] for call in calls}) == 2
    for call in calls:
        args = call["argv"]
        assert args.count("-p") == 1
        text = args[args.index("-p") + 1]
        assert "[SYSTEM]\nExisting system prompt" in text
        assert "[USER]\n" + prompt in text
        assert args[args.index("--output-format") + 1] == "json"
        assert args[args.index("--print-timeout") + 1] == "0.5s"
        assert not {"--continue", "--conversation", "--dangerously-skip-permissions"} & set(args)
        assert call["mode"] == 0o700
        assert call["stdin"] == ""
        assert not Path(call["cwd"]).exists()
    assert "--model" not in calls[0]["argv"]
    assert calls[1]["argv"].count("--model") == 1
    assert calls[1]["argv"][-2:] == ["--model", "explicit-model"]
    assert not marker.exists()
    assert quota.in_use == 0


@pytest.mark.parametrize(
    ("timeout_seconds", "expected_argument"),
    [
        (1_000_000.0, "1000000s"),
        (123456.7, "123456.7s"),
        (1e-7, "0.0000001s"),
        (300.0, "300s"),
        (0.5, "0.5s"),
    ],
)
async def test_runner_preserves_timeout_value_in_plain_seconds_argument(
    tmp_path: Path,
    timeout_seconds: float,
    expected_argument: str,
) -> None:
    from agent_search_gateway.providers.agent.antigravity_cli import run_antigravity

    command = write_fake_cli(
        tmp_path,
        """
        timeout = sys.argv[sys.argv.index("--print-timeout") + 1]
        print(json.dumps({"status": "SUCCESS", "response": timeout}))
    """,
    )
    assert (
        await run_antigravity(
            None,
            [],
            provider_name="agy",
            command=str(command),
            timeout_seconds=timeout_seconds,
            pass_env=(),
            quota=CapacityGate(1),
        )
        == expected_argument
    )


async def test_runner_omits_model_flag_when_model_is_none(tmp_path: Path) -> None:
    from agent_search_gateway.providers.agent import antigravity_cli as adapter

    command = write_fake_cli(
        tmp_path,
        """
        assert "--model" not in sys.argv
        print(json.dumps({"status": "SUCCESS", "response": "fixture-saved-default"}))
    """,
    )
    assert (
        await adapter.run_antigravity(
            None,
            [],
            provider_name="agy",
            command=str(command),
            timeout_seconds=1,
            pass_env=(),
            quota=CapacityGate(1),
        )
        == "fixture-saved-default"
    )


async def test_runner_passes_only_explicit_extra_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from agent_search_gateway.providers.agent.antigravity_cli import run_antigravity

    names = {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "LANG": "C",
        "TEST_HARNESS_SETTING": "explicit-value",
        "OPENAI_API_KEY": "[REDACTED_SECRET]",
        "TAVILY_API_KEY": "[REDACTED_SECRET]",
        "OTHER_SETTING": "unrequested-value",
        "LC_UNREQUESTED": "prefix-must-not-leak",
    }
    for name, value in names.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("MISSING_SETTING", raising=False)
    command = write_fake_cli(
        tmp_path,
        f"""
        response = {{name: os.environ.get(name) for name in {list(names)!r} + ["MISSING_SETTING"]}}
        print(json.dumps({{"status": "SUCCESS", "response": json.dumps(response)}}))
    """,
    )
    before = dict(os.environ)
    for extras in [
        ("TEST_HARNESS_SETTING", "MISSING_SETTING"),
        ("TEST_HARNESS_SETTING", "OPENAI_API_KEY"),
    ]:
        response = await run_antigravity(
            None,
            [],
            provider_name="agy",
            command=str(command),
            timeout_seconds=1,
            pass_env=extras,
            quota=CapacityGate(1),
        )
        seen = json.loads(response)
        assert seen["HOME"] == names["HOME"]
        assert seen["XDG_CONFIG_HOME"] == names["XDG_CONFIG_HOME"]
        assert seen["XDG_CACHE_HOME"] == names["XDG_CACHE_HOME"]
        assert seen["LANG"] == "C"
        assert seen["TEST_HARNESS_SETTING"] == "explicit-value"
        assert seen["OPENAI_API_KEY"] == (
            names["OPENAI_API_KEY"] if "OPENAI_API_KEY" in extras else None
        )
        assert all(
            seen[name] is None
            for name in [
                "TAVILY_API_KEY",
                "OTHER_SETTING",
                "LC_UNREQUESTED",
                "MISSING_SETTING",
            ]
        )
        assert dict(os.environ) == before
