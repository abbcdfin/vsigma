"""Tests for the effort-estimation conditional edge."""

from langchain_core.messages import HumanMessage

from vsigma import FakeLLM, LLMResponse, estimate_effort


def _state(text: str) -> dict[str, object]:
    return {
        "task_id": "t",
        "run_id": "r",
        "attempt_count": 0,
        "status": "running",
        "messages": [HumanMessage(content=text)],
    }


def test_routes_simple_to_cheap_path() -> None:
    router = estimate_effort(FakeLLM([LLMResponse(text="simple")]), cheap="fast", heavy="plan")
    assert router.fn(_state("add 1 + 1")) == "simple"
    assert router.mapping == {"simple": "fast", "complex": "plan"}


def test_routes_complex_to_heavy_path() -> None:
    router = estimate_effort(
        FakeLLM([LLMResponse(text="complex, many steps")]), cheap="fast", heavy="plan"
    )
    assert router.fn(_state("build a web app")) == "complex"


def test_defaults_to_complex_when_unclear() -> None:
    router = estimate_effort(FakeLLM([LLMResponse(text="not sure")]), cheap="fast", heavy="plan")
    assert router.fn(_state("???")) == "complex"
