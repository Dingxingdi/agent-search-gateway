from pathlib import Path

import httpx
import pytest

from agent_search_gateway.doctor import run_doctor
from agent_search_gateway.paths import RuntimePaths
from agent_search_gateway.runtime import Runtime
from tests.doctor._support import environment_name, write_valid_config


async def test_doctor_does_not_build_runtime_or_construct_http_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = RuntimePaths.from_home(tmp_path)
    write_valid_config(paths.config_file)

    def forbidden_runtime_build(*args: object, **kwargs: object) -> Runtime:
        raise AssertionError("doctor must not build Runtime")

    class ForbiddenClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("doctor must not construct HTTP clients")

    monkeypatch.setattr(Runtime, "build", forbidden_runtime_build)
    monkeypatch.setattr(httpx, "AsyncClient", ForbiddenClient)

    report = await run_doctor(
        paths,
        environ={environment_name(): "opaque-runtime-value-7391"},
    )

    assert report.exit_code == 0
    assert not paths.debug_log_file.exists()
    assert not paths.results_dir.exists()
    assert not paths.socket_file.exists()


async def test_doctor_agent_default_model_and_missing_command_stay_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import subprocess

    paths = RuntimePaths.from_home(tmp_path)
    write_valid_config(paths.config_file)
    with paths.config_file.open("a") as target:
        target.write(
            '[agent_providers.local]\nharness = "antigravity"\n'
            'command = "/missing/agy"\n'
            '[[search_agent.providers]]\nprovider = "local"\n'
        )

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("doctor must not probe Agent, HTTP, or Runtime")

    monkeypatch.setattr(Runtime, "build", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    report = await run_doctor(paths, environ={environment_name(): "[REDACTED_SECRET]"})
    assert report.exit_code == 0
    assert report.checks[0].message == "configuration valid"
    assert not paths.socket_file.exists() and not paths.results_dir.exists()
