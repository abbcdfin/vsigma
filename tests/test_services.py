"""Tests for the cross-cutting interceptor and interception points."""

from typing import Any, NotRequired, TypedDict, cast

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from vsigma import (
    HUMAN_INTERRUPT,
    Agent,
    FakeLLM,
    InMemoryStore,
    Interceptor,
    TrajectoryEvent,
    TrajectoryLog,
    add_wrapped,
    call,
    episode,
    make_guarded_react_agent,
    make_react_agent,
    say,
    wrap,
)
from vsigma.context import RecallContext, SlidingWindowContext
from vsigma.react import react_node
from vsigma.state import NodeFn, ReActState, State
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


def test_interceptor_kw_only() -> None:
    """Interceptor requires keyword-only arguments."""
    store = InMemoryStore()
    itc = Interceptor(memory=store)
    assert itc.memory is store


def test_run_end_without_memory_raises_at_construction() -> None:
    with pytest.raises(ValueError, match="run_end hook configured without a memory store"):
        Interceptor(run_end=lambda s, m: None)


def test_recall_context_without_memory_raises_at_first_assemble() -> None:
    ctx = RecallContext()
    with pytest.raises(ValueError, match="RecallContext requires a MemoryStore"):
        ctx.assemble({"messages": []}, memory=None)


def test_memory_none_means_zero_writes() -> None:
    """When memory is None, Agent and wrap skip all store writes."""

    class RecordingStore(InMemoryStore):
        writes: int = 0

        def write(self, namespace: str, key: str, value: object) -> None:
            self.writes += 1
            super().write(namespace, key, value)

    rec_store = RecordingStore()
    # Memory is explicitly None
    itc = Interceptor(memory=None, observer=TrajectoryLog())
    agent = make_react_agent(FakeLLM([say("done")]), [], interceptor=itc)
    run = agent.run("task without memory")
    assert run.status == "done"
    assert rec_store.writes == 0


def test_seam_keeps_every_node_firing_across_a_resume() -> None:
    """The seam's row key must not borrow `attempt_count`.

    `human_interrupt_node` resets the budget on resume (`resilience.py`) so the
    agent can continue; a key built on a resettable counter silently overwrote
    the rows written before the reset, losing exactly the runs where a human
    contributed something.
    """
    store = InMemoryStore()
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    llm = FakeLLM(
        [
            call("noop", text="a"),
            call("noop", text="b"),  # 2 attempts -> escalate
            call("noop", text="c"),
            say("done"),  # 2 more after the resume
        ]
    )
    agent = make_guarded_react_agent(llm, [_NOOP], interceptor=itc, limit=2)

    run = agent.run("task")
    assert run.status == "escalated"
    before = [key for key, _ in store.query(episode(run.run_id).namespace("context"))]

    run.resume("carry on")
    after = [key for key, _ in store.query(episode(run.run_id).namespace("context"))]

    # Six rows: 1 input row + five node firings (act x2, interrupt, act x2).
    # Nothing overwritten.
    assert len(after) == 6
    assert after[: len(before)] == before  # the pre-escalation rows survive verbatim
    assert len(set(after)) == len(after)


def test_attempt_count_still_resets_on_resume() -> None:
    """The budget must keep resetting — that is why it cannot key storage."""
    store = InMemoryStore()
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    llm = FakeLLM([call("noop", text="a"), call("noop", text="b"), say("done")])
    agent = make_guarded_react_agent(llm, [_NOOP], interceptor=itc, limit=2)

    run = agent.run("task")
    assert int(str(run.state["attempt_count"])) >= 2
    run.resume("carry on")

    assert run.status == "done"
    assert int(str(run.state["attempt_count"])) < 2  # reset by the interrupt
    keys = [key for key, _ in store.query(episode(run.run_id).namespace("context"))]
    assert len(keys) == len(set(keys)) == 5  # 1 input + 4 node firings, each with own row


def test_seam_leaves_the_node_update_untouched() -> None:
    """The seam must not add fields of its own to graph state: an extra channel
    write breaks fan-out, and a consumer's schema would filter it out anyway."""
    itc = Interceptor(memory=InMemoryStore(), observer=TrajectoryLog())
    update = wrap(lambda s: {"status": "done"}, itc, name="act")(
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
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    sg: StateGraph[Any, Any, Any, Any] = StateGraph(_Fork)
    add_wrapped(sg, "a", lambda s: {"a_done": True}, itc)
    add_wrapped(sg, "b", lambda s: {"b_done": True}, itc)
    sg.add_edge(START, "a")
    sg.add_edge(START, "b")
    sg.add_edge("a", END)
    sg.add_edge("b", END)

    run = Agent(sg.compile(checkpointer=MemorySaver()), itc).run("task")

    keys = [key for key, _ in store.query(episode(run.run_id).namespace("context"))]
    node_keys = [k for k in keys if not k.startswith("input:")]
    assert len(node_keys) == len(set(node_keys)) == 2
    assert {key.split(":")[0] for key in node_keys} == {"a", "b"}


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
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    sg: StateGraph[Any, Any, Any, Any] = StateGraph(_Legacy)

    def loop(state: State) -> State:
        n = int(str(state.get("attempt_count", 0))) + 1
        return {"attempt_count": n, "status": "done" if n >= 3 else "running"}

    add_wrapped(sg, "act", loop, itc)
    sg.add_edge(START, "act")
    sg.add_conditional_edges(
        "act", lambda s: END if s["status"] == "done" else "act", {END: END, "act": "act"}
    )

    run = Agent(sg.compile(checkpointer=MemorySaver()), itc).run("task")

    keys = [key for key, _ in store.query(episode(run.run_id).namespace("context"))]
    node_keys = [k for k in keys if not k.startswith("input:")]
    assert len(node_keys) == len(set(node_keys)) == 3
    assert all(key.startswith("act:") for key in node_keys)


def test_send_fan_out_to_one_node_keeps_a_row_per_firing() -> None:
    """`Send` runs several inputs through the *same* node in one superstep. A
    read-then-write ordinal allocator handed them the same number, so their rows
    overwrote each other; the key needs no coordination at all."""
    store = InMemoryStore()
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    sg: StateGraph[Any, Any, Any, Any] = StateGraph(_Fork)
    add_wrapped(sg, "work", lambda s: {}, itc)
    sg.add_conditional_edges(START, lambda s: [Send("work", dict(s)) for _ in range(5)], ["work"])
    sg.add_edge("work", END)

    run = Agent(sg.compile(checkpointer=MemorySaver()), itc).run("task")

    keys = [key for key, _ in store.query(episode(run.run_id).namespace("context"))]
    node_keys = [k for k in keys if not k.startswith("input:")]
    assert len(node_keys) == len(set(node_keys)) == 5


def test_context_assembly_is_view_only() -> None:
    """View-only context, proved (§H.8).

    With SlidingWindowContext(max_messages=3) over a multi-turn run:
    - the node sees <= 3 messages;
    - state keeps all of them;
    - each capture row holds only that node's new messages.
    """
    store = InMemoryStore()
    context = SlidingWindowContext(max_messages=3)
    itc = Interceptor(memory=store, observer=TrajectoryLog(), context=context)

    seen_lengths: list[int] = []

    class InspectingLLM(FakeLLM):
        def complete(
            self,
            messages: list[dict[str, object]],
            tools: list[dict[str, object]],
        ) -> Any:
            seen_lengths.append(len(messages))
            return super().complete(messages, tools)

    llm = InspectingLLM(
        [
            call("noop", text="turn 1"),
            call("noop", text="turn 2"),
            call("noop", text="turn 3"),
            say("final turn"),
        ]
    )

    sg: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    add_wrapped(sg, "act", react_node(llm, [_NOOP]), itc)
    sg.add_edge(START, "act")
    sg.add_conditional_edges(
        "act",
        lambda s: END if s.get("status") == "done" else "act",
        {END: END, "act": "act"},
    )

    run = Agent(sg.compile(checkpointer=MemorySaver()), itc).run("task")
    assert run.status == "done"

    # 1. The node never saw more than 3 messages (window applied)
    assert all(length <= 3 for length in seen_lengths)
    # 2. State keeps all messages across all turns
    messages = run.state.get("messages")
    assert isinstance(messages, list) and len(messages) > 3
    # 3. Each capture row holds only that node's new messages (linear capture)
    rows = store.query(episode(run.run_id).namespace("context"))
    for _, row in rows:
        if isinstance(row, dict) and isinstance(row.get("messages"), list):
            assert len(row["messages"]) <= 2


def test_guarded_run_across_pause_and_resume_emits_expected_sequence() -> None:
    log = TrajectoryLog()
    itc = Interceptor(observer=log)
    llm = FakeLLM([call("missing_tool"), say("done")])
    agent = make_guarded_react_agent(llm, [], limit=1, interceptor=itc)

    run = agent.run("task")
    assert run.status == "escalated"

    run.resume("resume guidance")
    assert run.status == "done"

    events = run.trajectory
    assert len(events) == 3
    assert [(e.node, e.attempt) for e in events] == [
        ("act", 0),
        (HUMAN_INTERRUPT, 1),
        ("act", 0),
    ]


def test_unwrapped_node_and_nested_graph_node_each_get_an_event() -> None:
    class State(TypedDict):
        val: str
        attempt_count: NotRequired[int]

    # Inner subgraph
    sub = StateGraph(State)
    sub.add_node("inner", lambda s: {"val": s.get("val", "") + ":inner"})
    sub.add_edge(START, "inner")
    sub_graph = sub.compile()

    # Outer main graph with unwrapped node and nested subgraph node
    main = StateGraph(State)
    main.add_node("plain", lambda s: {"val": s.get("val", "") + ":plain"})
    main.add_node("sub", sub_graph)
    main.add_edge(START, "plain")
    main.add_edge("plain", "sub")

    log = TrajectoryLog()
    itc = Interceptor(observer=log)
    agent = Agent(main.compile(checkpointer=MemorySaver()), itc)

    run = agent.run("test")
    events = run.trajectory
    nodes = [e.node for e in events]
    assert "plain" in nodes
    assert "sub/inner" in nodes


def test_agent_run_without_observer_or_memory_records_zero_events() -> None:
    llm = FakeLLM([say("done")])
    agent = make_react_agent(llm, [])
    assert agent.observer is None
    assert agent.memory is None

    run = agent.run("task")
    assert run.status == "done"
    with pytest.raises(
        RuntimeError,
        match="Run.trajectory requires a TrajectoryLog observer",
    ):
        _ = run.trajectory


def test_agent_run_with_custom_observer_raises_on_trajectory_access() -> None:
    class CustomObserver:
        def emit(self, event: TrajectoryEvent) -> None:
            pass

    llm = FakeLLM([say("done")])
    itc = Interceptor(observer=CustomObserver())
    agent = make_react_agent(llm, [], interceptor=itc)

    run = agent.run("task")
    assert run.status == "done"
    with pytest.raises(
        RuntimeError,
        match="Run.trajectory requires a TrajectoryLog observer",
    ):
        _ = run.trajectory


def test_wrap_passes_none_update_through_without_error() -> None:
    itc = Interceptor()

    def none_node(state: State) -> Any:
        return None

    wrapped = wrap(cast("NodeFn", none_node), itc, name="none_node")
    result = wrapped({"messages": []})
    assert result is None


def test_wrap_drops_echoed_messages_behind_sliding_window() -> None:
    store = InMemoryStore()
    itc = Interceptor(memory=store, context=SlidingWindowContext(max_messages=2))

    def echoing_node(state: State) -> State:
        messages = list(cast("list[Any]", state.get("messages", [])))
        new_msg = {"role": "assistant", "content": "fresh"}
        return {"messages": [*messages, new_msg], "status": "done"}

    sg: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    add_wrapped(sg, "echo_node", echoing_node, itc)
    sg.add_edge(START, "echo_node")
    agent = Agent(sg.compile(checkpointer=MemorySaver()), itc)
    sess = agent.session("s1")

    run1 = sess.send("hello")
    assert run1.status == "done"
    run2 = sess.send("again")
    assert run2.status == "done"

    msgs = cast("list[Any]", run2.state.get("messages", []))
    contents = [getattr(m, "content", "") for m in msgs]
    assert contents == ["hello", "fresh", "again", "fresh"]

    rows = store.query(episode(run2.run_id).namespace("context"))
    node_rows = [val for key, val in rows if key.startswith("echo_node:") and isinstance(val, dict)]
    assert len(node_rows) == 1
    assert node_rows[0]["messages"] == [{"role": "assistant", "content": "fresh"}]


def test_wrap_drops_echoed_messages_behind_recall_context() -> None:
    from vsigma.records import MemoryRecord, Provenance

    store = InMemoryStore()
    rec = MemoryRecord(
        id="k1",
        scope="global",
        kind="knowledge",
        content="important fact",
        provenance=Provenance(origin="injected", source_ref="test", confidence=1.0),
    )
    store.write(rec.namespace, rec.id, rec.to_dict())
    itc = Interceptor(memory=store, context=RecallContext())

    def echoing_node(state: State) -> State:
        messages = list(cast("list[Any]", state.get("messages", [])))
        new_msg = {"role": "assistant", "content": "fresh"}
        return {"messages": [*messages, new_msg], "status": "done"}

    sg: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    add_wrapped(sg, "echo_node", echoing_node, itc)
    sg.add_edge(START, "echo_node")
    agent = Agent(sg.compile(checkpointer=MemorySaver()), itc)

    run = agent.run("hello")
    assert run.status == "done"
    msgs = cast("list[Any]", run.state.get("messages", []))
    contents = [getattr(m, "content", "") for m in msgs]
    assert not any("[vsigma:recall]" in str(c) for c in contents)
    assert contents == ["hello", "fresh"]

    rows = store.query(episode(run.run_id).namespace("context"))
    node_rows = [val for key, val in rows if key.startswith("echo_node:") and isinstance(val, dict)]
    assert len(node_rows) == 1
    assert node_rows[0]["messages"] == [{"role": "assistant", "content": "fresh"}]
