"""End-to-end tests: templates, the raw-StateGraph seam, escalation, resume."""

from typing import Any

from examples.web_browsing.demo import build_browsing_agent, run_demo
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph

from vsigma import (
    HUMAN_INTERRUPT,
    Agent,
    FakeLLM,
    InMemoryStore,
    ReActState,
    Services,
    TrajectoryLog,
    add_router,
    add_wrapped,
    break_on_attempts,
    call,
    human_interrupt_node,
    make_react_agent,
    react_node,
    say,
)
from vsigma.tools import Tool

_NOOP = Tool(
    name="noop",
    description="noop",
    parameters={"type": "object", "properties": {}},
    func=lambda args: "ok",
)


def _looping_llm() -> FakeLLM:
    # Two tool calls force escalation at limit=2; the third (after resume) finishes.
    return FakeLLM([call("noop"), call("noop"), say("finished after human nudge")])


def test_template_escalates_then_resumes_to_done() -> None:
    agent = make_react_agent(_looping_llm(), [_NOOP], limit=2)
    run = agent.run("do the thing")

    assert run.status == "escalated"
    assert run.interrupted

    run.resume("just answer now")

    assert run.status == "done"
    assert run.result == "finished after human nudge"


def test_template_records_trajectory_and_memory() -> None:
    agent = make_react_agent(_looping_llm(), [_NOOP], limit=2)
    run = agent.run("do the thing", session_id="s1")

    assert run.trajectory
    assert all(event.run_id == run.run_id for event in run.trajectory)
    assert agent.memory.query("s1")


def test_wrap_seam_works_on_raw_stategraph() -> None:
    # No template — the seam applied directly to a LangGraph StateGraph you own.
    svc = Services(memory=InMemoryStore(), observer=TrajectoryLog())
    sg: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    add_wrapped(sg, "act", react_node(FakeLLM([say("hi")]), [_NOOP]), svc)
    add_wrapped(sg, HUMAN_INTERRUPT, human_interrupt_node, svc)
    sg.add_edge(START, "act")
    add_router(sg, "act", break_on_attempts(retry="act", escalate=HUMAN_INTERRUPT, limit=3))
    sg.add_edge(HUMAN_INTERRUPT, "act")
    agent = Agent(sg.compile(checkpointer=MemorySaver()), svc)

    run = agent.run("hello")

    assert run.status == "done"
    assert run.result == "hi"
    assert run.trajectory  # seam emitted trajectory on the raw graph
    assert agent.memory.query(run.task_id)  # seam wrote through memory


def test_browsing_demo_runs_offline() -> None:
    from examples.web_browsing.demo import _demo_fake_llm, _fake_fetch

    agent = build_browsing_agent(_demo_fake_llm(), fetch=_fake_fetch)
    run = agent.run("title?", session_id="demo")

    assert run.status == "done"
    assert run.result is not None
    assert "Hello World" in run.result


def test_run_demo_smoke() -> None:
    agent = run_demo()
    assert agent.memory.query("demo-session")
