"""Tool definitions.

A `Tool` is the only bridge between an agent's reasoning and the outside world.
`spec` renders the OpenAI-compatible function schema sent to the LLM.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast, overload

#: A tool implementation: receives parsed arguments, returns an observation.
ToolFunc = Callable[[dict[str, object]], str]


def normalize_tool_schema(schema: dict[str, object]) -> dict[str, object]:
    """Normalize a tool parameter schema by hoisting nested `$defs`/`definitions` to root.

    Servers like llama.cpp resolve ``#/$defs/...`` references against the parameters root
    schema. Pydantic submodels nested under a property place their ``$defs`` inside that
    property, causing resolution 400 errors. Hoisting all definitions to the parameters
    root ensures clean resolution across all local and remote endpoints.
    """
    if not isinstance(schema, dict):
        return schema

    import copy

    sc = copy.deepcopy(schema)
    hoisted: dict[str, object] = {}

    def _traverse_and_hoist(node: object) -> object:
        if isinstance(node, dict):
            for def_key in ("$defs", "definitions"):
                if def_key in node and isinstance(node[def_key], dict):
                    hoisted.update(cast("dict[str, object]", node[def_key]))
                    del node[def_key]
            return {k: _traverse_and_hoist(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_traverse_and_hoist(item) for item in node]
        return node

    cleaned = cast("dict[str, object]", _traverse_and_hoist(sc))
    if hoisted:
        existing_defs = cast("dict[str, object]", cleaned.get("$defs", {}))
        hoisted.update(existing_defs)
        cleaned["$defs"] = hoisted
    return cleaned


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, object]
    func: ToolFunc
    return_direct: bool = False

    def run(self, arguments: dict[str, object]) -> str:
        return self.func(arguments)

    @property
    def spec(self) -> dict[str, object]:
        """OpenAI-compatible tool schema with normalized root $defs."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": normalize_tool_schema(self.parameters),
            },
        }


@overload
def tool(
    func: ToolFunc,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: dict[str, object] | None = None,
    return_direct: bool = False,
) -> Tool: ...


@overload
def tool(
    func: None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: dict[str, object] | None = None,
    return_direct: bool = False,
) -> Callable[[ToolFunc], Tool]: ...


def tool(
    func: ToolFunc | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: dict[str, object] | None = None,
    return_direct: bool = False,
) -> Tool | Callable[[ToolFunc], Tool]:
    """Build a `Tool` from a plain ``(args) -> str`` function.

    Usable as ``@tool``, ``@tool(name=...)``, or ``tool(fn)``. Name defaults to the
    function name, description to its docstring, parameters to an empty object schema.
    """

    def make(f: ToolFunc) -> Tool:
        resolved_name = name or cast("str", getattr(f, "__name__", "tool"))
        resolved_desc = description or (cast("str", getattr(f, "__doc__", "")) or "").strip()
        schema: dict[str, object] = (
            parameters if parameters is not None else {"type": "object", "properties": {}}
        )
        return Tool(
            name=resolved_name,
            description=resolved_desc,
            parameters=schema,
            func=f,
            return_direct=return_direct,
        )

    return make(func) if func is not None else make
