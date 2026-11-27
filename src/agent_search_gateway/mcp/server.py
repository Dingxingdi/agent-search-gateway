"""MCP server for the gateway search and Fetch tools."""

import asyncio
import sys
from collections.abc import Mapping
from ipaddress import ip_address

from mcp import types
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette

from ..models import SearchScope
from ..paths import RuntimePaths
from .adapter import AdapterFailure, GatewayAdapter


class _QueryArgs(BaseModel, strict=True, extra="forbid"):
    query: str
    provider: str | None = None


class _PromptArgs(BaseModel, strict=True, extra="forbid"):
    prompt: str
    scope: SearchScope = "web"
    provider: str | None = None
    model: str | None = None


class _FetchArgs(BaseModel, strict=True, extra="forbid"):
    url: str
    focus: str | None = None


_QUERY_FILTER = "Filter by an exact, case-sensitive provider alias."
_PROMPT_FILTERS = (
    "Choose a web, paper, or all scope. Filter by an exact, case-sensitive provider alias "
    "or model, or supply both filters to select their intersection."
)
_TOOLS: dict[str, tuple[type[_QueryArgs | _PromptArgs | _FetchArgs], str]] = {
    "keyword_search": (
        _QueryArgs,
        f"Search the web by keyword and return JSONL text. {_QUERY_FILTER}",
    ),
    "paper_search": (_QueryArgs, f"Search academic papers and return JSONL text. {_QUERY_FILTER}"),
    "llm_search": (_PromptArgs, f"Search with an LLM and return JSONL text. {_PROMPT_FILTERS}"),
    "agent_search": (_PromptArgs, f"Search with an agent and return JSONL text. {_PROMPT_FILTERS}"),
    "url_fetch": (
        _FetchArgs,
        "Fetch a URL previously admitted by search and return its final text, optionally "
        "focused on a specific topic.",
    ),
}


def _text_result(text: str, *, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], is_error=is_error
    )


async def _dispatch(
    adapter: GatewayAdapter, name: str, args: _QueryArgs | _PromptArgs | _FetchArgs
) -> str:
    if isinstance(args, _QueryArgs):
        query_search = adapter.keyword_search if name == "keyword_search" else adapter.paper_search
        return await query_search(args.query, args.provider)
    if isinstance(args, _PromptArgs):
        prompt_search = adapter.llm_search if name == "llm_search" else adapter.agent_search
        return await prompt_search(args.prompt, args.scope, args.provider, args.model)
    return await adapter.url_fetch(args.url, args.focus)


def create_server(adapter: GatewayAdapter) -> Server[dict[str, object]]:
    tools = {
        name: types.Tool(name=name, description=description, input_schema=model.model_json_schema())
        for name, (model, description) in _TOOLS.items()
    }

    async def list_tools(
        ctx: ServerRequestContext[dict[str, object], object],
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=list(tools.values()))

    def input_schema(name: str) -> Mapping[str, object] | None:
        tool = tools.get(name)
        return tool.input_schema if tool is not None else None

    async def call_tool(
        ctx: ServerRequestContext[dict[str, object], object], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        definition = _TOOLS.get(params.name)
        if definition is None:
            raise MCPError(types.INVALID_PARAMS, "Unknown gateway tool.")
        model, _ = definition
        try:
            args = model.model_validate(params.arguments)
        except ValidationError:
            raise MCPError(types.INVALID_PARAMS, "Invalid tool arguments.") from None
        try:
            text = await _dispatch(adapter, params.name, args)
        except AdapterFailure as exc:
            message = exc.safe_message
            if exc.code is not None:
                message = f"{exc.code.value}: {message}"
            return _text_result(message, is_error=True)
        except Exception:
            print("Unexpected gateway tool failure", file=sys.stderr)
            return _text_result("Unexpected gateway tool failure.", is_error=True)
        return _text_result(text, is_error=False)

    return Server(
        "agent-search-gateway",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        get_tool_input_schema=input_schema,
    )


async def _run_stdio(server: Server[dict[str, object]]) -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def create_http_app(server: Server[dict[str, object]], host: str) -> Starlette:
    hosts = ["127.0.0.1", "localhost", "[::1]", "127.0.0.1:*", "localhost:*", "[::1]:*"]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{host_pattern}" for host_pattern in hosts],
    )
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=False,
        stateless_http=False,
        transport_security=security,
        host=host,
    )


def _is_loopback(host: str) -> bool:
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return host.removesuffix(".").lower() == "localhost"


def run_server(*, paths: RuntimePaths, transport: str, host: str | None, port: int | None) -> None:
    """Run the selected MCP transport against the existing gateway daemon."""
    server = create_server(GatewayAdapter(paths))
    if transport == "stdio":
        asyncio.run(_run_stdio(server))
    elif transport == "streamable-http" and host is not None and port is not None:
        import uvicorn

        if not _is_loopback(host):
            print(
                "Warning: non-loopback MCP bind. Use a trusted proxy or tunnel with "
                "authentication and TLS.",
                file=sys.stderr,
            )
        uvicorn.run(create_http_app(server, host), host=host, port=port, access_log=False)
    else:
        raise ValueError("Unsupported MCP transport settings")
