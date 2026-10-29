"""Tests for the Plan-and-Solve loop primitive."""

from langchain_core.messages import HumanMessage

from vsigma import FakeLLM, LLMResponse, plan_node
from vsigma.planning import PLAN_INSTRUCTION


def _state(text: str) -> dict[str, object]:
    return {
        "task_id": "t",
        "run_id": "r",
        "attempt_count": 0,
        "status": "running",
        "messages": [{"role": "user", "content": text}],
    }


def test_plan_node_records_and_surfaces_plan() -> None:
    node = plan_node(FakeLLM([LLMResponse(text="1. fetch page\n2. read title")]))

    update = node(_state("find the title"))

    assert update["status"] == "running"
    assert update["plan"] == "1. fetch page\n2. read title"
    messages = update["messages"]
    assert isinstance(messages, list)
    assert any("Plan:" in str(m.get("content", "")) for m in messages)


def test_plan_node_converts_messages_and_prepends_system() -> None:
    seen: list[list[dict[str, object]]] = []

    class _RecordingLLM:
        def complete(
            self, messages: list[dict[str, object]], tools: list[dict[str, object]]
        ) -> LLMResponse:
            seen.append(messages)
            return LLMResponse(text="1. step one\n2. step two")

    node = plan_node(_RecordingLLM())
    state: dict[str, object] = {
        "messages": [HumanMessage(content="solve this")],
        "system": "you are an expert planner",
        "attempt_count": 0,
    }
    node(state)

    assert len(seen) == 1
    prompt = seen[0]
    assert prompt == [
        {"role": "system", "content": "you are an expert planner"},
        {"role": "user", "content": "solve this"},
        {"role": "user", "content": PLAN_INSTRUCTION},
    ]
    for msg in prompt:
        assert isinstance(msg, dict)
