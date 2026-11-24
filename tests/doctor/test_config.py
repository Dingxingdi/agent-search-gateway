from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from agent_search_gateway import doctor
from agent_search_gateway.config import resolve_config
from agent_search_gateway.doctor import DoctorCheck, DoctorStatus, run_doctor
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.socket_probe import SocketProbeResult, SocketState
from tests.doctor._support import environment_name, write_valid_config


async def _missing_socket(_path: Path) -> SocketProbeResult:
    return SocketProbeResult(SocketState.MISSING)


async def test_doctor_reports_missing_and_invalid_config_without_stopping_other_checks(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    report = await run_doctor(paths, environ={}, socket_probe=_missing_socket)

    assert report.exit_code == 1
    assert report.checks[0].status is DoctorStatus.FAIL
    assert "config file not found" in report.checks[0].message
    assert any("directory is creatable" in check.message for check in report.checks)
    assert report.checks[-1].status is DoctorStatus.INFO

    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text("not = [valid", encoding="utf-8")
    malformed = await run_doctor(paths, environ={}, socket_probe=_missing_socket)
    assert malformed.checks[0].status is DoctorStatus.FAIL
    assert "config parse failed" in malformed.checks[0].message
    assert malformed.checks[-1].status is DoctorStatus.INFO


async def test_doctor_resolves_real_config_and_mentions_only_environment_name(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    write_valid_config(paths.config_file)
    env_name = environment_name()
    opaque = "opaque-runtime-value-7391"

    report = await run_doctor(
        paths,
        environ={env_name: opaque},
        socket_probe=_missing_socket,
    )

    assert report.exit_code == 0
    messages = "\n".join(check.message for check in report.checks)
    assert "configuration valid" in messages
    assert f"environment variable {env_name} is set" in messages
    assert opaque not in messages
    assert opaque not in repr(report)


async def test_doctor_resolves_config_once_and_passes_enabled_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    write_valid_config(paths.config_file)
    with paths.config_file.open("a") as target:
        target.write(
            '[web_providers.open_websearch]\nenable_fetch = true\napi_url = "http://example.test"\n'
        )
    resolve = Mock(wraps=resolve_config)
    monkeypatch.setattr(doctor, "resolve_config", resolve)
    healthy = DoctorCheck(DoctorStatus.OK, "open-webSearch daemon healthy")
    probe = AsyncMock(return_value=healthy)
    monkeypatch.setattr(doctor, "_probe_open_websearch_health", probe)

    report = await run_doctor(paths, environ={environment_name(): "present"})

    resolve.assert_called_once()
    probe.assert_awaited_once_with("http://example.test")
    assert report.checks[:2] == (
        DoctorCheck(DoctorStatus.OK, "configuration valid"),
        DoctorCheck(DoctorStatus.OK, f"environment variable {environment_name()} is set"),
    )
    assert healthy in report.checks


@pytest.mark.parametrize(
    ("case", "first_check"),
    [
        ("unconfigured", "configuration valid"),
        ("disabled", "configuration valid"),
        ("missing", "config file not found"),
        ("malformed", "config parse failed"),
        ("missing_env", "configuration invalid"),
        ("unknown_option", "configuration invalid"),
    ],
)
async def test_doctor_skips_health_for_missing_disabled_or_invalid_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, first_check: str
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    if case != "missing":
        write_valid_config(paths.config_file)
        with paths.config_file.open("a") as target:
            if case == "disabled":
                target.write(
                    "[web_providers.open_websearch]\nenable_search = false\nenable_fetch = false\n"
                )
            elif case != "unconfigured":
                target.write("[web_providers.open_websearch]\nenable_search = true\n")
                if case == "malformed":
                    target.write("broken = [\n")
                elif case == "unknown_option":
                    target.write("unknown_option = true\n")
    probe = AsyncMock(side_effect=AssertionError("unexpected health check"))
    monkeypatch.setattr(doctor, "_probe_open_websearch_health", probe)
    environ = {} if case == "missing_env" else {environment_name(): "present"}

    report = await run_doctor(paths, environ=environ)

    probe.assert_not_awaited()
    assert report.checks[0].message.startswith(first_check)
    assert report.exit_code == (0 if first_check == "configuration valid" else 1)
    assert [check.message for check in report.checks[-4:-1]] == [
        f"directory is creatable: {path}"
        for path in (paths.socket_file.parent, paths.results_dir, paths.logs_dir)
    ]
    assert report.checks[-1] == DoctorCheck(DoctorStatus.INFO, "daemon not running")


@pytest.mark.parametrize(
    ("api_url", "with_key", "status", "message"),
    [
        (None, False, DoctorStatus.FAIL, "You.com REST requires an API key"),
        (None, True, DoctorStatus.OK, "configuration valid"),
        ("https://api.you.com/mcp/free", False, DoctorStatus.OK, "configuration valid"),
        ("https://api.you.com/mcp/free", True, DoctorStatus.FAIL, "free MCP does not accept"),
    ],
)
async def test_doctor_checks_youcom_protocol_credentials(
    tmp_path: Path, api_url: str | None, with_key: bool, status: DoctorStatus, message: str
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    write_valid_config(paths.config_file)
    with paths.config_file.open("a") as target:
        target.write("[web_providers.youcom]\nenable_search = true\n")
        if api_url is not None:
            target.write(f'api_url = "{api_url}"\n')
        if with_key:
            target.write('api_key_env = "TEST_WEB"\n')

    report = await run_doctor(
        paths, environ={environment_name(): "x", "TEST_WEB": "x"}, socket_probe=_missing_socket
    )

    assert report.checks[0].status is status
    assert message in report.checks[0].message


async def test_doctor_surfaces_real_config_resolution_failure_safely(tmp_path: Path) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    write_valid_config(paths.config_file)

    report = await run_doctor(paths, environ={}, socket_probe=_missing_socket)

    assert report.exit_code == 1
    assert report.checks[0].status is DoctorStatus.FAIL
    assert environment_name() in report.checks[0].message
    assert "configuration invalid" in report.checks[0].message


@pytest.mark.parametrize(
    "model_line", ['model = ""', 'model = " "', "model = 7", "model = []", "extra_body = {}"]
)
async def test_doctor_reports_invalid_agent_configuration(tmp_path: Path, model_line: str) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    write_valid_config(paths.config_file)
    with paths.config_file.open("a") as target:
        target.write(
            '[agent_providers.local]\nharness = "antigravity"\n'
            '[[search_agent.providers]]\nprovider = "local"\n' + model_line + "\n"
        )
    report = await run_doctor(
        paths,
        environ={environment_name(): "[REDACTED_SECRET]"},
        socket_probe=_missing_socket,
    )
    assert report.exit_code == 1
    assert report.checks[0].status is DoctorStatus.FAIL
    assert "configuration invalid" in report.checks[0].message
