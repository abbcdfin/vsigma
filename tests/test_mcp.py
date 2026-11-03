"""MCP tool adapter test — real adapter, in-memory transport (no network/subprocess)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast

from mcp import ClientSession

from vsigma import ConnectionFactory, Tool, make_mcp_tools


def _register_tools(server: Any) -> None:
    @server.tool()  # type: ignore[untyped-decorator]
    def greet(name: str) -> str:
        return f"hello {name}"

    @server.tool()  # type: ignore[untyped-decorator]
    def boom() -> str:
        raise RuntimeError("kaboom")


def _test_connection() -> ConnectionFactory:
    try:
        from mcp import Client
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP  # type: ignore[attr-defined]
        from mcp.shared.memory import (  # type: ignore[attr-defined]
            create_connected_server_and_client_session,
        )

        server1 = FastMCP("test")
        _register_tools(server1)
        return lambda: cast(
            "AbstractAsyncContextManager[ClientSession]",
            create_connected_server_and_client_session(server1),
        )

    server2 = MCPServer("test")
    _register_tools(server2)

    @asynccontextmanager
    async def connect() -> AsyncGenerator[ClientSession, None]:
        async with Client(server2) as client:
            yield client.session

    return connect


def test_make_mcp_tools_wraps_and_invokes() -> None:
    with make_mcp_tools(_test_connection()) as toolset:
        by_name = {tool.name: tool for tool in toolset.tools}

        # The MCP tool is wrapped as a native vsigma Tool...
        assert "greet" in by_name
        assert isinstance(by_name["greet"], Tool)
        assert by_name["greet"].parameters["type"] == "object"

        # ...and calling it through the Tool contract reaches the MCP server.
        result = by_name["greet"].run({"name": "ada"})
        assert "hello ada" in result

        # A tool that raises is reported with the "error:" prefix.
        err_result = by_name["boom"].run({})
        assert err_result.startswith("error:")
