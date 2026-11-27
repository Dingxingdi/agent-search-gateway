"""Command entry point for the optional MCP interface."""

import argparse
import ipaddress
import re
import sys
from collections.abc import Sequence

from agent_search_gateway.paths import RuntimePaths


def _host(value: str) -> str:
    if not value or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 or character in "/\\\\"
        for character in value
    ):
        raise argparse.ArgumentTypeError("host must be an IP address or hostname")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        hostname = value.removesuffix(".")
        if (
            len(hostname) > 253
            or all(character in "0123456789." for character in hostname)
            or not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in hostname.split(".")
            )
        ):
            raise argparse.ArgumentTypeError("host must be an IP address or hostname") from None
    return value


def _startup_failure() -> int:
    print(
        "MCP server startup failed; check the installation and transport settings.",
        file=sys.stderr,
    )
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-search-gateway-mcp")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", type=_host)
    parser.add_argument("--port", type=int)
    options = parser.parse_args(argv)
    if options.transport == "stdio" and (options.host is not None or options.port is not None):
        parser.error("--host and --port require --transport streamable-http")
    if options.port is not None and not 1 <= options.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if options.transport == "streamable-http":
        if options.host is None:
            options.host = "127.0.0.1"
        if options.port is None:
            options.port = 8765

    try:
        try:
            from .server import run_server
        except ModuleNotFoundError as error:
            if error.name != "mcp":
                raise
            print(
                "MCP dependencies are missing; from the source directory run "
                "uv tool install '.[mcp]'.",
                file=sys.stderr,
            )
            return 1
        run_server(
            paths=RuntimePaths.default(),
            transport=options.transport,
            host=options.host,
            port=options.port,
        )
    except SystemExit as error:
        if error.code not in (None, 0):
            return _startup_failure()
    except Exception:
        return _startup_failure()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
