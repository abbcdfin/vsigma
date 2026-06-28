"""Tool definitions.

A `Tool` is the only bridge between an agent's reasoning and the outside world.
`spec` renders the OpenAI-compatible function schema sent to the LLM.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

#: A tool implementation: receives parsed arguments, returns an observation.
ToolFunc = Callable[[dict[str, object]], str]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, object]
    func: ToolFunc

    def run(self, arguments: dict[str, object]) -> str:
        return self.func(arguments)

    @property
    def spec(self) -> dict[str, object]:
        """OpenAI-compatible tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def tool(
    func: ToolFunc | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: dict[str, object] | None = None,
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
        return Tool(name=resolved_name, description=resolved_desc, parameters=schema, func=f)

    return make(func) if func is not None else make
