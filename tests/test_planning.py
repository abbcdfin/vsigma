"""Tests for the Plan-and-Solve loop primitive."""

from vsigma import FakeLLM, LLMResponse, plan_node


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
