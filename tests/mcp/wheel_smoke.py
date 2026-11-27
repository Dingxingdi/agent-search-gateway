"""Exercise one installed wheel using its tool environment's Python and exposed scripts."""

import argparse
import asyncio
import importlib.util
import os
import platform
import subprocess
import sys
import sysconfig
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

_CLI = "agent-search-gateway"
_CLI_PROBE = """
import sys
import sysconfig
from pathlib import Path
from agent_search_gateway import cli

assert Path(cli.__file__).resolve().is_relative_to(Path(sysconfig.get_path("purelib")).resolve())
if sys.argv[1:]:
    try:
        status = cli.main(sys.argv[1:])
    except SystemExit as exc:
        status = exc.code
    assert status == 0
for name in ("agent_search_gateway.mcp", "mcp", "pydantic", "uvicorn"):
    assert name not in sys.modules, f"CLI loaded {name}"
"""


def _run(home: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        env={"HOME": str(home), "PATH": os.defpath},
        cwd=home,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )


async def _check_mcp(executable: Path, home: Path) -> None:
    from mcp import types
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters

    assert Path(types.__file__).resolve().is_relative_to(Path(sysconfig.get_path("purelib")))
    params = StdioServerParameters(command=str(executable), cwd=home, env={"HOME": str(home)})
    async with asyncio.timeout(25), Client(params, cache=None) as client:
        assert client.protocol_version
        tools = (await client.list_tools()).tools
        assert sorted(tool.name for tool in tools) == [
            "agent_search",
            "keyword_search",
            "llm_search",
            "paper_search",
            "url_fetch",
        ]
        result = await client.call_tool("keyword_search", {"query": "wheel smoke"})
        assert result.is_error and result.structured_content is None
        (message,) = result.content
        assert isinstance(message, types.TextContent)
        assert "agent-search-gateway start" in message.text
        assert "Traceback" not in message.text and "/" not in message.text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("base", "mcp"), required=True)
    parser.add_argument("--bin-dir", type=Path, required=True)
    args = parser.parse_args()
    cli = (args.bin_dir / _CLI).absolute()
    mcp = cli.with_name(f"{_CLI}-mcp")
    scripts = Path(sysconfig.get_path("scripts"))
    for executable in (cli, mcp):
        assert executable.samefile(scripts / executable.name), f"wrong installation: {executable}"

    with TemporaryDirectory(dir="/tmp", prefix="asg-") as directory:
        home = Path(directory)
        for flags, expected in [
            (["--help"], "keyword-search"),
            (["--version"], f"{_CLI} {version(_CLI)}"),
            (["start", "--help"], "--debug"),
        ]:
            result = _run(home, str(cli), *flags)
            result.check_returncode()
            assert expected in result.stdout and not result.stderr

        for flags in ([], ["--version"]):
            probe = _run(home, sys.executable, "-I", "-c", _CLI_PROBE, *flags)
            assert probe.returncode == 0, probe.stderr

        if args.mode == "base":
            assert importlib.util.find_spec("mcp") is None, "base install includes the MCP SDK"
            help_result = _run(home, str(mcp), "--help")
            help_result.check_returncode()
            assert "--transport" in help_result.stdout and not help_result.stderr
            missing = _run(home, str(mcp))
            assert missing.returncode != 0 and missing.stdout == ""
            assert "source directory" in missing.stderr
            assert "uv tool install '.[mcp]'" in missing.stderr
            assert "Traceback" not in missing.stderr and "/" not in missing.stderr
            sdk = "absent"
        else:
            asyncio.run(_check_mcp(mcp, home))
            # The SDK owns session cleanup. Check the executable's exit status on STDIO EOF too.
            eof = _run(home, str(mcp))
            eof.check_returncode()
            assert eof.stdout == "" and "Traceback" not in eof.stderr
            sdk = version("mcp")

    print(
        f"{args.mode} wheel smoke passed on {platform.system()} {platform.machine()}, "
        f"Python {platform.python_version()}, MCP SDK {sdk}"
    )


if __name__ == "__main__":
    # Ignore the caller's PYTHONPATH, user site, and checkout when importing the installed SDK.
    if not sys.flags.isolated:
        os.execv(sys.executable, [sys.executable, "-I", *sys.argv])
    main()
