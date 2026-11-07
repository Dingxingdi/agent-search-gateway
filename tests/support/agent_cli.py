"""Executable fake harnesses for offline process and socket acceptance tests."""

import sys
import textwrap
from pathlib import Path


def write_fake_cli(directory: Path, body: str, *, name: str = "agy") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, subprocess, sys, time\nfrom pathlib import Path\n"
        + textwrap.dedent(body),
        encoding="utf-8",
    )
    target.chmod(0o700)
    return target


async def wait_for_json(path: Path) -> dict[str, object]:
    import asyncio
    import json

    async with asyncio.timeout(3):
        while not path.exists():
            await asyncio.sleep(0.005)
    value = json.loads(path.read_text())
    assert isinstance(value, dict)
    return value


def process_is_alive(pid: int) -> bool:
    import os
    import subprocess

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def force_cleanup(state: dict[str, object]) -> None:
    import os
    import signal
    from contextlib import suppress

    pid = state.get("pid")
    if isinstance(pid, int):
        with suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
