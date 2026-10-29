"""Tests for thread retention: keep_latest, Session.end, Run.end, retain_window."""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Sequence
from typing import Any, cast

import pytest
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.memory import InMemorySaver, MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from vsigma import (
    Agent,
    FakeLLM,
    Interceptor,
    LLMResponse,
    ReActState,
    Tool,
    ToolCall,
    keep_latest,
    make_guarded_react_agent,
    make_react_agent,
    retain_window,
)


class UnsupportedSaver(BaseCheckpointSaver[str]):
    """A dummy checkpointer that inherits from BaseCheckpointSaver but does not implement prune."""

    def get_tuple(self, config: Any) -> CheckpointTuple | None:
        return None

    def list(self, config: Any, **kwargs: Any) -> Any:
        return iter(())

    def put(
        self,
        config: Any,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> Any:
        return config

    def put_writes(
        self,
        config: Any,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        pass


class PruneDelegatingSaver(UnsupportedSaver):
    """A checkpointer that overrides prune."""

    def __init__(self) -> None:
        super().__init__()
        self.prune_calls: list[tuple[Sequence[str], str]] = []

    def prune(
        self,
        thread_ids: Sequence[str],
        *,
        strategy: str = "keep_latest",
    ) -> None:
        self.prune_calls.append((thread_ids, strategy))


def _make_dummy_graph(checkpointer: Any = None, with_delta: bool = False) -> Any:
    builder: StateGraph[Any, Any, Any, Any] = StateGraph(dict)
    if with_delta:
        builder.channels["delta"] = DeltaChannel(lambda a, b: a)
    builder.add_node("n", lambda s: s)
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    return builder.compile(checkpointer=checkpointer)


# Refusal tests


def test_refusal_no_checkpointer() -> None:
    g = _make_dummy_graph(checkpointer=None)
    with pytest.raises(ValueError, match="checkpointer"):
        keep_latest(g, "t1")


def test_refusal_unsupported_checkpointer() -> None:
    g = _make_dummy_graph(checkpointer=UnsupportedSaver())
    with pytest.raises(NotImplementedError, match="UnsupportedSaver"):
        keep_latest(g, "t1")


def test_refusal_delta_channel() -> None:
    g = _make_dummy_graph(checkpointer=MemorySaver(), with_delta=True)
    with pytest.raises(ValueError, match="DeltaChannel"):
        keep_latest(g, "t1")


# Delegation test


def test_delegation_to_prune_override() -> None:
    saver = PruneDelegatingSaver()
    g = _make_dummy_graph(checkpointer=saver)
    keep_latest(g, "thread-42")
    assert saver.prune_calls == [(["thread-42"], "keep_latest")]


# Parametrized Saver fixture


@pytest.fixture(params=["in_memory", "sqlite"])
def saver(request: pytest.FixtureRequest, tmp_path: pathlib.Path) -> BaseCheckpointSaver[Any]:
    if request.param == "in_memory":
        return InMemorySaver()
    db_path = tmp_path / f"{request.node.name}.sqlite"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return SqliteSaver(conn)


def test_three_turns_then_keep_latest(saver: BaseCheckpointSaver[Any]) -> None:
    llm = FakeLLM([
        LLMResponse(text="one"),
        LLMResponse(text="two"),
        LLMResponse(text="three"),
        LLMResponse(text="four"),
    ])
    agent = make_react_agent(llm, tools=[], checkpointer=saver)
    sess = agent.session("s1")

    sess.send("1")
    sess.send("2")
    sess.send("3")

    if isinstance(saver, InMemorySaver):
        assert len(saver.storage["s1"][""]) >= 3
        messages_blobs_before = [
            k for k in saver.blobs if k[0] == "s1" and k[1] == "" and k[2] == "messages"
        ]
        assert len(messages_blobs_before) >= 3
    elif isinstance(saver, SqliteSaver):
        cur = saver.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = 's1'")
        row = cur.fetchone()
        assert row is not None and row[0] >= 3

    keep_latest(agent.graph, "s1")

    if isinstance(saver, InMemorySaver):
        assert len(saver.storage["s1"][""]) == 1
        messages_blobs_after = [
            k for k in saver.blobs if k[0] == "s1" and k[1] == "" and k[2] == "messages"
        ]
        assert len(messages_blobs_after) == 1
    elif isinstance(saver, SqliteSaver):
        cur = saver.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = 's1'")
        row = cur.fetchone()
        assert row is not None and row[0] == 1
        cur.execute("SELECT COUNT(*) FROM writes WHERE thread_id = 's1'")
        row_w = cur.fetchone()
        assert row_w is not None and row_w[0] == 0

    assert len(list(agent.graph.get_state_history(cast("Any", sess.config)))) == 1

    # Dialog is intact
    dialog = sess.dialog
    assert len(dialog) == 6
    assert dialog[0]["content"] == "1"
    assert dialog[1]["content"] == "one"
    assert dialog[2]["content"] == "2"

    # Fourth turn runs cleanly
    run4 = sess.send("4")
    assert run4.status == "done"
    assert len(sess.dialog) == 8


def test_paused_guarded_session_resumes(saver: BaseCheckpointSaver[Any]) -> None:
    tools = [
        Tool(
            name="fail",
            description="always fails",
            parameters={"type": "object", "properties": {}},
            func=lambda args: "failed",
        )
    ]
    responses = [
        LLMResponse(text="", tool_calls=[ToolCall(id="c1", name="fail", arguments={})]),
        LLMResponse(text="", tool_calls=[ToolCall(id="c2", name="fail", arguments={})]),
    ]
    llm = FakeLLM(responses)
    agent = make_guarded_react_agent(
        llm,
        tools=tools,
        checkpointer=saver,
        limit=1,
    )
    sess = agent.session("guarded-sess")

    run1 = sess.send("start")
    assert run1.interrupted
    assert sess.paused

    # Call keep_latest while paused
    keep_latest(agent.graph, "guarded-sess")

    # The pending interrupt survives
    assert sess.paused
    assert sess.interrupt is not None

    # Resume completes turn
    llm.responses = [LLMResponse(text="recovered")]
    llm._cursor = 0
    run2 = sess.resume("manual answer")
    assert run2.status == "done"


def test_paused_subgraph_namespace_survives_then_pruned_after_resume(
    saver: BaseCheckpointSaver[Any],
) -> None:
    sub: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)

    def pause_node(state: ReActState) -> dict[str, object]:
        msgs = state.get("messages", [])
        if len(msgs) > 2:
            return {"status": "done"}
        val = interrupt("sub_pause")
        return {
            "messages": [{"role": "assistant", "content": f"sub_ans={val}"}],
            "status": "done",
        }

    sub.add_node("step", pause_node)
    sub.add_edge(START, "step")

    parent: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    parent.add_node("sub_node", sub.compile())
    parent.add_edge(START, "sub_node")

    agent = Agent(parent.compile(checkpointer=saver), Interceptor())
    sess = agent.session("sub-sess")

    run = sess.send("start")
    assert run.interrupted
    assert sess.paused

    # Subgraph namespace exists
    if isinstance(saver, InMemorySaver):
        sub_namespaces = [ns for ns in saver.storage["sub-sess"] if ns != ""]
    elif isinstance(saver, SqliteSaver):
        cur = saver.conn.cursor()
        cur.execute(
            "SELECT DISTINCT checkpoint_ns FROM checkpoints "
            "WHERE thread_id = 'sub-sess' AND checkpoint_ns != ''"
        )
        sub_namespaces = [r[0] for r in cur.fetchall()]
    else:
        sub_namespaces = []

    assert len(sub_namespaces) == 1
    sub_ns = sub_namespaces[0]

    # keep_latest while paused: subgraph namespace survives
    keep_latest(agent.graph, "sub-sess")
    if isinstance(saver, InMemorySaver):
        assert sub_ns in saver.storage["sub-sess"]
        assert len(saver.storage["sub-sess"][sub_ns]) == 1
    elif isinstance(saver, SqliteSaver):
        cur = saver.conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = 'sub-sess' AND checkpoint_ns = ?",
            (sub_ns,),
        )
        row = cur.fetchone()
        assert row is not None and row[0] == 1

    # Resume
    run2 = sess.resume("answer")
    assert run2.status == "done"
    assert not sess.paused

    # keep_latest after resume: subgraph namespace is gone
    keep_latest(agent.graph, "sub-sess")
    if isinstance(saver, InMemorySaver):
        remaining_namespaces = list(saver.storage["sub-sess"].keys())
    elif isinstance(saver, SqliteSaver):
        cur = saver.conn.cursor()
        cur.execute(
            "SELECT DISTINCT checkpoint_ns FROM checkpoints WHERE thread_id = 'sub-sess'"
        )
        remaining_namespaces = [r[0] for r in cur.fetchall()]
    else:
        remaining_namespaces = []
    assert remaining_namespaces == [""]

    # Next turn runs cleanly
    run3 = sess.send("next")
    assert run3.status == "done"


def test_finished_subgraph_leaves_no_namespace(saver: BaseCheckpointSaver[Any]) -> None:
    sub: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)

    def sub_step(state: ReActState) -> dict[str, object]:
        return {
            "messages": [{"role": "assistant", "content": "from sub"}],
            "status": "done",
        }

    sub.add_node("step", sub_step)
    sub.add_edge(START, "step")

    parent: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    parent.add_node("sub_node", sub.compile())
    parent.add_edge(START, "sub_node")

    agent = Agent(parent.compile(checkpointer=saver), Interceptor(), durability=None)
    sess = agent.session("sub-straight")

    run = sess.send("start")
    assert run.status == "done"

    if isinstance(saver, InMemorySaver):
        sub_ns = [ns for ns in saver.storage["sub-straight"] if ns != ""]
        assert len(sub_ns) == 1
        assert len(saver.storage["sub-straight"][sub_ns[0]]) >= 1
    elif isinstance(saver, SqliteSaver):
        cur = saver.conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM checkpoints "
            "WHERE thread_id = 'sub-straight' AND checkpoint_ns != ''"
        )
        row = cur.fetchone()
        assert row is not None and row[0] >= 1

    keep_latest(agent.graph, "sub-straight")
    if isinstance(saver, InMemorySaver):
        remaining = list(saver.storage["sub-straight"].keys())
        assert [k for k in saver.blobs if k[0] == "sub-straight" and k[1] != ""] == []
        assert [k for k in saver.writes if k[0] == "sub-straight" and k[1] != ""] == []
    elif isinstance(saver, SqliteSaver):
        cur = saver.conn.cursor()
        cur.execute(
            "SELECT DISTINCT checkpoint_ns FROM checkpoints WHERE thread_id = 'sub-straight'"
        )
        remaining = [r[0] for r in cur.fetchall()]
        cur.execute(
            "SELECT COUNT(*) FROM writes WHERE thread_id = 'sub-straight' AND checkpoint_ns != ''"
        )
        assert cur.fetchone()[0] == 0
    else:
        remaining = []
    assert remaining == [""]

    # The next turn runs on the pruned thread
    assert sess.send("next").status == "done"


def test_earlier_run_snapshot_raises(saver: BaseCheckpointSaver[Any]) -> None:
    llm = FakeLLM([
        LLMResponse(text="reply 1"),
        LLMResponse(text="reply 2"),
    ])
    agent = make_react_agent(llm, tools=[], checkpointer=saver)
    sess = agent.session("snap-sess")

    run1 = sess.send("one")
    assert run1.snapshot is not None

    run2 = sess.send("two")
    assert run2.snapshot is not None

    keep_latest(agent.graph, "snap-sess")

    # run2 (active) snapshot succeeds; run1 (earlier) snapshot raises RuntimeError
    assert run2.snapshot is not None
    with pytest.raises(RuntimeError, match=run1.run_id):
        _ = run1.snapshot


def test_other_thread_untouched(saver: BaseCheckpointSaver[Any]) -> None:
    llm = FakeLLM([
        LLMResponse(text="t1_1"),
        LLMResponse(text="t1_2"),
        LLMResponse(text="t2_1"),
        LLMResponse(text="t2_2"),
    ])
    agent = make_react_agent(llm, tools=[], checkpointer=saver)
    sess1 = agent.session("thread-1")
    sess2 = agent.session("thread-2")

    sess1.send("a")
    sess1.send("b")
    sess2.send("c")
    sess2.send("d")

    if isinstance(saver, InMemorySaver):
        t2_checkpoints_before = len(saver.storage["thread-2"][""])
        t2_blobs_before = len([k for k in saver.blobs if k[0] == "thread-2"])
    elif isinstance(saver, SqliteSaver):
        cur = saver.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = 'thread-2'")
        t2_checkpoints_before = cur.fetchone()[0]
    else:
        t2_checkpoints_before = 0

    keep_latest(agent.graph, "thread-1")

    # thread-1 pruned
    if isinstance(saver, InMemorySaver):
        assert len(saver.storage["thread-1"][""]) == 1
        assert len(saver.storage["thread-2"][""]) == t2_checkpoints_before
        assert len([k for k in saver.blobs if k[0] == "thread-2"]) == t2_blobs_before
    elif isinstance(saver, SqliteSaver):
        cur = saver.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = 'thread-1'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = 'thread-2'")
        assert cur.fetchone()[0] == t2_checkpoints_before


def test_sqlite_reopened_saver_continues_session_and_resumes_pause(
    tmp_path: pathlib.Path,
) -> None:
    db_file = tmp_path / "reopen.sqlite"

    # Part 1: regular session across connections
    conn1 = sqlite3.connect(str(db_file), check_same_thread=False)
    saver1 = SqliteSaver(conn1)
    llm1 = FakeLLM([
        LLMResponse(text="reply 1"),
        LLMResponse(text="reply 2"),
    ])
    agent1 = make_react_agent(llm1, tools=[], checkpointer=saver1)
    sess1 = agent1.session("s-reopen")
    sess1.send("turn 1")
    sess1.send("turn 2")
    keep_latest(agent1.graph, "s-reopen")
    conn1.close()

    # Reopen on same file with new connection
    conn2 = sqlite3.connect(str(db_file), check_same_thread=False)
    saver2 = SqliteSaver(conn2)
    llm2 = FakeLLM([LLMResponse(text="reply 3")])
    agent2 = make_react_agent(llm2, tools=[], checkpointer=saver2)
    sess2 = agent2.session("s-reopen")
    assert len(sess2.dialog) == 4
    run3 = sess2.send("turn 3")
    assert run3.status == "done"
    assert len(sess2.dialog) == 6
    conn2.close()

    # Part 2: paused session across connections
    tools = [
        Tool(
            name="fail",
            description="always fails",
            parameters={"type": "object", "properties": {}},
            func=lambda args: "failed",
        )
    ]
    conn3 = sqlite3.connect(str(db_file), check_same_thread=False)
    saver3 = SqliteSaver(conn3)
    llm3 = FakeLLM([
        LLMResponse(text="", tool_calls=[ToolCall(id="c1", name="fail", arguments={})]),
    ])
    agent3 = make_guarded_react_agent(llm3, tools=tools, checkpointer=saver3, limit=1)
    sess3 = agent3.session("s-guarded-reopen")
    run_p = sess3.send("start")
    assert run_p.interrupted
    assert sess3.paused
    keep_latest(agent3.graph, "s-guarded-reopen")
    conn3.close()

    # Reopen and resume
    conn4 = sqlite3.connect(str(db_file), check_same_thread=False)
    saver4 = SqliteSaver(conn4)
    llm4 = FakeLLM([LLMResponse(text="fixed")])
    agent4 = make_guarded_react_agent(llm4, tools=tools, checkpointer=saver4, limit=1)
    sess4 = agent4.session("s-guarded-reopen")
    assert sess4.paused
    assert sess4.interrupt is not None
    run_res = sess4.resume("manual input")
    assert run_res.status == "done"
    conn4.close()


# Step 4: checkpoints="latest" on Agent and templates


def test_agent_checkpoints_latest_construction_refusals() -> None:
    g_no_cp = _make_dummy_graph(checkpointer=None)
    with pytest.raises(ValueError, match="checkpointer"):
        Agent(g_no_cp, Interceptor(), checkpoints="latest")

    g_unsupported = _make_dummy_graph(checkpointer=UnsupportedSaver())
    with pytest.raises(NotImplementedError, match="UnsupportedSaver"):
        Agent(g_unsupported, Interceptor(), checkpoints="latest")

    g_delta = _make_dummy_graph(checkpointer=MemorySaver(), with_delta=True)
    with pytest.raises(ValueError, match="DeltaChannel"):
        Agent(g_delta, Interceptor(), checkpoints="latest")

    # make_react_agent with unsupported checkpointer
    llm = FakeLLM([])
    with pytest.raises(NotImplementedError, match="UnsupportedSaver"):
        make_react_agent(llm, tools=[], checkpointer=UnsupportedSaver(), checkpoints="latest")

    # Invalid checkpoints value
    g_ok = _make_dummy_graph(checkpointer=MemorySaver())
    with pytest.raises(ValueError, match="checkpoints"):
        Agent(g_ok, Interceptor(), checkpoints=cast("Any", "latestt"))


def test_agent_checkpoints_latest_three_turns_holds_one_root_checkpoint(
    saver: BaseCheckpointSaver[Any],
) -> None:
    llm = FakeLLM([
        LLMResponse(text="one"),
        LLMResponse(text="two"),
        LLMResponse(text="three"),
    ])
    agent = make_react_agent(llm, tools=[], checkpointer=saver, checkpoints="latest")
    sess = agent.session("latest-sess")

    sess.send("1")
    sess.send("2")
    sess.send("3")

    # Only 1 root checkpoint remains
    assert len(list(agent.graph.get_state_history(cast("Any", sess.config)))) == 1
    assert len(sess.dialog) == 6


def test_agent_checkpoints_latest_with_reflexive_consolidation() -> None:
    from vsigma.consolidation import reflexive_consolidator
    from vsigma.memory import InMemoryStore
    from vsigma.observability import TrajectoryLog
    from vsigma.scope import session

    fact_line = json.dumps({"content": "blue is favorite", "source_ref": "t:1"})
    store = InMemoryStore()
    itc = Interceptor(
        memory=store,
        observer=TrajectoryLog(),
        run_end=reflexive_consolidator(FakeLLM([LLMResponse(text=fact_line)])),
    )
    saver = MemorySaver()
    agent = make_react_agent(
        FakeLLM([LLMResponse(text="done")]),
        tools=[],
        interceptor=itc,
        checkpointer=saver,
        checkpoints="latest",
    )
    sess = agent.session("cons-sess", user_id="alice")
    run = sess.send("my favorite color is blue")

    assert run.status == "done"
    # Episode summary / context was written
    stored = store.read(session("cons-sess").namespace("context"), f"episode:{run.run_id}")
    assert stored is not None
    # Knowledge was distilled
    facts = store.query(session("cons-sess").namespace("knowledge"))
    assert len(facts) == 1
    # Checkpointer kept only 1 checkpoint
    assert len(list(agent.graph.get_state_history(cast("Any", sess.config)))) == 1


def test_agent_checkpoints_latest_paused_turn_keeps_pause_and_resumes() -> None:
    tools = [
        Tool(
            name="fail",
            description="always fails",
            parameters={"type": "object", "properties": {}},
            func=lambda args: "failed",
        )
    ]
    llm = FakeLLM([
        LLMResponse(text="", tool_calls=[ToolCall(id="c1", name="fail", arguments={})]),
    ])
    saver = MemorySaver()
    agent = make_guarded_react_agent(
        llm,
        tools=tools,
        checkpointer=saver,
        limit=1,
        checkpoints="latest",
    )
    sess = agent.session("guarded-latest")

    run1 = sess.send("start")
    assert run1.interrupted
    assert sess.paused

    # Pending interrupt preserved
    assert sess.interrupt is not None
    # Checkpointer kept only latest (paused) checkpoint
    assert len(list(agent.graph.get_state_history(cast("Any", sess.config)))) == 1

    # Resume completes
    llm.responses = [LLMResponse(text="recovered")]
    llm._cursor = 0
    run2 = sess.resume("manual answer")
    assert run2.status == "done"
    assert not sess.paused


def test_agent_checkpoints_default_all_leaves_three_checkpoints(
    saver: BaseCheckpointSaver[Any],
) -> None:
    llm = FakeLLM([
        LLMResponse(text="one"),
        LLMResponse(text="two"),
        LLMResponse(text="three"),
    ])
    agent = make_react_agent(llm, tools=[], checkpointer=saver, checkpoints="all")
    sess = agent.session("all-sess")

    sess.send("1")
    sess.send("2")
    sess.send("3")

    # With default "all", 3 turns leave at least 3 checkpoints
    assert len(list(agent.graph.get_state_history(cast("Any", sess.config)))) >= 3


# Step 5: Session.end() and Run.end()


def test_session_end_deletes_thread_dialog_empty_later_send_starts_empty(
    saver: BaseCheckpointSaver[Any],
) -> None:
    llm = FakeLLM([
        LLMResponse(text="one"),
        LLMResponse(text="two"),
        LLMResponse(text="three"),
    ])
    agent = make_react_agent(llm, tools=[], checkpointer=saver)
    sess = agent.session("s-end")
    sess.send("1")
    sess.send("2")
    assert len(sess.dialog) == 4

    sess.end()
    assert sess.dialog == []
    assert len(list(agent.graph.get_state_history(cast("Any", sess.config)))) == 0

    run3 = sess.send("3")
    assert run3.status == "done"
    assert len(sess.dialog) == 2


def test_session_end_paused_refuses_without_abandon_and_ends_with_abandon(
    saver: BaseCheckpointSaver[Any],
) -> None:
    from vsigma.sessions import SessionPaused

    tools = [
        Tool(
            name="fail",
            description="always fails",
            parameters={"type": "object", "properties": {}},
            func=lambda args: "failed",
        )
    ]
    llm = FakeLLM([
        LLMResponse(text="", tool_calls=[ToolCall(id="c1", name="fail", arguments={})]),
    ])
    agent = make_guarded_react_agent(llm, tools=tools, checkpointer=saver, limit=1)
    sess = agent.session("s-pause-end")
    sess.send("start")
    assert sess.paused

    with pytest.raises(SessionPaused):
        sess.end()

    # With abandon=True, it ends cleanly
    sess.end(abandon=True)
    assert sess.dialog == []
    assert len(list(agent.graph.get_state_history(cast("Any", sess.config)))) == 0


def test_end_preserves_capture_rows_and_episode_summary() -> None:
    from vsigma.consolidation import reflexive_consolidator
    from vsigma.memory import InMemoryStore
    from vsigma.observability import TrajectoryLog
    from vsigma.scope import session

    fact_line = json.dumps({"content": "important fact", "source_ref": "t:1"})
    store = InMemoryStore()
    itc = Interceptor(
        memory=store,
        observer=TrajectoryLog(),
        run_end=reflexive_consolidator(FakeLLM([LLMResponse(text=fact_line)])),
    )
    saver = MemorySaver()
    agent = make_react_agent(
        FakeLLM([LLMResponse(text="done")]),
        tools=[],
        interceptor=itc,
        checkpointer=saver,
    )
    sess = agent.session("preserve-sess", user_id="alice")
    run = sess.send("hello")
    assert run.status == "done"

    sess.end()

    # Checkpoint thread is deleted
    assert len(list(agent.graph.get_state_history(cast("Any", sess.config)))) == 0
    # Capture rows (context) and knowledge survive in memory store
    stored = store.read(session("preserve-sess").namespace("context"), f"episode:{run.run_id}")
    assert stored is not None
    facts = store.query(session("preserve-sess").namespace("knowledge"))
    assert len(facts) == 1


def test_run_end_on_session_run_raises(saver: BaseCheckpointSaver[Any]) -> None:
    llm = FakeLLM([LLMResponse(text="reply")])
    agent = make_react_agent(llm, tools=[], checkpointer=saver)
    sess = agent.session("s-run-end")
    run = sess.send("hello")

    with pytest.raises(ValueError, match="Session.end()"):
        run.end()


def test_run_end_one_shot_deletes_thread_and_result_raises(
    saver: BaseCheckpointSaver[Any],
) -> None:
    llm = FakeLLM([LLMResponse(text="reply")])
    agent = make_react_agent(llm, tools=[], checkpointer=saver)
    run = agent.run("one-shot")
    assert run.result == "reply"

    run.end()

    # Result/snapshot raises RuntimeError after end()
    with pytest.raises(RuntimeError):
        _ = run.result

    cfg = {"configurable": {"thread_id": run.run_id}}
    assert len(list(agent.graph.get_state_history(cast("Any", cfg)))) == 0


def test_run_end_paused_refuses_without_abandon_and_ends_with_abandon(
    saver: BaseCheckpointSaver[Any],
) -> None:
    tools = [
        Tool(
            name="fail",
            description="always fails",
            parameters={"type": "object", "properties": {}},
            func=lambda args: "failed",
        )
    ]
    llm = FakeLLM([
        LLMResponse(text="", tool_calls=[ToolCall(id="c1", name="fail", arguments={})]),
    ])
    agent = make_guarded_react_agent(llm, tools=tools, checkpointer=saver, limit=1)
    run = agent.run("start")
    assert run.interrupted

    with pytest.raises(RuntimeError, match="abandon=True"):
        run.end()

    run.end(abandon=True)
    cfg = {"configurable": {"thread_id": run.run_id}}
    assert len(list(agent.graph.get_state_history(cast("Any", cfg)))) == 0


# Step 6: The message window (retain_messages and retain_window)


def test_retain_messages_ten_turns_bounded_and_no_leading_tool() -> None:
    echo_tool = Tool(
        name="echo",
        description="echoes",
        parameters={"type": "object", "properties": {"v": {"type": "string"}}},
        func=lambda args: f"echoed:{args.get('v')}",
    )
    # Each turn does a tool call then answers, so 2 model responses per turn, 3 messages per turn
    responses: list[LLMResponse] = []
    for i in range(10):
        responses.append(
            LLMResponse(
                text="",
                tool_calls=[ToolCall(id=f"call_{i}", name="echo", arguments={"v": str(i)})],
            )
        )
        responses.append(LLMResponse(text=f"reply_{i}"))

    llm = FakeLLM(responses)
    saver = MemorySaver()
    agent = make_react_agent(llm, tools=[echo_tool], checkpointer=saver, retain_messages=10)
    sess = agent.session("window-sess")

    for i in range(10):
        run = sess.send(f"task_{i}")
        assert run.status == "done"
        messages = cast("list[Any]", sess.snapshot.values.get("messages", []))
        # After each turn: at most 10 messages remain
        assert len(messages) <= 10
        # Never starts with a tool message
        assert messages[0].type != "tool"
        # Newest reply is retained
        assert messages[-1].content == f"reply_{i}"

    # Session dialog reflects only the retained window
    assert len(sess.dialog) <= 10


def test_retain_messages_capture_rows_survive_with_memory() -> None:
    from vsigma.memory import InMemoryStore
    from vsigma.observability import TrajectoryLog
    from vsigma.scope import episode

    store = InMemoryStore()
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    llm = FakeLLM([
        LLMResponse(text="reply 1"),
        LLMResponse(text="reply 2"),
        LLMResponse(text="reply 3"),
    ])
    agent = make_react_agent(
        llm,
        tools=[],
        interceptor=itc,
        checkpointer=MemorySaver(),
        retain_messages=2,
    )
    sess = agent.session("mem-window")
    r1 = sess.send("turn 1")
    r2 = sess.send("turn 2")
    r3 = sess.send("turn 3")

    # In thread state: messages are windowed
    messages = cast("list[Any]", sess.snapshot.values.get("messages", []))
    assert len(messages) <= 2

    # In memory store: every turn's capture rows survive in full
    for rid in [r1.run_id, r2.run_id, r3.run_id]:
        rows = store.query(episode(rid).namespace("context"))
        assert len(rows) >= 2  # input and act capture rows


def test_retain_messages_escalated_turn_removes_nothing() -> None:
    tools = [
        Tool(
            name="fail",
            description="always fails",
            parameters={"type": "object", "properties": {}},
            func=lambda args: "failed",
        )
    ]
    llm = FakeLLM([
        LLMResponse(text="", tool_calls=[ToolCall(id="c1", name="fail", arguments={})]),
    ])
    saver = MemorySaver()
    agent = make_guarded_react_agent(
        llm,
        tools=tools,
        checkpointer=saver,
        limit=1,
        retain_messages=1,
    )
    sess = agent.session("escalated-window")
    run = sess.send("start")
    assert run.interrupted
    assert sess.paused

    # On escalation, retain node did not execute, so messages are not trimmed
    messages = cast("list[Any]", sess.snapshot.values.get("messages", []))
    assert len(messages) > 1


def test_retain_messages_construction_raises_when_smaller_than_view() -> None:
    from vsigma.context import RecallContext, SlidingWindowContext

    llm = FakeLLM([])

    # Direct SlidingWindowContext with larger window
    itc1 = Interceptor(context=SlidingWindowContext(max_messages=20))
    with pytest.raises(ValueError, match="SlidingWindowContext"):
        make_react_agent(llm, tools=[], interceptor=itc1, retain_messages=10)

    # Nested SlidingWindowContext inside RecallContext
    itc2 = Interceptor(context=RecallContext(inner=SlidingWindowContext(max_messages=15)))
    with pytest.raises(ValueError, match="SlidingWindowContext"):
        make_guarded_react_agent(llm, tools=[], interceptor=itc2, retain_messages=10)


def test_retain_window_rejects_below_one() -> None:
    with pytest.raises(ValueError, match="max_messages"):
        retain_window(0)
    with pytest.raises(ValueError, match="max_messages"):
        retain_window(-2)
    with pytest.raises(ValueError, match="max_messages"):
        make_react_agent(FakeLLM([]), tools=[], retain_messages=0)
