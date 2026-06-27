"""Plan-and-Solve loop primitive.

An anticipatory node that breaks the goal into a deterministic plan *before* tools
are invoked, reducing drift. Wire it ahead of the executing loop (e.g.
``plan -> act``); the plan is recorded in state and surfaced into the context.
"""

from __future__ import annotations

from typing import cast

from .llm import LLM
from .state import NodeFn, State

PLAN_INSTRUCTION = (
    "Before acting, break this task into a short numbered plan of concrete steps. "
    "Output only the plan."
)


def plan_node(llm: LLM, *, instruction: str = PLAN_INSTRUCTION) -> NodeFn:
    def node(state: State) -> State:
        messages = list(cast("list[dict[str, object]]", state.get("messages", [])))
        response = llm.complete(messages + [{"role": "user", "content": instruction}], [])
        plan = response.text
        messages.append({"role": "assistant", "content": f"Plan:\n{plan}"})
        return {"messages": messages, "plan": plan, "status": "running"}

    return node
