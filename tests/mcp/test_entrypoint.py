"""Packaging and import boundaries for the optional MCP interface."""

import builtins
import importlib
import importlib.util
import socket
import subprocess
import sys
import textwrap
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from agent_search_gateway.paths import RuntimePaths


def test_lazy_import_entrypoint_is_available() -> None:
    assert importlib.util.find_spec("agent_search_gateway.mcp.__main__") is not None


def _entry_main() -> Callable[[Sequence[str] | None], int]:
    module = importlib.import_module("agent_search_gateway.mcp.__main__")
    main = getattr(module, "main", None)
    assert callable(main), "the MCP entrypoint must expose main(argv)"
    return cast(Callable[[Sequence[str] | None], int], main)


@pytest.mark.parametrize(
    ("argv", "transport", "host", "port"),
    [
        ([], "stdio", None, None),
        (["--transport", "stdio"], "stdio", None, None),
        (["--transport", "streamable-http"], "streamable-http", "127.0.0.1", 8765),
        (
            ["--transport", "streamable-http", "--host", "localhost", "--port", "9123"],
            "streamable-http",
            "localhost",
            9123,
        ),
        (
            ["--transport", "streamable-http", "--host", "::1", "--port", "65535"],
            "streamable-http",
            "::1",
            65535,
        ),
    ],
)
def test_options_pass_validated_settings_to_runner(
    argv: list[str],
    transport: str,
    host: str | None,
    port: int | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []
    server = ModuleType("agent_search_gateway.mcp.server")

    def run_server(**options: object) -> None:
        calls.append(options)

    server.run_server = run_server  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, server.__name__, server)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert _entry_main()(argv) == 0

    assert calls == [
        {
            "paths": RuntimePaths.from_home(tmp_path),
            "transport": transport,
            "host": host,
            "port": port,
        }
    ]
    assert capsys.readouterr() == ("", "")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "argv",
    [
        ["--transport", "invalid"],
        ["--host", "127.0.0.1"],
        ["--port", "8765"],
        ["--transport", "stdio", "--host", "127.0.0.1"],
        ["--transport", "stdio", "--port", "8765"],
        *[
            ["--transport", "streamable-http", "--host", host]
            for host in (
                "",
                " ",
                " local.test",
                "local.test ",
                "bad host",
                "http://localhost",
                "localhost/path",
                "localhost:8765",
                "[::1]",
                "bad\nname",
                "bad\x00name",
                "bad\tname",
                "bad_name",
                "-bad.test",
                "bad-.test",
                "bad..test",
                "256.0.0.1",
                "fe80::1%eth0/path",
                "fe80::1%eth0\\path",
            )
        ],
        *[
            ["--transport", "streamable-http", "--port", port]
            for port in ("word", "0", "-1", "65536", "1.5")
        ],
    ],
)
def test_options_reject_usage_before_loading_runner(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main = _entry_main()
    imported: list[str] = []
    original_import = builtins.__import__
    server = ModuleType("agent_search_gateway.mcp.server")
    server.run_server = lambda **options: None  # type: ignore[attr-defined]

    def track_import(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> ModuleType:
        imported.append(name)
        if name in {"server", server.__name__}:
            return server
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", track_import)
    status: object
    try:
        status = main(argv)
    except SystemExit as error:
        status = error.code
    captured = capsys.readouterr()

    assert status != 0
    assert captured.out == ""
    assert "usage:" in captured.err
    assert "server" not in imported
    assert "agent_search_gateway.mcp.server" not in imported


@pytest.mark.parametrize(
    "failure",
    [
        ModuleNotFoundError("private-token /private/config", name="mcp"),
        ModuleNotFoundError("private-token /private/config", name="mcp.server"),
        ModuleNotFoundError("private-token /private/config", name="unrelated_dependency"),
        ImportError("private-token /private/config"),
        RuntimeError("private-token /private/config"),
    ],
    ids=[
        "missing-extra",
        "missing-sdk-submodule",
        "unrelated-module",
        "import-bug",
        "import-runtime",
    ],
)
def test_missing_extra_only_for_root_sdk_module(
    failure: Exception,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main = _entry_main()
    original_import = builtins.__import__

    def fail_server_import(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> ModuleType:
        if name in {"server", "agent_search_gateway.mcp.server"}:
            raise failure
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fail_server_import)
    outcome: object
    try:
        outcome = main([])
    except Exception as error:
        outcome = error
    captured = capsys.readouterr()

    assert isinstance(outcome, int), "startup failures must become safe nonzero status"
    assert outcome != 0
    assert captured.out == ""
    assert captured.err
    assert "private-token" not in captured.err
    assert "/private/config" not in captured.err
    assert "Traceback" not in captured.err
    if isinstance(failure, ModuleNotFoundError) and failure.name == "mcp":
        assert "uv tool install '.[mcp]'" in captured.err
        assert "source directory" in captured.err
    else:
        assert "startup" in captured.err.lower()
        assert "uv tool install" not in captured.err


@pytest.mark.parametrize(
    "failure",
    [
        OSError("private-token /private/config"),
        RuntimeError("private-token /private/config"),
        ModuleNotFoundError("private-token /private/config", name="mcp"),
    ],
    ids=["bind", "startup", "runtime-module"],
)
def test_lazy_import_startup_failure_has_safe_diagnostic(
    failure: Exception,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = ModuleType("agent_search_gateway.mcp.server")

    def run_server(**options: object) -> None:
        raise failure

    server.run_server = run_server  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, server.__name__, server)
    outcome: object
    try:
        outcome = _entry_main()([])
    except Exception as error:
        outcome = error
    captured = capsys.readouterr()

    assert isinstance(outcome, int), "transport failures must become safe nonzero status"
    assert outcome != 0
    assert captured.out == ""
    assert "startup" in captured.err.lower()
    assert "uv tool install" not in captured.err
    assert "private-token" not in captured.err
    assert "/private/config" not in captured.err
    assert "Traceback" not in captured.err


def test_http_startup_system_exit_has_safe_diagnostic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = ModuleType("agent_search_gateway.mcp.server")

    def fail(**options: object) -> None:
        raise SystemExit("private-detail")

    monkeypatch.setattr(server, "run_server", fail, raising=False)
    monkeypatch.setitem(sys.modules, server.__name__, server)
    try:
        status: object = _entry_main()(["--transport", "streamable-http"])
    except SystemExit as exc:
        status = exc
    assert status == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "startup" in captured.err.lower()
    assert "private-detail" not in captured.err


def test_http_bind_failure_exits_child_with_safe_stderr(mcp_home: Path) -> None:
    script = textwrap.dedent(
        """
        import runpy
        import sys

        target = ("127.0.0.1", int(sys.argv[-1]))
        attempted = []
        def observe(event, args):
            if event == "socket.bind" and args[1] == target:
                attempted.append(target)
        sys.addaudithook(observe)

        try:
            runpy.run_module("agent_search_gateway.mcp", run_name="__main__", alter_sys=True)
        except SystemExit:
            if not attempted:
                raise SystemExit(99)
            raise
        """
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                script,
                "--transport",
                "streamable-http",
                "--port",
                str(port),
            ],
            cwd=mcp_home,
            env={"HOME": str(mcp_home)},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "startup" in result.stderr.lower()
    assert "Traceback" not in result.stderr and str(mcp_home) not in result.stderr
    assert list(mcp_home.iterdir()) == []


def test_sdk_startup_failure_exits_child_with_safe_stderr(mcp_home: Path) -> None:
    script = textwrap.dedent(
        """
        from unittest.mock import patch
        from mcp.server import Server
        from agent_search_gateway.mcp.__main__ import main

        with patch.object(
            Server, "create_initialization_options", side_effect=RuntimeError("private-detail")
        ) as initialize:
            status = main([])
        # Distinguish a real SDK startup failure from an earlier unrelated failure.
        raise SystemExit(status if initialize.called else 99)
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=mcp_home,
        env={"HOME": str(mcp_home)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "startup" in result.stderr.lower()
    assert "Traceback" not in result.stderr and "private-detail" not in result.stderr
    assert list(mcp_home.iterdir()) == []


def test_lazy_import_help_without_sdk_or_runtime_access(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import builtins
        import runpy
        import sys

        original_import = builtins.__import__
        def without_optional(name, *args, **kwargs):
            if name.split(".")[0] in {"mcp", "pydantic", "uvicorn"}:
                raise AssertionError("help must not load optional dependencies")
            if name.endswith(".server") or name == "server":
                raise AssertionError("help must not load the runner")
            return original_import(name, *args, **kwargs)
        builtins.__import__ = without_optional

        def audit(event, args):
            if event.startswith("socket.") or event in {"os.mkdir", "os.remove"}:
                raise AssertionError("help must not touch runtime resources")
            if event == "open" and isinstance(args[0], str):
                assert "config.toml" not in args[0] and ".cache/" not in args[0]
        sys.addaudithook(audit)
        sys.argv = ["agent-search-gateway-mcp", "--help"]
        runpy.run_module("agent_search_gateway.mcp", run_name="__main__")
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=tmp_path,
        env={"HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert "--transport" in result.stdout
    assert "--host" in result.stdout
    assert "--port" in result.stdout
    assert result.stderr == ""
    assert list(tmp_path.iterdir()) == []


def test_options_resolve_paths_without_configuration_or_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main = _entry_main()
    calls: list[dict[str, object]] = []
    server = ModuleType("agent_search_gateway.mcp.server")

    def run_server(**options: object) -> None:
        calls.append(options)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("entrypoint must not read runtime files or probe the daemon")

    server.run_server = run_server  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, server.__name__, server)
    monkeypatch.setenv("HOME", str(tmp_path))
    for method in ("open", "read_text", "read_bytes", "exists", "is_file", "mkdir"):
        monkeypatch.setattr(Path, method, forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)

    assert main(["--transport", "streamable-http", "--host", "example.test"]) == 0
    assert calls[0]["paths"] == RuntimePaths.from_home(tmp_path)
    assert capsys.readouterr() == ("", "")


def test_optional_mcp_metadata() -> None:
    root = Path(__file__).resolve().parents[2]
    with (root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]

    assert project["scripts"].get("agent-search-gateway-mcp") == (
        "agent_search_gateway.mcp.__main__:main"
    )
    assert project.get("optional-dependencies", {}).get("mcp") == [
        "mcp>=2.3.0,<3",
        "pydantic>=2.12,<3",
        "uvicorn>=0.31.1,<1",
    ]
    assert project["dependencies"] == ["httpx>=0.27,<1"]
    assert project["scripts"]["agent-search-gateway"] == "agent_search_gateway.cli:main"


@pytest.mark.parametrize("module", ["agent_search_gateway.cli", "agent_search_gateway.mcp"])
def test_import_has_no_optional_dependencies_or_runtime_side_effects(
    module: str, tmp_path: Path
) -> None:
    script = textwrap.dedent(
        """\
        import importlib
        import importlib.util
        import sys

        socket_events = []
        def audit(event, args):
            if event.startswith("socket."):
                socket_events.append(event)
        sys.addaudithook(audit)

        module = sys.argv[1]
        assert importlib.util.find_spec(module) is not None, f"{module} must be importable"
        importlib.import_module(module)
        loaded = {"mcp", "pydantic", "uvicorn"}.intersection(
            name.split(".")[0] for name in sys.modules
        )
        assert not loaded, f"unexpected optional imports: {sorted(loaded)}"
        assert not socket_events, f"unexpected socket activity: {socket_events}"
        """
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, module],
        cwd=tmp_path,
        env={"HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert list(tmp_path.iterdir()) == []
