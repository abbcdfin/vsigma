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
    episode,
    human_interrupt_node,
    make_guarded_react_agent,
    make_react_agent,
    react_node,
    recall,
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


class _AlwaysCallLLM:
    """Never finishes — always asks for the noop tool, so the loop runs until the
    loop-breaker escalates. Used to prove the recursion backstop scales with `limit`."""

    def complete(
        self, messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> Any:
        return call("noop")


def test_flat_react_agent_runs_to_done() -> None:
    # The baseline flat loop: tool call, then finish — no human-interrupt node.
    agent = make_react_agent(FakeLLM([call("noop"), say("all done")]), [_NOOP])
    run = agent.run("do the thing")

    assert run.status == "done"
    assert run.result == "all done"


def test_guarded_template_escalates_then_resumes_to_done() -> None:
    agent = make_guarded_react_agent(_looping_llm(), [_NOOP], limit=2)
    run = agent.run("do the thing")

    assert run.status == "escalated"
    assert run.interrupted

    run.resume("just answer now")

    assert run.status == "done"
    assert run.result == "finished after human nudge"


def test_high_limit_escalates_cleanly_not_recursion_error() -> None:
    # A `limit` well above LangGraph's old default backstop (25). The template now
    # couples recursion_limit to limit, so the loop-breaker escalates cleanly
    # instead of raising GraphRecursionError first.
    agent = make_guarded_react_agent(_AlwaysCallLLM(), [_NOOP], limit=40)
    run = agent.run("do the thing")

    assert run.status == "escalated"
    assert run.interrupted


def test_recursion_limit_override_is_respected() -> None:
    # An explicit override wins over the limit-coupled default.
    agent = make_guarded_react_agent(FakeLLM([]), [_NOOP], limit=2, recursion_limit=99)
    assert agent.recursion_limit == 99


def test_flat_react_agent_recursion_limit_is_tunable() -> None:
    agent = make_react_agent(FakeLLM([]), [_NOOP], recursion_limit=7)
    assert agent.recursion_limit == 7


def test_custom_checkpointer_is_respected() -> None:
    custom_chk = MemorySaver()
    agent = make_react_agent(FakeLLM([say("ok")]), [_NOOP], checkpointer=custom_chk)
    run = agent.run("hello")
    assert run.status == "done"


def test_guarded_template_records_trajectory_and_memory() -> None:
    agent = make_guarded_react_agent(_looping_llm(), [_NOOP], limit=2)
    run = agent.run("do the thing", session_id="s1", user_id="alice")

    assert run.trajectory
    assert all(event.run_id == run.run_id for event in run.trajectory)
    # The seam captures node updates as episodic context (scope ladder).
    assert agent.memory.query(episode(run.run_id).namespace("context"))
    # And recall sees them through the visibility rule.
    assert recall(agent.memory, {"run_id": run.run_id, "user_id": "alice"})


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
    # Seam wrote through memory, into the episode scope.
    assert agent.memory.query(episode(run.run_id).namespace("context"))


def test_browsing_demo_runs_offline() -> None:
    from examples.web_browsing.demo import _demo_fake_llm, _fake_fetch

    agent = build_browsing_agent(_demo_fake_llm(), fetch=_fake_fetch)
    run = agent.run("title?", session_id="demo")

    assert run.status == "done"
    assert run.result is not None
    assert "Hello World" in run.result


def test_run_demo_smoke() -> None:
    run = run_demo()
    assert run.status == "done"
    assert run.trajectory
