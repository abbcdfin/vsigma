"""Dynamic effort estimation as a conditional edge.

A fast, structured classification of task complexity that branches the graph to
a cheap path or a heavy path — kept in a conditional edge rather than a heavy
node (per the architecture guidelines). This is the Phase-2 binary form
(cheap vs. heavy); richer rubrics/anchoring can extend it later.
"""

from __future__ import annotations

from typing import cast

from langchain_core.messages import AnyMessage

from .llm import LLM
from .routing import Router
from .state import State

DEFAULT_RUBRIC = (
    "Classify the user's task by effort. Reply with exactly one word: "
    "'simple' for a short, single-step request, or 'complex' for a multi-step task "
    "that needs planning."
)


def _latest_user_text(state: State) -> str:
    messages = cast("list[AnyMessage]", state.get("messages", []))
    for message in reversed(messages):
        if message.type == "human":
            return str(message.text)
    return ""


def estimate_effort(
    llm: LLM, *, cheap: str, heavy: str, rubric: str = DEFAULT_RUBRIC
) -> Router:
    """Route to `cheap` for simple tasks, else `heavy`. Defaults to heavy on doubt."""

    def route(state: State) -> str:
        response = llm.complete(
            [
                {"role": "system", "content": rubric},
                {"role": "user", "content": _latest_user_text(state)},
            ],
            [],
        )
        return "simple" if "simple" in response.text.strip().lower() else "complex"

    return Router(fn=route, mapping={"simple": cheap, "complex": heavy})
