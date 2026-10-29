"""Tests for LangGraph messages: subgraph composition, merge by id, loop-breaker."""

from __future__ import annotations

from typing import Any, cast

from langchain_core.messages import convert_to_openai_messages
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.types import interrupt

from vsigma import (
    Agent,
    Interceptor,
    LLMResponse,
    ReActState,
    Tool,
    ToolCall,
    make_guarded_react_agent,
)


def test_subgraph_shares_messages_pauses_and_resumes_cleanly() -> None:
    sub: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)

    def pause_node(state: ReActState) -> dict[str, object]:
        val = interrupt("need_input")
        return {
            "messages": [{"role": "assistant", "content": f"answer={val}"}],
            "status": "done",
        }

    sub.add_node("step", pause_node)
    sub.add_edge(START, "step")

    parent: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    parent.add_node("sub_node", sub.compile())
    parent.add_edge(START, "sub_node")

    agent = Agent(parent.compile(checkpointer=MemorySaver()), Interceptor())
    sess = agent.session("sub-sess")

    run = sess.send("go")
    assert run.interrupted
    run2 = sess.resume("42")
    assert run2.status == "done"

    messages = cast("list[Any]", run2.state.get("messages", []))
    contents = [m["content"] for m in convert_to_openai_messages(messages)]
    assert contents == ["go", "answer=42"]


def test_subgraph_run_straight_through_succeeds() -> None:
    sub: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)

    def sub_node(state: ReActState) -> dict[str, object]:
        return {
            "messages": [{"role": "assistant", "content": "from sub"}],
            "status": "done",
        }

    sub.add_node("step", sub_node)
    sub.add_edge(START, "step")

    parent: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    parent.add_node("sub_node", sub.compile())
    parent.add_edge(START, "sub_node")

    agent = Agent(parent.compile(checkpointer=MemorySaver()), Interceptor())
    run = agent.run("go")
    assert run.status == "done"

    messages = cast("list[Any]", run.state.get("messages", []))
    contents = [getattr(m, "content", "") for m in messages]
    assert contents == ["go", "from sub"]


def test_unwrapped_echoing_node_adds_new_once() -> None:
    builder: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)

    def echoing_node(state: ReActState) -> dict[str, object]:
        return {
            "messages": [
                *list(cast("list[Any]", state.get("messages", []))),
                {"role": "assistant", "content": "fresh"},
            ],
            "status": "done",
        }

    builder.add_node("echo", echoing_node)
    builder.add_edge(START, "echo")

    agent = Agent(builder.compile(checkpointer=MemorySaver()), Interceptor())
    run = agent.run("hello")
    assert run.status == "done"

    messages = cast("list[Any]", run.state.get("messages", []))
    contents = [getattr(m, "content", "") for m in messages]
    assert contents == ["hello", "fresh"]


def test_guarded_agent_repeating_failing_tool_call_with_empty_id_escalates() -> None:
    tool = Tool(
        name="fail_tool",
        description="fails",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "failure",
    )

    class RepeatingToolLLM:
        def complete(
            self, messages: list[dict[str, object]], tools: list[dict[str, object]]
        ) -> LLMResponse:
            return LLMResponse(tool_calls=[ToolCall(id="", name="fail_tool", arguments={})])

    agent = make_guarded_react_agent(RepeatingToolLLM(), [tool], limit=2)
    run = agent.run("start")

    assert run.status == "escalated"
    assert run.interrupted
