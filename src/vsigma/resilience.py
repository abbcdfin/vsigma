"""Resilience — loop-breakers and the human-interrupt escalation path.

`break_on_attempts` is a conditional-edge router: finish when done, escalate to a
human-interrupt node when the attempt budget is spent, otherwise retry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from langgraph.graph import END
from langgraph.types import interrupt

from .state import State

#: Conventional node name for the human-interrupt escalation node.
HUMAN_INTERRUPT = "human_interrupt"


@dataclass(frozen=True)
class Router:
    """A conditional edge: a routing function plus its branch mapping."""

    fn: Callable[[State], str]
    mapping: dict[str, str]


def break_on_attempts(
    *, retry: str, escalate: str, limit: int, done: str = END
) -> Router:
    def route(state: State) -> str:
        if state.get("status") == "done":
            return "done"
        if cast(int, state.get("attempt_count", 0)) >= limit:
            return "escalate"
        return "retry"

    return Router(fn=route, mapping={"done": done, "retry": retry, "escalate": escalate})


def human_interrupt_node(state: State) -> State:
    """Pause the run for human input, then resume with the feedback injected.

    On resume the attempt budget is reset so the agent can continue.
    """
    feedback = interrupt(
        {"reason": "attempt limit reached", "task_id": state.get("task_id")}
    )
    messages = list(cast("list[dict[str, object]]", state.get("messages", [])))
    messages.append({"role": "user", "content": str(feedback)})
    return {"messages": messages, "attempt_count": 0, "status": "running"}
