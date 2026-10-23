from pathlib import Path

import pytest

from agent_search_gateway.doctor import DoctorStatus, run_doctor
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
