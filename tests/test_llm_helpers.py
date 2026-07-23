from typing import Any, cast

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


def test_openai_compatible_llm_temperature_and_tool_choice(monkeypatch: object) -> None:
    from vsigma import OpenAICompatibleLLM, default_llm

    llm = OpenAICompatibleLLM(
        model="m",
        base_url="http://localhost:8000",
        api_key="k",
        temperature=0.7,
        tool_choice="required",
    )
    assert llm.temperature == 0.7
    assert llm.tool_choice == "required"

    mp = cast("Any", monkeypatch)
    mp.setenv("VSIGMA_LLM_BASE_URL", "http://localhost:8000")
    mp.setenv("VSIGMA_LLM_API_KEY", "key")
    mp.setenv("VSIGMA_LLM_TEMPERATURE", "0.2")
    mp.setenv("VSIGMA_LLM_TOOL_CHOICE", "required")

    parsed = default_llm()
    assert isinstance(parsed, OpenAICompatibleLLM)
    assert parsed.temperature == 0.2
    assert parsed.tool_choice == "required"
