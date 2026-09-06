"""Tests for the cross-cutting service defaults."""

from typing import Any, NotRequired, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from vsigma import (
    Agent,
    FakeLLM,
    InMemoryStore,
    Services,
    TrajectoryEvent,
    TrajectoryLog,
    add_wrapped,
    call,
    episode,
    make_guarded_react_agent,
    say,
    wrap,
)
from vsigma.episodes import next_step
from vsigma.state import State
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
    store = InMemoryStore()
    svc = Services(memory=store, observer=TrajectoryLog())
    llm = FakeLLM([call("noop", text="a"), call("noop", text="b"), say("done")])
    agent = make_guarded_react_agent(llm, [_NOOP], services=svc, limit=2)

    run = agent.run("task")
    assert int(str(run.state["attempt_count"])) >= 2
    run.resume("carry on")

    assert run.status == "done"
    assert int(str(run.state["attempt_count"])) < 2  # reset by the interrupt
    assert next_step(store, run.run_id) == 4  # but the seam's counter kept going


def test_seam_leaves_the_node_update_untouched() -> None:
    """The seam must not add fields of its own to graph state: an extra channel
    write breaks fan-out, and a consumer's schema would filter it out anyway."""
    svc = Services(memory=InMemoryStore(), observer=TrajectoryLog())
    update = wrap(lambda s: {"status": "done"}, svc, name="act")(
        {"run_id": "r1", "task_id": "t1", "attempt_count": 0, "status": "running"}
    )
    assert update == {"status": "done"}


class _Fork(TypedDict):
    task_id: str
    run_id: str
    attempt_count: int
    status: str
    a_done: NotRequired[bool]
    b_done: NotRequired[bool]


def test_seam_survives_a_fan_out_to_two_wrapped_nodes() -> None:
    """Two wrapped nodes in one superstep. A counter in graph state made this
    raise InvalidUpdateError even though the nodes write disjoint keys."""
    store = InMemoryStore()
    svc = Services(memory=store, observer=TrajectoryLog())
    sg: StateGraph[Any, Any, Any, Any] = StateGraph(_Fork)
    add_wrapped(sg, "a", lambda s: {"a_done": True}, svc)
    add_wrapped(sg, "b", lambda s: {"b_done": True}, svc)
    sg.add_edge(START, "a")
    sg.add_edge(START, "b")
    sg.add_edge("a", END)
    sg.add_edge("b", END)

    run = Agent(sg.compile(checkpointer=MemorySaver()), svc).run("task")

    keys = [key for key, _ in store.query(episode(run.run_id).namespace("context"))]
    # Two distinct rows and no channel conflict. Which node takes which ordinal
    # is not fixed — execution order within a superstep is LangGraph's business.
    assert len(keys) == len(set(keys)) == 2
    assert {key.split(":")[0] for key in keys} == {"a", "b"}


class _Legacy(TypedDict):
    """A consumer's own schema, predating any field the seam might want."""

    task_id: str
    run_id: str
    attempt_count: int
    status: str


def test_seam_does_not_depend_on_the_consumers_state_schema() -> None:
    """A schema that declares only the control fields still gets one row per
    firing. A counter carried in graph state was filtered out of every update
    here, so it read zero forever and each row overwrote the last."""
    store = InMemoryStore()
    svc = Services(memory=store, observer=TrajectoryLog())
    sg: StateGraph[Any, Any, Any, Any] = StateGraph(_Legacy)

    def loop(state: State) -> State:
        n = int(str(state.get("attempt_count", 0))) + 1
        return {"attempt_count": n, "status": "done" if n >= 3 else "running"}

    add_wrapped(sg, "act", loop, svc)
    sg.add_edge(START, "act")
    sg.add_conditional_edges(
        "act", lambda s: END if s["status"] == "done" else "act", {END: END, "act": "act"}
    )

    run = Agent(sg.compile(checkpointer=MemorySaver()), svc).run("task")

    keys = [key for key, _ in store.query(episode(run.run_id).namespace("context"))]
    assert keys == ["act:0", "act:1", "act:2"]
