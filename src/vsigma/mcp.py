"""MCP tool adapter — source tools from an MCP server through the `Tool` contract.

`Tool` is the uniform in-process contract the engine runs against; MCP is one
*source* behind it. This adapter connects to an MCP server, lists its tools, and
wraps each as a `Tool` whose `func` performs the call — so the engine never
learns about MCP (the same adapter pattern as the memory layer).

The MCP SDK is async while the engine is sync, so calls are bridged onto a
dedicated background event loop; `Tool.func` stays an ordinary synchronous call.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from concurrent.futures import Future
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import TracebackType
from typing import cast

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPTool

from .bridge import AsyncBridge
from .tools import Tool

#: Yields an *initialized* `ClientSession`. Abstracts the transport (stdio, HTTP,
#: in-memory) so the adapter and its tests share one code path.
ConnectionFactory = Callable[[], AbstractAsyncContextManager[ClientSession]]


def _result_to_text(result: CallToolResult) -> str:
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
        else:
            parts.append(str(block))
    text = "\n".join(parts)
    return f"error: {text}" if result.isError else text


class MCPToolset:
    """Live handle over an MCP connection. Exposes `tools`; `close()` tears down.

    Usable as a context manager: ``with make_mcp_tools(factory) as ts: ...``.
    """

    def __init__(
        self,
        bridge: AsyncBridge,
        stop: asyncio.Event,
        serve: Future[None],
        tools: list[Tool],
    ) -> None:
        self._bridge = bridge
        self._stop = stop
        self._serve = serve
        self.tools = tools

    def close(self) -> None:
        self._bridge.call_soon(self._stop.set)
        try:
            self._serve.result(timeout=5.0)
        except Exception:
            pass
        self._bridge.shutdown()

    def __enter__(self) -> MCPToolset:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


async def _new_event() -> asyncio.Event:
    return asyncio.Event()


def _wrap(bridge: AsyncBridge, session: ClientSession, mcp_tool: MCPTool) -> Tool:
    name = mcp_tool.name

    def func(arguments: dict[str, object]) -> str:
        result = bridge.run(session.call_tool(name, arguments))
        return _result_to_text(result)

    return Tool(
        name=name,
        description=mcp_tool.description or "",
        parameters=cast("dict[str, object]", mcp_tool.inputSchema),
        func=func,
    )


def make_mcp_tools(connect: ConnectionFactory) -> MCPToolset:
    """Connect to an MCP server and wrap its tools as `Tool`s.

    `connect` yields an initialized `ClientSession` (see `stdio_connection` or the
    in-memory transport helper). The connection is held open until `close()`.
    """
    bridge = AsyncBridge()
    stop = bridge.run(_new_event())
    ready: Future[tuple[ClientSession, list[MCPTool]]] = Future()

    async def serve() -> None:
        try:
            async with connect() as session:
                listed = await session.list_tools()
                ready.set_result((session, listed.tools))
                await stop.wait()
        except Exception as exc:  # surface setup failures to the caller
            if not ready.done():
                ready.set_exception(exc)
            raise

    serve_future = bridge.submit(serve())
    try:
        session, mcp_tools = ready.result(timeout=30.0)
    except Exception:
        bridge.shutdown()
        raise
    tools = [_wrap(bridge, session, mcp_tool) for mcp_tool in mcp_tools]
    return MCPToolset(bridge, stop, serve_future, tools)


def stdio_connection(
    command: str, *, args: list[str] | None = None, env: dict[str, str] | None = None
) -> ConnectionFactory:
    """A `ConnectionFactory` for a stdio MCP server (launched as a subprocess)."""
    params = StdioServerParameters(command=command, args=args or [], env=env)

    @asynccontextmanager
    async def factory() -> AsyncGenerator[ClientSession, None]:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            yield session

    return factory
