"""Tests for the ReAct loop primitive and the resilience router."""

from vsigma import (
    FakeLLM,
    LLMResponse,
    ToolCall,
    break_on_attempts,
    make_react_agent,
    react_node,
)
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


def test_react_malformed_json_tool_call_replays_valid_json_arguments() -> None:
    malformed_call = ToolCall(
        id="1",
        name="echo",
        arguments={},
        raw_arguments='{"x": 1, "y":',
        error=(
            "error: Malformed tool arguments JSON. "
            'Raw argument string emitted: {"x": 1, "y":'
        ),
    )
    llm = FakeLLM([LLMResponse(tool_calls=[malformed_call])])
    node = react_node(llm, [_ECHO])

    update = node(_state([{"role": "user", "content": "hi"}]))

    assert update["status"] == "running"
    messages = update["messages"]
    assert isinstance(messages, list)

    assistant_msg = messages[-2]
    assert isinstance(assistant_msg, dict)
    assert assistant_msg["role"] == "assistant"
    tool_calls = assistant_msg["tool_calls"]
    assert isinstance(tool_calls, list)
    assert tool_calls[0]["function"]["arguments"] == "{}"

    tool_msg = messages[-1]
    assert isinstance(tool_msg, dict)
    assert tool_msg["role"] == "tool"
    assert "error: Malformed tool arguments JSON" in str(tool_msg["content"])
    assert '{"x": 1, "y":' in str(tool_msg["content"])


def test_break_on_attempts_routes() -> None:
    router = break_on_attempts(retry="act", escalate="human", limit=3, done="__end__")

    assert router.fn({"status": "done", "attempt_count": 1}) == "done"
    assert router.fn({"status": "running", "attempt_count": 3}) == "escalate"
    assert router.fn({"status": "running", "attempt_count": 1}) == "retry"
    assert router.mapping == {"done": "__end__", "retry": "act", "escalate": "human"}


def test_react_node_sends_plain_openai_dicts_to_llm() -> None:
    seen: list[list[dict[str, object]]] = []

    class _RecordingLLM:
        def complete(
            self, messages: list[dict[str, object]], tools: list[dict[str, object]]
        ) -> LLMResponse:
            seen.append(messages)
            return LLMResponse(text="done")

    node = react_node(_RecordingLLM(), [_ECHO])
    state: dict[str, object] = {
        "messages": [{"role": "user", "content": "hello"}],
        "system": "you are helpful",
        "attempt_count": 0,
    }
    node(state)

    assert len(seen) == 1
    prompt = seen[0]
    assert prompt == [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hello"},
    ]
    for msg in prompt:
        assert isinstance(msg, dict)
        assert "role" in msg
        assert "content" in msg


def test_react_agent_converts_tool_call_turns_to_openai_messages() -> None:
    seen: list[list[dict[str, object]]] = []

    class _RecordingLLM:
        def __init__(self) -> None:
            self.calls = 0

        def complete(
            self, messages: list[dict[str, object]], tools: list[dict[str, object]]
        ) -> LLMResponse:
            seen.append(messages)
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    tool_calls=[
                        ToolCall(id="", name="echo", arguments={"x": 1}),
                        ToolCall(id="c2", name="echo", arguments={"x": 2}),
                    ]
                )
            return LLMResponse(text="done")

    tool = Tool(
        name="echo",
        description="echo",
        parameters={"type": "object", "properties": {}},
        func=lambda args: f"got {args}",
    )
    agent = make_react_agent(_RecordingLLM(), [tool], system="sys")
    run = agent.run("go")
    assert run.status == "done"

    assert len(seen) == 2
    assert seen[1] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"x": 1}'},
                },
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": "echo", "arguments": '{"x": 2}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "", "content": "got {'x': 1}"},
        {"role": "tool", "tool_call_id": "c2", "content": "got {'x': 2}"},
    ]

