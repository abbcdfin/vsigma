"""MCP tool adapter test — real adapter, in-memory transport (no network/subprocess)."""

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from vsigma import Tool, make_mcp_tools


def _test_server() -> FastMCP:
    server = FastMCP("test")

    @server.tool()
    def greet(name: str) -> str:
        return f"hello {name}"

    return server


def test_make_mcp_tools_wraps_and_invokes() -> None:
    server = _test_server()
    with make_mcp_tools(lambda: create_connected_server_and_client_session(server)) as toolset:
        by_name = {tool.name: tool for tool in toolset.tools}

        # The MCP tool is wrapped as a native vsigma Tool...
        assert "greet" in by_name
        assert isinstance(by_name["greet"], Tool)
        assert by_name["greet"].parameters["type"] == "object"

        # ...and calling it through the Tool contract reaches the MCP server.
        result = by_name["greet"].run({"name": "ada"})
        assert "hello ada" in result
