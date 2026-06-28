"""Tests for the offline LLM test helpers: call/say + StaticLLM."""

from vsigma import FakeLLM, StaticLLM, call, react_node, say
from vsigma.tools import Tool

_NOOP = Tool(
    name="noop",
    description="noop",
    parameters={"type": "object", "properties": {}},
    func=lambda args: "ok",
)


def test_say_builds_final_answer() -> None:
    response = say("hello")
    assert response.text == "hello"
    assert response.tool_calls == []


def test_call_builds_tool_call_with_auto_id() -> None:
    response = call("noop", x=1)
    assert response.text == ""
    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.name == "noop"
    assert tc.arguments == {"x": 1}
    assert tc.id  # auto-generated, non-empty


def test_fakellm_with_terse_script() -> None:
    llm = FakeLLM([call("noop"), say("done!")])
    node = react_node(llm, [_NOOP])
    state: dict[str, object] = {
        "task_id": "t", "run_id": "r", "attempt_count": 0,
        "status": "running", "messages": [{"role": "user", "content": "go"}],
    }
    after_call = node(state)
    assert after_call["status"] == "running"
    after_say = node({**state, "messages": after_call["messages"], "attempt_count": 1})
    assert after_say["status"] == "done"
    assert after_say["result"] == "done!"


def test_static_llm_always_finishes() -> None:
    llm = StaticLLM("fixed answer")
    assert llm.complete([], []).text == "fixed answer"
    assert llm.complete([{"role": "user", "content": "anything"}], []).text == "fixed answer"
