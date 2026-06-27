"""State schema for vsigma agents.

`BaseState` carries identifiers and control fields only — never payloads (those
live behind `memory_ref`, see the memory store). `ReActState` adds the working
fields a tool-calling loop needs.

At runtime LangGraph hands nodes a plain dict, so the framework internals operate
on the `State` alias; the TypedDicts document the public schema and seed the
initial state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, NotRequired, TypedDict

Status = Literal["running", "done", "escalated"]

#: Runtime view of the graph state (LangGraph passes a plain mapping to nodes).
State = dict[str, object]

#: A graph node: receives the state, returns a partial state update.
NodeFn = Callable[[State], State]


class BaseState(TypedDict):
    """Lean control state shared by every vsigma agent."""

    task_id: str
    run_id: str
    attempt_count: int
    status: Status
    session_id: NotRequired[str | None]
    parent_run_id: NotRequired[str | None]
    memory_ref: NotRequired[str]


class ReActState(BaseState):
    """`BaseState` plus the working fields a ReAct / tool-calling loop needs."""

    messages: list[dict[str, object]]
    result: NotRequired[str]
