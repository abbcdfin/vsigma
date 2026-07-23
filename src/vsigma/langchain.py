"""Adapter: LangChain tools → vsigma `Tool`.

Reuse the LangChain / LangGraph tool ecosystem (`@tool`, `StructuredTool`, `BaseTool`)
without standing up an MCP server — native, in-process, zero extra overhead. `Tool` stays
the uniform contract; this is just another source (like `make_mcp_tools`).

`langchain-core` is imported lazily, so vsigma does not hard-depend on it at import time
(langgraph already provides it for users who have it).
"""

from __future__ import annotations

from typing import Any, cast

from .tools import Tool


def from_langchain(lc_tool: object) -> Tool:
    """Adapt a single LangChain `BaseTool` / `StructuredTool` into a vsigma `Tool`."""
    from langchain_core.utils.function_calling import convert_to_openai_tool

    spec = convert_to_openai_tool(cast("Any", lc_tool))["function"]
    parameters = cast(
        "dict[str, object]", spec.get("parameters", {"type": "object", "properties": {}})
    )

    def func(arguments: dict[str, object]) -> str:
        result = cast("Any", lc_tool).invoke(arguments)
        return result if isinstance(result, str) else str(result)

    return Tool(
        name=str(spec["name"]),
        description=str(spec.get("description", "")),
        parameters=parameters,
        func=func,
        return_direct=bool(getattr(lc_tool, "return_direct", False)),
    )


def from_langchain_tools(lc_tools: list[object]) -> list[Tool]:
    """Adapt a list of LangChain tools into vsigma `Tool`s."""
    return [from_langchain(t) for t in lc_tools]
