from pathlib import Path

import pytest

from agent_search_gateway import doctor
from agent_search_gateway.config import load_toml
from agent_search_gateway.doctor import DoctorStatus, run_doctor
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.socket_probe import SocketProbeResult, SocketState
from tests.doctor._support import environment_name, write_valid_config


async def _missing_socket(_path: Path) -> SocketProbeResult:
    return SocketProbeResult(SocketState.MISSING)


async def test_doctor_reports_configured_and_unlimited_credential_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    write_valid_config(paths.config_file)
    data = load_toml(paths.config_file)
    providers = data["llm_providers"]
    assert isinstance(providers, dict)
    members = providers["primary"]["credentials"]
    members.append(dict(members[0]))
    members[0].update(
        {
            "max_concurrency": 2,
            "rate_limits": [
                {"requests": 3, "period_seconds": 2},
                {"requests": 10, "period_seconds": 60},
            ],
            "min_interval_seconds": 0.5,
        }
    )
    monkeypatch.setattr(doctor, "load_toml", lambda path: data)

    report = await run_doctor(
        paths, environ={environment_name(): "present"}, socket_probe=_missing_socket
    )

    assert report.exit_code == 0
    details = [check.message for check in report.checks if check.status is DoctorStatus.INFO]
    assert (
        "llm_providers.primary.credentials[0]: max_concurrency=2, "
        "rate_limits=[3 requests/2.0s, 10 requests/60.0s], min_interval_seconds=0.5"
    ) in details
    assert (
        "llm_providers.primary.credentials[1]: max_concurrency=unlimited, "
        "rate_limits=none, min_interval_seconds=none"
    ) in details
