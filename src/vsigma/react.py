"""The ReAct loop primitive.

One `react_node` call is one ReAct step: ask the LLM, and either execute the
tool calls it requested (and loop) or record its final answer (and finish). The
loop itself is formed by a conditional edge (see `resilience.break_on_attempts`).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

from langchain_core.messages import convert_to_openai_messages

from .llm import LLM
from .state import NodeFn, State
from .tools import Tool


def react_node(llm: LLM, tools: list[Tool]) -> NodeFn:
    for candidate in tools:
        if not isinstance(candidate, Tool):
            raise TypeError(
                f"react_node expects vsigma Tool objects; got {type(candidate).__name__}. "
                "Adapt it first: tool(fn) for plain functions, from_langchain(t) for "
                "LangChain tools, or make_mcp_tools(...) for MCP servers."
            )
    tool_map = {tool.name: tool for tool in tools}
    specs = [tool.spec for tool in tools]

    def node(state: State) -> State:
        raw_messages = cast("Sequence[Any]", state.get("messages", []))
        converted = cast("list[dict[str, object]]", convert_to_openai_messages(raw_messages))
        attempt = cast(int, state.get("attempt_count", 0))
        system_prompt = state.get("system")
        llm_messages: list[dict[str, object]] = (
            [{"role": "system", "content": str(system_prompt)}, *converted]
            if system_prompt
            else converted
        )
        response = llm.complete(llm_messages, specs)
        new_messages: list[dict[str, object]] = []

        if response.tool_calls:
            new_messages.append(
                {
                    "role": "assistant",
                    "content": response.text,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in response.tool_calls
                    ],
                }
            )
            terminal_result: str | None = None
            for call in response.tool_calls:
                tool = tool_map.get(call.name)
                is_error = False
                if call.error:
                    observation = call.error
                    is_error = True
                elif tool is None:
                    observation = f"error: unknown tool '{call.name}'"
                    is_error = True
                else:
                    try:
                        observation = tool.run(call.arguments)
                    except Exception as exc:
                        observation = f"error: tool '{call.name}' raised exception: {exc}"
                        is_error = True

                if (
                    not is_error
                    and tool is not None
                    and tool.return_direct
                    and terminal_result is None
                ):
                    terminal_result = observation

                new_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": observation,
                    }
                )

            if terminal_result is not None:
                return {
                    "messages": new_messages,
                    "result": terminal_result,
                    "attempt_count": attempt + 1,
                    "status": "done",
                }
            return {"messages": new_messages, "attempt_count": attempt + 1, "status": "running"}

        new_messages.append({"role": "assistant", "content": response.text})
        return {
            "messages": new_messages,
            "result": response.text,
            "attempt_count": attempt + 1,
            "status": "done",
        }

    return node
