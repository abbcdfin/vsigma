"""End-to-end tests for the assembled agent: composition, escalation, resume."""

from vsigma import (
    FakeLLM,
    GraphBuilder,
    InMemoryStore,
    LLMResponse,
    ReActState,
    ToolCall,
    TrajectoryLog,
    break_on_attempts,
    human_interrupt_node,
    react_node,
)
from vsigma.builder import Agent
from vsigma.demo import build_browsing_agent, run_demo
from vsigma.tools import Tool

_NOOP = Tool(
    name="noop",
    description="noop",
    parameters={"type": "object", "properties": {}},
    func=lambda args: "ok",
)


def _looping_agent(limit: int) -> Agent:
    # Two tool calls (never finishing) force escalation; the third response,
    # delivered after resume, finishes the task.
    llm = FakeLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="1", name="noop", arguments={})]),
            LLMResponse(tool_calls=[ToolCall(id="2", name="noop", arguments={})]),
            LLMResponse(text="finished after human nudge"),
        ]
    )
    builder = GraphBuilder(ReActState)
    builder.add("act", react_node(llm, [_NOOP]))
    builder.add("human_interrupt", human_interrupt_node)
    builder.set_entry("act")
    builder.route_conditional(
        "act",
        break_on_attempts(retry="act", escalate="human_interrupt", limit=limit, done="__end__"),
    )
    builder.add_edge("human_interrupt", "act")
    return builder.compile(memory=InMemoryStore(), observer=TrajectoryLog())


def test_loop_breaker_escalates_then_resumes_to_done() -> None:
    agent = _looping_agent(limit=2)
    run = agent.run("do the thing")

    assert run.status == "escalated"
    assert run.interrupted

    run.resume("just answer now")

    assert run.status == "done"
    assert run.result == "finished after human nudge"


def test_run_records_trajectory_and_memory() -> None:
    agent = _looping_agent(limit=2)
    run = agent.run("do the thing", session_id="s1")

    # At least one trajectory event for this run, all correctly keyed.
    assert run.trajectory
    assert all(event.run_id == run.run_id for event in run.trajectory)
    # The wrapper seam wrote node updates through the memory interface.
    assert agent.memory.query("s1")


def test_browsing_demo_runs_offline() -> None:
    from vsigma.demo import _demo_fake_llm, _fake_fetch

    agent = build_browsing_agent(_demo_fake_llm(), fetch=_fake_fetch)
    run = agent.run("title?", system="browse", session_id="demo")

    assert run.status == "done"
    assert run.result is not None
    assert "Hello World" in run.result


def test_run_demo_smoke() -> None:
    # Uses the offline fake LLM + canned pages by default.
    agent = run_demo()
    assert agent.memory.query("demo-session")
