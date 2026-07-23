"""Tests for the ReAct loop primitive and the resilience router."""

from vsigma import FakeLLM, LLMResponse, ToolCall, break_on_attempts, react_node
from vsigma.tools import Tool

_ECHO = Tool(
    name="echo",
    description="echo",
    parameters={"type": "object", "properties": {}},
    func=lambda args: f"echoed {args.get('x')}",
)


def _state(messages: list[dict[str, object]], attempt: int = 0) -> dict[str, object]:
    return {
        "task_id": "t",
        "run_id": "r",
        "attempt_count": attempt,
        "status": "running",
        "messages": messages,
    }


def test_react_executes_tool_then_continues() -> None:
    llm = FakeLLM([LLMResponse(tool_calls=[ToolCall(id="1", name="echo", arguments={"x": 5})])])
    node = react_node(llm, [_ECHO])

    update = node(_state([{"role": "user", "content": "hi"}]))

    assert update["status"] == "running"
    assert update["attempt_count"] == 1
    messages = update["messages"]
    assert isinstance(messages, list)
    assert messages[-1] == {"role": "tool", "tool_call_id": "1", "content": "echoed 5"}


def test_react_records_final_answer() -> None:
    llm = FakeLLM([LLMResponse(text="done!")])
    node = react_node(llm, [_ECHO])

    update = node(_state([{"role": "user", "content": "hi"}]))

    assert update["status"] == "done"
    assert update["result"] == "done!"


def test_react_catches_tool_exception() -> None:
    def _buggy(args: object) -> str:
        raise ValueError("something went wrong")

    buggy_tool = Tool(
        name="buggy",
        description="buggy tool",
        parameters={"type": "object", "properties": {}},
        func=_buggy,
    )
    llm = FakeLLM([LLMResponse(tool_calls=[ToolCall(id="1", name="buggy", arguments={})])])
    node = react_node(llm, [buggy_tool])

    update = node(_state([{"role": "user", "content": "hi"}]))

    assert update["status"] == "running"
    messages = update["messages"]
    assert isinstance(messages, list)
    assert "error: tool 'buggy' raised exception: something went wrong" in str(
        messages[-1]["content"]
    )


def test_react_return_direct_tool_finishes_run() -> None:
    record_tool = Tool(
        name="record_plan",
        description="record plan",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "plan recorded successfully",
        return_direct=True,
    )
    llm = FakeLLM(
        [LLMResponse(tool_calls=[ToolCall(id="1", name="record_plan", arguments={})])]
    )
    node = react_node(llm, [record_tool])

    update = node(_state([{"role": "user", "content": "hi"}]))

    assert update["status"] == "done"
    assert update["result"] == "plan recorded successfully"


def test_react_return_direct_tool_with_error_does_not_finish() -> None:
    def _failing_record(args: object) -> str:
        raise RuntimeError("database write failed")

    failing_tool = Tool(
        name="record_plan",
        description="record plan",
        parameters={"type": "object", "properties": {}},
        func=_failing_record,
        return_direct=True,
    )
    llm = FakeLLM(
        [LLMResponse(tool_calls=[ToolCall(id="1", name="record_plan", arguments={})])]
    )
    node = react_node(llm, [failing_tool])

    update = node(_state([{"role": "user", "content": "hi"}]))

    assert update["status"] == "running"
    messages = update["messages"]
    assert isinstance(messages, list)
    assert "error: tool 'record_plan' raised exception: database write failed" in str(
        messages[-1]["content"]
    )


def test_break_on_attempts_routes() -> None:
    router = break_on_attempts(retry="act", escalate="human", limit=3, done="__end__")

    assert router.fn({"status": "done", "attempt_count": 1}) == "done"
    assert router.fn({"status": "running", "attempt_count": 3}) == "escalate"
    assert router.fn({"status": "running", "attempt_count": 1}) == "retry"
    assert router.mapping == {"done": "__end__", "retry": "act", "escalate": "human"}
