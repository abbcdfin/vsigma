"""MCP tool adapter test — real adapter, in-memory transport (no network/subprocess)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from typing import Any, cast

import pytest
from mcp import ClientSession

from vsigma import (
    ConnectionFactory,
    FakeLLM,
    Tool,
    call,
    make_guarded_react_agent,
    make_mcp_tools,
    make_plan_execute_agent,
    make_react_agent,
    make_reflexion_agent,
    say,
    tool,
)


def _register_tools(server: Any) -> None:
    @server.tool()  # type: ignore[untyped-decorator]
    def greet(who: str) -> str:
        return f"hello {who}"

    @server.tool()  # type: ignore[untyped-decorator]
    def boom() -> str:
        raise RuntimeError("kaboom")

    @server.tool()  # type: ignore[untyped-decorator]
    async def sleepy(delay: float) -> str:
        await asyncio.sleep(delay)
        return f"slept {delay}"


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
        by_name = {t.name: t for t in toolset.tools}

        # The MCP tool is wrapped as a native vsigma Tool...
        assert "greet" in by_name
        assert isinstance(by_name["greet"], Tool)
        assert by_name["greet"].parameters["type"] == "object"

        # ...and calling it through the Tool contract reaches the MCP server.
        result = by_name["greet"].run({"who": "ada"})
        assert "hello ada" in result

        # A tool that raises is reported with the "error:" prefix.
        err_result = by_name["boom"].run({})
        assert err_result.startswith("error:")


def test_renamed_mcp_tool_resolves_clash_and_calls_server_tool() -> None:
    local_greet = tool(lambda args: f"local {args.get('who')}", name="greet", description="local")
    with make_mcp_tools(_test_connection()) as toolset:
        mcp_greet = next(t for t in toolset.tools if t.name == "greet")

        # Clash refused across all four builders
        llm = FakeLLM([])
        for builder in [
            lambda: make_react_agent(llm, [local_greet, mcp_greet]),
            lambda: make_guarded_react_agent(llm, [local_greet, mcp_greet]),
            lambda: make_plan_execute_agent(llm, [local_greet, mcp_greet]),
            lambda: make_reflexion_agent(llm, [local_greet, mcp_greet], lambda _: None),
        ]:
            with pytest.raises(ValueError, match="greet") as exc_info:
                builder()  # type: ignore[no-untyped-call]
            assert "dataclasses.replace" in str(exc_info.value)

        # Renaming the MCP tool with dataclasses.replace builds and still
        # reaches the server's "greet" tool
        renamed_mcp = replace(mcp_greet, name="mcp_greet")
        agent = make_react_agent(
            FakeLLM([call("mcp_greet", who="grace"), say("done")]),
            [local_greet, renamed_mcp],
        )
        run = agent.run("say hi")
        assert run.result == "done"
        msgs = cast("list[Any]", run.state["messages"])
        assert any(getattr(m, "content", None) == "hello grace" for m in msgs)
        assert renamed_mcp.run({"who": "hopper"}) == "hello hopper"


def test_mcp_tool_timeout_recovers_and_timeout_none_waits() -> None:
    # 1. With a short timeout, a sleeping tool returns the timeout error, the turn
    # finishes, the session isn't busy afterwards, and the next call succeeds.
    with make_mcp_tools(_test_connection(), timeout=0.05) as toolset:
        by_name = {t.name: t for t in toolset.tools}
        sleepy_tool = by_name["sleepy"]
        greet_tool = by_name["greet"]

        direct_err = sleepy_tool.run({"delay": 0.5})
        assert direct_err == "error: MCP tool 'sleepy' timed out after 0.05s"

        agent = make_react_agent(
            FakeLLM(
                [
                    call("sleepy", delay=0.5),
                    say("recovered from timeout"),
                    call("greet", who="linus"),
                    say("greeted linus"),
                ]
            ),
            [sleepy_tool, greet_tool],
        )
        sess = agent.session("s_mcp_timeout")
        r1 = sess.send("run slow tool")
        assert r1.result == "recovered from timeout"
        msgs = cast("list[Any]", r1.state["messages"])
        assert any(
            getattr(m, "content", None) == "error: MCP tool 'sleepy' timed out after 0.05s"
            for m in msgs
        )

        # Session is not busy afterwards, and the next call on the same toolset succeeds
        r2 = sess.send("now greet")
        assert r2.result == "greeted linus"
        assert greet_tool.run({"who": "ken"}) == "hello ken"

    # 2. With timeout=None, the call waits for completion
    with make_mcp_tools(_test_connection(), timeout=None) as toolset_no_limit:
        by_name_no_limit = {t.name: t for t in toolset_no_limit.tools}
        assert by_name_no_limit["sleepy"].run({"delay": 0.05}) == "slept 0.05"
