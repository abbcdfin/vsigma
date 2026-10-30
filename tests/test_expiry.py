"""Tests for thread expiry (Phase 4.12, Step 2): ThreadInfo and ThreadExpiry.threads()."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START, StateGraph
from langgraph.types import interrupt

from vsigma import (
    Agent,
    ExpiryPolicy,
    FakeLLM,
    Interceptor,
    LLMResponse,
    ReActState,
    ThreadExpiry,
    make_guarded_react_agent,
    make_react_agent,
)
from vsigma.expiry import checkpoint_id_to_datetime


def _make_sqlite_saver() -> SqliteSaver:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


@pytest.fixture(params=["memory", "sqlite"])
def checkpointer(request: pytest.FixtureRequest) -> BaseCheckpointSaver[Any]:
    if request.param == "memory":
        return InMemorySaver()
    return _make_sqlite_saver()


def test_checkpoint_id_to_datetime_matches_ts(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    llm = FakeLLM([LLMResponse(text="reply")])
    agent = make_react_agent(llm, tools=[], checkpointer=checkpointer)
    agent.run("hello", session_id="s1")

    # Read latest checkpoint directly to compare ts with decoded timestamp
    state = agent.graph.get_state({"configurable": {"thread_id": "s1"}})
    cid = state.config["configurable"]["checkpoint_id"]
    # StateSnapshot does not expose raw ts directly, but checkpointer get_tuple does
    cp_tuple = checkpointer.get_tuple({"configurable": {"thread_id": "s1"}})
    assert cp_tuple is not None
    ts_iso = cp_tuple.checkpoint["ts"]
    dt_actual = datetime.fromisoformat(ts_iso)

    dt_decoded = checkpoint_id_to_datetime(cid)
    diff_ms = abs((dt_decoded - dt_actual).total_seconds()) * 1000
    assert diff_ms < 50.0, f"Decoded timestamp drifted by {diff_ms:.2f}ms"


def test_checkpoint_id_to_datetime_matches_ts_for_subgraph(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    sub: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)

    def sub_node(state: ReActState) -> dict[str, object]:
        ans = interrupt("subgraph question")
        return {"result": ans}

    sub.add_node("ask", sub_node)
    sub.add_edge(START, "ask")

    parent: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    parent.add_node("sub", sub.compile())
    parent.add_edge(START, "sub")

    agent = Agent(parent.compile(checkpointer=checkpointer), Interceptor())
    sess = agent.session("s-sub")
    sess.send("start")

    # Both root and subgraph checkpoints match their ts
    root_tuple = checkpointer.get_tuple({"configurable": {"thread_id": "s-sub"}})
    assert root_tuple is not None
    root_cid = root_tuple.checkpoint["id"]
    root_ts = datetime.fromisoformat(root_tuple.checkpoint["ts"])
    diff_root = abs((checkpoint_id_to_datetime(root_cid) - root_ts).total_seconds()) * 1000
    assert diff_root < 50.0

    # Subgraph namespace checkpoint
    history = list(checkpointer.list({"configurable": {"thread_id": "s-sub"}}))
    sub_tuples = [t for t in history if t.config.get("configurable", {}).get("checkpoint_ns")]
    assert len(sub_tuples) > 0
    sub_cid = sub_tuples[0].checkpoint["id"]
    sub_ts = datetime.fromisoformat(sub_tuples[0].checkpoint["ts"])
    diff_sub = abs((checkpoint_id_to_datetime(sub_cid) - sub_ts).total_seconds()) * 1000
    assert diff_sub < 50.0


def test_threads_scanning_metadata_and_pauses(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    # 1. A finished session (s-fin) with user_id="alice"
    llm1 = FakeLLM([LLMResponse(text="session finished")])
    agent1 = make_react_agent(llm1, tools=[], checkpointer=checkpointer)
    run1 = agent1.run("hi", session_id="s-fin", user_id="alice")

    # 2. A one-shot run (r-oneshot) with user_id="bob"
    llm2 = FakeLLM([LLMResponse(text="oneshot finished")])
    agent2 = make_react_agent(llm2, tools=[], checkpointer=checkpointer)
    agent2.run("oneshot task", run_id="r-oneshot", user_id="bob")

    # 3. A paused session (s-paused)
    from vsigma import Tool, ToolCall
    fail_tool = Tool(
        name="fail",
        description="fails",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "error",
    )
    llm3 = FakeLLM([
        LLMResponse(text="", tool_calls=[ToolCall(id="c1", name="fail", arguments={})])
    ])
    agent3 = make_guarded_react_agent(llm3, tools=[fail_tool], checkpointer=checkpointer, limit=1)
    run3 = agent3.session("s-paused", user_id="charlie").send("pause please")
    assert run3.interrupted

    # 4. A session paused inside a subgraph (s-sub-paused)
    sub: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    sub.add_node("sub_ask", lambda s: {"result": interrupt("which item?")})
    sub.add_edge(START, "sub_ask")
    parent: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    parent.add_node("sub_node", sub.compile())
    parent.add_edge(START, "sub_node")
    agent4 = Agent(parent.compile(checkpointer=checkpointer), Interceptor())
    agent4.session("s-sub-paused", user_id="dave").send("go")

    now = datetime.now(UTC)
    policy = ExpiryPolicy(idle_ttl=None, keep_last=10)
    expiry = ThreadExpiry(checkpointer, policy)

    threads = {info.thread_id: info for info in expiry.threads(now=now)}
    assert "s-fin" in threads
    assert "r-oneshot" in threads
    assert "s-paused" in threads
    assert "s-sub-paused" in threads

    # Verify s-fin
    t_fin = threads["s-fin"]
    assert t_fin.kind == "session"
    assert t_fin.user_id == "alice"
    assert t_fin.run_id == run1.run_id
    assert not t_fin.paused
    assert t_fin.idle.total_seconds() >= 0

    # Verify r-oneshot
    t_one = threads["r-oneshot"]
    assert t_one.kind == "run"
    assert t_one.user_id == "bob"
    assert t_one.run_id == "r-oneshot"
    assert not t_one.paused
    assert t_one.idle.total_seconds() >= 0

    # Verify s-paused
    t_paused = threads["s-paused"]
    assert t_paused.kind == "session"
    assert t_paused.user_id == "charlie"
    assert t_paused.run_id == run3.run_id
    assert t_paused.paused

    # Verify s-sub-paused (paused inside subgraph)
    t_sub = threads["s-sub-paused"]
    assert t_sub.kind == "session"
    assert t_sub.user_id == "dave"
    assert t_sub.paused


def test_unsupported_checkpointer_raises() -> None:
    class DummySaver:
        pass

    policy = ExpiryPolicy(keep_last=5)
    with pytest.raises(NotImplementedError, match="DummySaver"):
        ThreadExpiry(DummySaver(), policy)
