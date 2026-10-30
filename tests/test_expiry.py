"""Tests for thread expiry (Phase 4.12, Step 2): ThreadInfo and ThreadExpiry.threads()."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
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


class BlockingLLM:
    """An LLM that signals `entered` when called and blocks until `release` is set."""

    def __init__(self, responses: list[LLMResponse] | None = None) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._responses = list(responses or [LLMResponse(text="blocking reply")])
        self._idx = 0
        self._lock = threading.Lock()

    def complete(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> LLMResponse:
        self.entered.set()
        self.release.wait(timeout=5.0)
        with self._lock:
            resp = self._responses[min(self._idx, len(self._responses) - 1)]
            self._idx += 1
            return resp


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


def test_expiry_policy_validation() -> None:
    # Empty policy raises
    with pytest.raises(ValueError, match="at least one limit"):
        ExpiryPolicy()

    # Non-positive duration raises
    with pytest.raises(ValueError, match="idle_ttl must be positive"):
        ExpiryPolicy(idle_ttl=timedelta(0))
    with pytest.raises(ValueError, match="paused_idle_ttl must be positive"):
        ExpiryPolicy(paused_idle_ttl=timedelta(seconds=-5))
    with pytest.raises(ValueError, match="run_ttl must be positive"):
        ExpiryPolicy(run_ttl=timedelta(seconds=-1))

    # keep_last < 1 raises
    with pytest.raises(ValueError, match="keep_last must be >= 1"):
        ExpiryPolicy(keep_last=0)


def test_expiry_limits_alone_and_combined(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    llm = FakeLLM([LLMResponse(text=f"reply-{i}") for i in range(10)])
    agent = make_react_agent(llm, tools=[], checkpointer=checkpointer)

    agent.run("t1", session_id="s1")
    agent.run("t2", session_id="s2")
    agent.run("t3", run_id="r1")

    # Fast forward time to test idle_ttl
    now = datetime.now(UTC) + timedelta(days=2)

    # 1. idle_ttl alone: expires both sessions and runs (since run_ttl defaults to idle_ttl)
    policy_idle = ExpiryPolicy(idle_ttl=timedelta(days=1))
    expiry_idle = ThreadExpiry(checkpointer, policy_idle)
    threads = {t.thread_id: t for t in expiry_idle.threads(now=now)}
    assert len(threads) == 3

    # dry_run reports candidates without deleting
    rep_dry = expiry_idle.run(now=now, dry_run=True)
    assert len(rep_dry.expired) == 3
    assert len(list(expiry_idle.threads(now=now))) == 3

    # real run deletes them
    rep_real = expiry_idle.run(now=now)
    assert len(rep_real.expired) == 3
    assert len(list(expiry_idle.threads(now=now))) == 0


def test_keep_last_per_user_and_anonymous(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    llm = FakeLLM([LLMResponse(text=f"reply-{i}") for i in range(20)])
    agent = make_react_agent(llm, tools=[], checkpointer=checkpointer)

    # Alice has 3 sessions
    agent.run("a1", session_id="alice-1", user_id="alice")
    agent.run("a2", session_id="alice-2", user_id="alice")
    agent.run("a3", session_id="alice-3", user_id="alice")

    # Bob has 2 sessions
    agent.run("b1", session_id="bob-1", user_id="bob")
    agent.run("b2", session_id="bob-2", user_id="bob")

    # Anonymous (None) has 3 sessions
    agent.run("anon1", session_id="anon-1", user_id=None)
    agent.run("anon2", session_id="anon-2", user_id=None)
    agent.run("anon3", session_id="anon-3", user_id=None)

    # One-shot run
    agent.run("one", run_id="r-one", user_id="alice")

    # keep_last=2 alone:
    # Alice keeps alice-3 and alice-2; alice-1 expires
    # Bob has 2 -> none expires
    # Anonymous keeps anon-3 and anon-2; anon-1 expires
    # r-one (run) is exempt from keep_last -> does not expire
    policy = ExpiryPolicy(keep_last=2)
    expiry = ThreadExpiry(checkpointer, policy)

    report = expiry.run()
    expired_ids = {e.thread_id for e in report.expired}
    assert expired_ids == {"alice-1", "anon-1"}

    remaining = {t.thread_id for t in expiry.threads()}
    assert remaining == {"alice-3", "alice-2", "bob-1", "bob-2", "anon-3", "anon-2", "r-one"}


def test_paused_thread_exempt_from_keep_last(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    from vsigma import Tool, ToolCall
    fail_tool = Tool(
        name="fail",
        description="fails",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "err",
    )
    llm = FakeLLM([
        LLMResponse(text="", tool_calls=[ToolCall(id="c1", name="fail", arguments={})]),
        LLMResponse(text="reply u1"),
        LLMResponse(text="reply u2"),
    ])
    agent = make_guarded_react_agent(llm, tools=[fail_tool], checkpointer=checkpointer, limit=1)

    # Create 1 paused session and 2 unpaused sessions for Alice
    agent.session("alice-paused", user_id="alice").send("pause")
    agent.run("work 1", session_id="alice-u1", user_id="alice")
    agent.run("work 2", session_id="alice-u2", user_id="alice")

    policy = ExpiryPolicy(keep_last=1)
    expiry = ThreadExpiry(checkpointer, policy)

    report = expiry.run()
    expired_ids = {e.thread_id for e in report.expired}
    # Only alice-u1 expires! alice-paused is exempt from keep_last
    assert expired_ids == {"alice-u1"}

    remaining = {t.thread_id for t in expiry.threads()}
    assert remaining == {"alice-paused", "alice-u2"}


def test_turn_in_progress_is_skipped_and_survives(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    llm = BlockingLLM([LLMResponse(text="reply 1"), LLMResponse(text="reply 2")])
    agent = make_react_agent(llm, tools=[], checkpointer=checkpointer)
    sess = agent.session("s-in-flight")

    # Turn 1 writes initial checkpoint to thread
    sess.send("turn 1")
    llm.entered.clear()

    # Turn 2 starts and blocks in LLM
    t1 = threading.Thread(target=lambda: sess.send("turn 2"))
    t1.start()
    assert llm.entered.wait(timeout=5.0)

    try:
        # While turn 2 is in progress, expiry runs with idle_ttl=1 day
        now = datetime.now(UTC) + timedelta(days=2)
        policy = ExpiryPolicy(idle_ttl=timedelta(days=1))
        expiry = ThreadExpiry(checkpointer, policy)

        report = expiry.run(now=now)
        assert report.skipped_busy == ["s-in-flight"]
        assert report.expired == []
    finally:
        # Release turn 2
        llm.release.set()
        t1.join(timeout=5.0)

    # Turn completed and messages survive intact
    assert len(sess.dialog) == 4
    assert sess.dialog[1]["content"] == "reply 1"
    assert sess.dialog[3]["content"] == "reply 2"


def test_checkpoint_changed_after_scan_is_skipped(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    llm = FakeLLM([LLMResponse(text="reply 1"), LLMResponse(text="reply 2")])
    agent = make_react_agent(llm, tools=[], checkpointer=checkpointer)
    sess = agent.session("s-changed")
    sess.send("turn 1")

    now = datetime.now(UTC) + timedelta(days=2)
    policy = ExpiryPolicy(idle_ttl=timedelta(days=1))
    expiry = ThreadExpiry(checkpointer, policy)

    # Scan candidates
    candidates = list(expiry.threads(now=now))
    assert len(candidates) == 1
    old_info = candidates[0]

    # Before expiry deletes it, another turn runs on that session, advancing the checkpoint
    sess.send("turn 2")

    # Running deletion with old_info's checkpoint_id sees mismatch
    from vsigma.threads import thread_lock
    with thread_lock(checkpointer, "s-changed") as locked:
        deleted = expiry._delete_thread_locked(locked, old_info.checkpoint_id)
        assert not deleted

    # Thread still exists and has turn 2
    assert len(sess.dialog) == 4

