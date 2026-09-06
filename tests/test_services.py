"""Tests for the cross-cutting service defaults."""

from vsigma import (
    FakeLLM,
    InMemoryStore,
    Services,
    TrajectoryEvent,
    TrajectoryLog,
    call,
    episode,
    make_guarded_react_agent,
    say,
    wrap,
)
from vsigma.tools import Tool

_NOOP = Tool(
    name="noop",
    description="noop",
    parameters={"type": "object", "properties": {}},
    func=lambda args: "ok",
)


def test_inmemory_store_is_symmetric() -> None:
    store = InMemoryStore()
    store.write("ns", "k1", {"a": 1})
    store.write("ns", "k2", "value")

    assert store.read("ns", "k1") == {"a": 1}
    assert store.read("ns", "missing") is None
    assert dict(store.query("ns")) == {"k1": {"a": 1}, "k2": "value"}
    assert store.query("other") == []


def test_trajectory_log_appends() -> None:
    log = TrajectoryLog()
    event = TrajectoryEvent(
        run_id="r", task_id="t", session_id=None, node="act", attempt=1, summary="s"
    )
    log.emit(event)
    assert log.events == [event]


def test_seam_keeps_every_node_firing_across_a_resume() -> None:
    """The seam's row key must not borrow `attempt_count`.

    `human_interrupt_node` resets the budget on resume (`resilience.py`) so the
    agent can continue; a key built on a resettable counter silently overwrote
    the rows written before the reset, losing exactly the runs where a human
    contributed something.
    """
    store = InMemoryStore()
    svc = Services(memory=store, observer=TrajectoryLog())
    llm = FakeLLM(
        [call("noop", text="a"), call("noop", text="b"),  # 2 attempts -> escalate
         call("noop", text="c"), say("done")]             # 2 more after the resume
    )
    agent = make_guarded_react_agent(llm, [_NOOP], services=svc, limit=2)

    run = agent.run("task")
    assert run.status == "escalated"
    before = [key for key, _ in store.query(episode(run.run_id).namespace("context"))]

    run.resume("carry on")
    after = [key for key, _ in store.query(episode(run.run_id).namespace("context"))]

    # Five firings: act x2, the interrupt, act x2. Nothing overwritten.
    assert len(after) == 5
    assert after[: len(before)] == before  # the pre-escalation rows survive verbatim
    assert len(set(after)) == len(after)


def test_attempt_count_still_resets_on_resume() -> None:
    """The budget must keep resetting — that is why it cannot key storage."""
    svc = Services(memory=InMemoryStore(), observer=TrajectoryLog())
    llm = FakeLLM([call("noop", text="a"), call("noop", text="b"), say("done")])
    agent = make_guarded_react_agent(llm, [_NOOP], services=svc, limit=2)

    run = agent.run("task")
    assert int(str(run.state["attempt_count"])) >= 2
    run.resume("carry on")

    assert run.status == "done"
    assert int(str(run.state["step_count"])) > int(str(run.state["attempt_count"]))


def test_seam_injects_step_count_into_a_schema_that_declares_it() -> None:
    """`step_count` rides on `BaseState`, so any vsigma state schema has it; a
    consumer's own schema must extend `BaseState`/`ReActState` as documented."""
    svc = Services(memory=InMemoryStore(), observer=TrajectoryLog())
    update = wrap(lambda s: {"status": "done"}, svc, name="act")(
        {"run_id": "r1", "task_id": "t1", "attempt_count": 0, "status": "running"}
    )
    assert update == {"status": "done", "step_count": 1}
