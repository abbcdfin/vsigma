"""The ReAct loop primitive.

One `react_node` call is one ReAct step: ask the LLM, and either execute the
tool calls it requested (and loop) or record its final answer (and finish). The
loop itself is formed by a conditional edge (see `resilience.break_on_attempts`).
"""

from __future__ import annotations

import json
from typing import cast

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
        messages = list(cast("list[dict[str, object]]", state.get("messages", [])))
        attempt = cast(int, state.get("attempt_count", 0))
        response = llm.complete(messages, specs)

        if response.tool_calls:
            messages.append(
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

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": observation,
                    }
                )

            if terminal_result is not None:
                return {
                    "messages": messages,
                    "result": terminal_result,
                    "attempt_count": attempt + 1,
                    "status": "done",
                }
            return {"messages": messages, "attempt_count": attempt + 1, "status": "running"}

        messages.append({"role": "assistant", "content": response.text})
        return {
            "messages": messages,
            "result": response.text,
            "attempt_count": attempt + 1,
            "status": "done",
        }

    return node
