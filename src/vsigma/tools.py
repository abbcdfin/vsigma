"""Tool definitions.

A `Tool` is the only bridge between an agent's reasoning and the outside world.
`spec` renders the OpenAI-compatible function schema sent to the LLM.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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
