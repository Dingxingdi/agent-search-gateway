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


async def test_agent_caps_complete_prompt_argument_before_spawn(tmp_path: Path) -> None:
    from agent_search_gateway.input_limits import TruncatableMessage
    from agent_search_gateway.providers.agent.antigravity_cli import run_antigravity

    command = write_fake_cli(
        tmp_path,
        r"""
        prompt = sys.argv[sys.argv.index("-p") + 1]
        result = {
            "bytes": len(prompt.encode("utf-8")),
            "prefix": prompt.startswith("[SYSTEM]\nInstruction\n\n[USER]\nFocus\nHEAD"),
            "suffix": prompt.endswith("TAIL\nEnd"),
            "marker": "[... truncated ...]" in prompt,
        }
        print(json.dumps({"status": "SUCCESS", "response": json.dumps(result)}))
        """,
    )
    result = json.loads(
        await run_antigravity(
            None,
            (
                {"role": "system", "content": "Instruction"},
                TruncatableMessage("Focus\n", "HEAD" + "a界" * (30 * 1024) + "TAIL", "\nEnd"),
            ),
            provider_name="agy",
            command=str(command),
            timeout_seconds=1,
            pass_env=(),
            quota=CapacityGate(1),
        )
    )
    assert result["bytes"] <= 98304
    assert result["prefix"] and result["suffix"] and result["marker"]


@pytest.mark.parametrize("mode", ["debug", "info", "late_debug"])
async def test_agent_previews_the_sent_prompt_and_full_stdout_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mode: str,
) -> None:
    import asyncio
    import logging
    from io import StringIO

    from agent_search_gateway import observability
    from agent_search_gateway.body_preview import BodyScrubber
    from agent_search_gateway.input_limits import TruncatableMessage
    from agent_search_gateway.providers.agent import antigravity_cli as adapter

    credential = "PRIVATE_LEFT" + "x" * 2000 + "PRIVATE_MIDDLE" + "y" * 2000 + "PRIVATE_RIGHT"
    body = "VISIBLE_HEAD " + credential + " VISIBLE_TAIL"
    monkeypatch.setenv("EXAMPLE_AGENT_ENV", "PRIVATE_ENV")
    sizes = tmp_path / "sizes.json"
    command = write_fake_cli(
        tmp_path,
        f"""
        prompt = sys.argv[sys.argv.index("-p") + 1]
        assert "PRIVATE_LEFT" in prompt and "PRIVATE_RIGHT" in prompt
        assert "PRIVATE_MIDDLE" not in prompt
        sys.stderr.write("PRIVATE_STDERR " + os.environ["EXAMPLE_AGENT_ENV"])
        response = json.dumps({{"status": "SUCCESS", "token": "PRIVATE_TOKEN",
            "url": "https://example.test/page?q=PRIVATE_QUERY",
            "response": "public text " + "x" * 10000}})
        Path({str(sizes)!r}).write_text(json.dumps([len(prompt.encode()),len(response.encode())]))
        sys.stdout.write(response)
        """,
    )
    logger = logging.getLogger(adapter.__name__)
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    stream = StringIO()
    session = observability.configure_debug_logging(tmp_path / "debug.log", stderr=stream)
    session.add_secrets(observability.SecretValue(credential))
    gate = CapacityGate(1)
    try:
        if mode != "debug":
            caplog.set_level(logging.INFO, logger=adapter.__name__)

            def unexpected(*args: object) -> None:
                pytest.fail("inactive logger prepared a body preview")

            monkeypatch.setattr(BodyScrubber, "project", unexpected)
            if mode == "info":
                monkeypatch.setattr(observability, "_active_scrubber", unexpected)
        async with gate.lease():
            task = asyncio.create_task(
                adapter.run_antigravity(
                    None,
                    [TruncatableMessage("", body)],
                    provider_name="alias",
                    command=str(command),
                    timeout_seconds=2,
                    pass_env=("EXAMPLE_AGENT_ENV",),
                    quota=gate,
                    prompt_max_bytes=500,
                )
            )
            await asyncio.sleep(0)
            if mode == "late_debug":
                caplog.set_level(logging.DEBUG, logger=adapter.__name__)
        assert await task == "public text " + "x" * 10000
    finally:
        session.close()
    lines = stream.getvalue()
    all_logs = (
        lines + (tmp_path / "debug.log").read_text() + repr([r.__dict__ for r in caplog.records])
    )
    for forbidden in ("PRIVATE_", "--output-format", "--print-timeout", "EXAMPLE_AGENT_ENV"):
        assert forbidden not in all_logs
    previews = [
        r for r in caplog.records if getattr(r, "gateway_event", None) == "upstream_body_preview"
    ]
    if mode == "info":
        assert not previews and "event=upstream_body_preview" not in lines
        return
    [preview] = previews
    fields = getattr(preview, "gateway_fields", {})
    sent_bytes, stdout_bytes = json.loads(sizes.read_text())
    assert (fields["provider"], fields["stage"], fields["attempt"], fields["transport"]) == (
        "alias",
        "agent_process",
        1,
        "agent_cli",
    )
    assert fields["endpoint"] is None and "protocol_attempt" not in fields
    assert fields["request_body_sent_bytes"] == sent_bytes <= 500
    assert fields["request_body_original_bytes"] == len(("[USER]\n" + body).encode())
    assert (
        fields["request_input_truncated"] is True and fields["request_preview_truncated"] is False
    )
    assert fields["response_body_bytes"] == stdout_bytes > 8192
    assert (
        fields["response_preview_truncated"] is True
        and "public text" in fields["response_body_preview"]
    )
    assert "https://example.test/page" in fields["response_body_preview"]
    if mode == "debug":
        assert (
            "VISIBLE_HEAD" in fields["request_body_preview"]
            and "VISIBLE_TAIL" in fields["request_body_preview"]
        )
        assert "VISIBLE_HEAD" in lines and "VISIBLE_TAIL" in lines
    else:
        assert fields["request_body_preview"] is None


@pytest.mark.parametrize("case", ["protected", "nul", "invalid_utf8"])
async def test_unfit_prompt_is_rejected_before_quota_and_spawn(
    case: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import asyncio
    import logging

    from agent_search_gateway.providers.agent.antigravity_cli import run_antigravity

    content = "PRIVATE" + {"protected": "x" * 100, "nul": "\x00", "invalid_utf8": "\ud800"}[case]
    gate = CapacityGate(1)

    async def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Rejected prompt reached spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    caplog.set_level(logging.DEBUG, logger="agent_search_gateway")
    with pytest.raises(ExecutionFailure) as caught:
        await run_antigravity(
            None,
            ({"role": "system", "content": content},),
            provider_name="agy",
            command="unused",
            timeout_seconds=1,
            pass_env=(),
            quota=gate,
            prompt_max_bytes=32,
        )
    assert caught.value.code is ErrorCode.ALL_PROVIDERS_FAILED
    assert caught.value.reason == "input_budget_unfit"
    assert caught.value.__context__ is None
    assert gate.max_observed_in_use == 0
    assert not any(
        getattr(r, "gateway_event", None) == "upstream_body_preview" for r in caplog.records
    )
    [record] = [r for r in caplog.records if getattr(r, "gateway_event", None) == "input_rejected"]
    fields = getattr(record, "gateway_fields", {})
    assert fields["reason"] == "input_budget_unfit"
    assert fields["limit_bytes"] == 32
    if case == "invalid_utf8":
        assert "request_body_original_bytes" not in fields
    else:
        assert fields["request_body_original_bytes"] == len(("[SYSTEM]\n" + content).encode())
    assert not record.exc_info
    assert "PRIVATE" not in repr(fields) + caplog.text + str(caught.value)


@pytest.mark.parametrize(
    ("platform", "page_size", "prompt_bytes", "reject"),
    [
        ("linux", 4096, 131072, True),
        ("linux", 4096, 131071, False),
        ("linux", 65536, 131072, False),
        ("darwin", 4096, 131072, False),
        ("linux", 0, 131072, False),
        ("linux", None, 131072, False),
    ],
)
async def test_agent_checks_only_reliable_single_argument_bound(
    platform: str,
    page_size: int | None,
    prompt_bytes: int,
    reject: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import errno
    import os
    import sys

    from agent_search_gateway.providers.agent.antigravity_cli import run_antigravity

    def sysconf(name: str) -> int:
        assert name == "SC_PAGE_SIZE"
        if page_size is None:
            raise OSError("page size unavailable")
        return page_size

    observed: list[int] = []

    async def capture_spawn(*argv: str, **kwargs: object) -> None:
        observed.append(len(argv[argv.index("-p") + 1].encode()))
        raise OSError(errno.E2BIG, "other arguments or environment exceed OS limit")

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os, "sysconf", sysconf)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_spawn)
    gate = CapacityGate(1)
    with pytest.raises(ExecutionFailure) as caught:
        await run_antigravity(
            None,
            ({"role": "user", "content": "a" * (prompt_bytes - len("[USER]\n"))},),
            provider_name="agy",
            command="unused",
            timeout_seconds=1,
            pass_env=(),
            quota=gate,
            prompt_max_bytes=256 * 1024,
        )

    assert caught.value.reason == ("input_budget_unfit" if reject else "argument_limit")
    assert observed == ([] if reject else [prompt_bytes])
    assert gate.max_observed_in_use == (0 if reject else 1)
    assert gate.in_use == 0


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
