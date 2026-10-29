"""Plan-and-Solve loop primitive.

An anticipatory node that breaks the goal into a deterministic plan *before* tools
are invoked, reducing drift. Wire it ahead of the executing loop (e.g.
``plan -> act``); the plan is recorded in state and surfaced into the context.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from langchain_core.messages import convert_to_openai_messages

from .llm import LLM
from .state import NodeFn, State

PLAN_INSTRUCTION = (
    "Before acting, break this task into a short numbered plan of concrete steps. "
    "Output only the plan."
)


def plan_node(llm: LLM, *, instruction: str = PLAN_INSTRUCTION) -> NodeFn:
    def node(state: State) -> State:
        raw_messages = cast("Sequence[Any]", state.get("messages", []))
        converted = cast("list[dict[str, object]]", convert_to_openai_messages(raw_messages))
        system_prompt = state.get("system")
        llm_messages: list[dict[str, object]] = (
            [{"role": "system", "content": str(system_prompt)}, *converted]
            if system_prompt
            else list(converted)
        )
        user_instruction: dict[str, object] = {"role": "user", "content": instruction}
        response = llm.complete(
            [*llm_messages, user_instruction], []
        )
        plan = response.text
        return {
            "messages": [{"role": "assistant", "content": f"Plan:\n{plan}"}],
            "plan": plan,
            "status": "running",
        }

    return node
