"""Thin command-line client for the local foreground gateway daemon."""

import argparse
import asyncio
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Protocol, TextIO

from .daemon import ForegroundDaemon
from .doctor import DoctorReport, render_doctor, run_doctor
from .errors import DaemonUnavailable, ErrorCode, GatewayError, InputFailure
from .models import (
    ErrorResponse,
    KeywordSearchRequest,
    LLMSearchRequest,
    PaperSearchRequest,
    Request,
    Response,
    ShutdownRequest,
    SuccessResponse,
    URLFetchRequest,
)
from .observability import DebugLoggingSession, configure_debug_logging
from .paths import RuntimePaths
from .protocol import send_request
from .url_normalization import normalize_url

EXIT_OK = 0
EXIT_ERROR = 1
_START_INSTRUCTION = "Start the daemon with: agent-search-gateway start"

SocketClient = Callable[[Path, Request], Awaitable[Response]]


class DaemonLike(Protocol):
    async def start(self) -> None: ...


DaemonFactory = Callable[..., DaemonLike]
LoggingConfigurer = Callable[..., DebugLoggingSession]
DoctorRunner = Callable[..., Awaitable[DoctorReport]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-search-gateway")
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start")
    start.add_argument("--debug", action="store_true")
    subparsers.add_parser("stop")
    subparsers.add_parser("doctor")

    keyword = subparsers.add_parser("keyword-search")
    keyword.add_argument("query")
    keyword.add_argument(
        "--provider", metavar="NAME", help="Select an enabled keyword-search provider"
    )

    paper = subparsers.add_parser("paper-search")
    paper.add_argument("query")
    paper.add_argument("--provider", metavar="NAME", help="Select an enabled paper-search provider")

    llm = subparsers.add_parser("llm-search")
    llm.add_argument("prompt")
    llm.add_argument("--scope", choices=("web", "paper", "all"), default="web")
    llm.add_argument(
        "--provider", metavar="NAME", help="Select a configured search LLM provider alias"
    )
    llm.add_argument("--model", metavar="NAME", help="Select an exact configured search model")

    fetch = subparsers.add_parser("url-fetch")
    fetch.add_argument("url")
    fetch.add_argument("focus", nargs="?")
    return parser


def _normalize_selector(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise InputFailure(ErrorCode.BAD_REQUEST, f"{label} must not be empty")
    return value


def _request_from_args(args: argparse.Namespace) -> Request:
    if args.command == "stop":
        return ShutdownRequest()
    if args.command == "keyword-search":
        query = args.query.strip()
        if not query:
            raise InputFailure(ErrorCode.EMPTY_QUERY, "Query must not be empty")
        return KeywordSearchRequest(query, provider=_normalize_selector(args.provider, "Provider"))
    if args.command == "paper-search":
        query = args.query.strip()
        if not query:
            raise InputFailure(ErrorCode.EMPTY_QUERY, "Query must not be empty")
        return PaperSearchRequest(query, provider=_normalize_selector(args.provider, "Provider"))
    if args.command == "llm-search":
        prompt = args.prompt.strip()
        if not prompt:
            raise InputFailure(ErrorCode.EMPTY_QUERY, "Prompt must not be empty")
        return LLMSearchRequest(
            prompt,
            args.scope,
            provider=_normalize_selector(args.provider, "Provider"),
            model=_normalize_selector(args.model, "Model"),
        )
    if args.command == "url-fetch":
        url = normalize_url(args.url)
        focus = args.focus.strip() if args.focus is not None and args.focus.strip() else None
        return URLFetchRequest(str(url), focus)
    raise InputFailure(ErrorCode.BAD_REQUEST, "Unknown command")


def _write_text(stream: TextIO, text: str) -> None:
    stream.write(text)
    if not text.endswith("\n"):
        stream.write("\n")


async def run_command(
    args: argparse.Namespace,
    paths: RuntimePaths,
    *,
    client: SocketClient = send_request,
    daemon_factory: DaemonFactory = ForegroundDaemon,
    logging_configurer: LoggingConfigurer = configure_debug_logging,
    doctor_runner: DoctorRunner = run_doctor,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if args.command == "start":
        loop = asyncio.get_running_loop()
        current_task = asyncio.current_task()
        terminated = False
        signal_handler_installed = False
        logging_session: DebugLoggingSession | None = None

        def cancel_for_sigterm() -> None:
            nonlocal terminated
            terminated = True
            if current_task is not None:
                current_task.cancel()

        try:
            if args.debug:
                logging_session = logging_configurer(paths.debug_log_file, stderr=stderr)
            daemon = daemon_factory(
                paths,
                debug=args.debug,
                logging_session=logging_session,
            )
            try:
                loop.add_signal_handler(signal.SIGTERM, cancel_for_sigterm)
                signal_handler_installed = True
            except (NotImplementedError, RuntimeError, ValueError):
                pass
            await daemon.start()
        except asyncio.CancelledError:
            if terminated:
                return 128 + signal.SIGTERM
            raise
        except GatewayError as exc:
            _write_text(stderr, exc.message)
            return EXIT_ERROR
        finally:
            if signal_handler_installed:
                loop.remove_signal_handler(signal.SIGTERM)
            if logging_session is not None:
                logging_session.close()
        return EXIT_OK

    if args.command == "doctor":
        try:
            report = await doctor_runner(
                paths,
                environ=os.environ if environ is None else environ,
            )
        except Exception:
            _write_text(stderr, "[fail] doctor internal error")
            return EXIT_ERROR
        render_doctor(report, stdout)
        return report.exit_code

    try:
        request = _request_from_args(args)
    except GatewayError as exc:
        _write_text(stderr, exc.message)
        return EXIT_ERROR

    try:
        response = await client(paths.socket_file, request)
    except DaemonUnavailable:
        if isinstance(request, ShutdownRequest):
            _write_text(stdout, "Daemon is not running.")
            return EXIT_OK
        _write_text(stderr, _START_INSTRUCTION)
        return EXIT_ERROR
    except GatewayError as exc:
        _write_text(stderr, exc.message)
        return EXIT_ERROR

    if isinstance(response, SuccessResponse):
        _write_text(stdout, response.text)
        return EXIT_OK
    if isinstance(response, ErrorResponse):
        _write_text(stderr, response.message)
        return EXIT_ERROR
    _write_text(stderr, "Invalid daemon response")
    return EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(
        run_command(
            args,
            RuntimePaths.default(),
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    )
