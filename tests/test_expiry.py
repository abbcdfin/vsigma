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
    InMemoryStore,
    Interceptor,
    LLMResponse,
    ReActState,
    ThreadExpiry,
    call,
    make_guarded_react_agent,
    make_react_agent,
    say,
)
from vsigma.episodes import (
    EPISODE_STATUS_NS,
    get_episode_status,
    record_episode,
    record_episode_status,
)
from vsigma.expiry import checkpoint_id_to_datetime
from vsigma.threads import thread_lock


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


def test_first_turn_crashed_before_first_checkpoint_recovered_abandoned(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    store = InMemoryStore()
    record_episode(store, "crash-run", session_id="s-crash")
    with thread_lock(checkpointer, "s-crash") as locked:
        record_episode_status(store, "crash-run", "running", locked=locked)

    expiry = ThreadExpiry(
        checkpointer, ExpiryPolicy(idle_ttl=timedelta(days=1)), store=store
    )
    report = expiry.run()
    assert "crash-run" in report.abandoned_runs
    assert get_episode_status(store, "crash-run") == "abandoned"


def test_turn_in_progress_holding_lock_left_running(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    store = InMemoryStore()
    record_episode(store, "in-flight-run", session_id="s-flight")
    with thread_lock(checkpointer, "s-flight") as locked:
        record_episode_status(store, "in-flight-run", "running", locked=locked)
        expiry = ThreadExpiry(
            checkpointer, ExpiryPolicy(idle_ttl=timedelta(days=1)), store=store
        )
        report = expiry.run()

    assert "in-flight-run" not in report.abandoned_runs
    assert get_episode_status(store, "in-flight-run") == "running"


def test_expiry_deletes_paused_thread_and_records_run_abandoned(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    store = InMemoryStore()
    itc = Interceptor(memory=store)
    agent = make_guarded_react_agent(
        FakeLLM([call("missing_tool")]),
        [],
        interceptor=itc,
        checkpointer=checkpointer,
        limit=1,
    )
    run = agent.run("paused task", session_id="s-paused")
    assert run.status == "escalated"
    assert get_episode_status(store, run.run_id) == "escalated"

    policy = ExpiryPolicy(paused_idle_ttl=timedelta(seconds=1))
    expiry = ThreadExpiry(checkpointer, policy, store=store)
    now = datetime.now(UTC) + timedelta(seconds=10)

    report = expiry.run(now=now)
    assert any(e.thread_id == "s-paused" for e in report.expired)
    assert run.run_id in report.abandoned_runs
    assert get_episode_status(store, run.run_id) == "abandoned"


def test_escalated_run_whose_session_ran_another_turn_recorded_abandoned(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    store = InMemoryStore()
    itc = Interceptor(memory=store)
    agent = make_guarded_react_agent(
        FakeLLM([call("missing_tool"), say("turn 2 done")]),
        [],
        interceptor=itc,
        checkpointer=checkpointer,
        limit=1,
    )
    sess = agent.session("s-multi")
    run1 = sess.send("task 1")
    assert run1.status == "escalated"

    run2 = sess.send("task 2", abandon=True)
    assert run2.status == "done"

    # Simulate run1 status remaining escalated (e.g. crash before abandon recorded)
    with thread_lock(checkpointer, "s-multi"):
        store.write(EPISODE_STATUS_NS, run1.run_id, "escalated")

    # Another session currently paused
    agent_p = make_guarded_react_agent(
        FakeLLM([call("missing_tool")]),
        [],
        interceptor=itc,
        checkpointer=checkpointer,
        limit=1,
    )
    sess_p = agent_p.session("s-still-paused")
    run_p = sess_p.send("task paused")
    assert run_p.status == "escalated"

    # Run expiry sweep without expiring by TTL
    policy = ExpiryPolicy(idle_ttl=timedelta(days=10))
    expiry = ThreadExpiry(checkpointer, policy, store=store)
    report = expiry.run()

    # run1 thread moved on to run2, so run1 is abandoned
    assert run1.run_id in report.abandoned_runs
    assert get_episode_status(store, run1.run_id) == "abandoned"

    # run_p thread still holds the pause and latest run is run_p, so left escalated
    assert run_p.run_id not in report.abandoned_runs
    assert get_episode_status(store, run_p.run_id) == "escalated"


def test_dry_run_writes_no_status_and_reports_recovered(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    store = InMemoryStore()
    record_episode(store, "crash-dry", session_id="s-dry")
    with thread_lock(checkpointer, "s-dry") as locked:
        record_episode_status(store, "crash-dry", "running", locked=locked)

    itc = Interceptor(memory=store)
    agent = make_guarded_react_agent(
        FakeLLM([call("missing_tool")]),
        [],
        interceptor=itc,
        checkpointer=checkpointer,
        limit=1,
    )
    run_paused = agent.run("paused dry", session_id="s-paused-dry")
    assert get_episode_status(store, run_paused.run_id) == "escalated"

    policy = ExpiryPolicy(paused_idle_ttl=timedelta(seconds=1))
    expiry = ThreadExpiry(checkpointer, policy, store=store)
    now = datetime.now(UTC) + timedelta(seconds=10)

    report = expiry.run(now=now, dry_run=True)
    assert any(e.thread_id == "s-paused-dry" for e in report.expired)
    assert "crash-dry" in report.abandoned_runs
    assert run_paused.run_id in report.abandoned_runs

    # In dry run, store remains untouched
    assert get_episode_status(store, "crash-dry") == "running"
    assert get_episode_status(store, run_paused.run_id) == "escalated"


def test_status_with_no_index_entry_is_skipped_and_warned(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    store = InMemoryStore()
    store.write(EPISODE_STATUS_NS, "orphan-run", "running")

    expiry = ThreadExpiry(
        checkpointer, ExpiryPolicy(idle_ttl=timedelta(days=1)), store=store
    )
    report = expiry.run()
    assert "orphan-run" not in report.abandoned_runs
    assert get_episode_status(store, "orphan-run") == "running"


def test_sweep_rechecks_status_under_lock_preventing_stale_abandon(
    monkeypatch: pytest.MonkeyPatch,
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    store = InMemoryStore()
    itc = Interceptor(memory=store)
    agent = make_guarded_react_agent(
        FakeLLM([call("missing_tool")]),
        [],
        interceptor=itc,
        checkpointer=checkpointer,
        limit=1,
    )
    sess = agent.session("s-sweep-race")

    run_id = "race-run"

    def simulate_turn_completing(s: Any, rid: str) -> Any:
        if rid == run_id:
            agent.run("task", run_id=run_id, session_id="s-sweep-race")
        from vsigma.episodes import episode_owner as real_owner

        return real_owner(s, rid)

    store.write(EPISODE_STATUS_NS, run_id, "running")
    monkeypatch.setattr("vsigma.expiry.episode_owner", simulate_turn_completing)

    expiry = ThreadExpiry(
        checkpointer, ExpiryPolicy(idle_ttl=timedelta(days=1)), store=store
    )
    report = expiry.run()

    assert run_id not in report.abandoned_runs
    assert get_episode_status(store, run_id) == "escalated"
    assert sess.paused is True


def test_run_ttl_alone(checkpointer: BaseCheckpointSaver[Any]) -> None:
    llm = FakeLLM([LLMResponse(text=f"reply-{i}") for i in range(10)])
    agent = make_react_agent(llm, tools=[], checkpointer=checkpointer)
    agent.run("t1", session_id="s1")
    agent.run("t2", run_id="r1")

    now = datetime.now(UTC) + timedelta(days=2)
    policy = ExpiryPolicy(run_ttl=timedelta(days=1))
    expiry = ThreadExpiry(checkpointer, policy)

    rep = expiry.run(now=now)
    assert [e.thread_id for e in rep.expired] == ["r1"]
    threads = {t.thread_id for t in expiry.threads(now=now)}
    assert threads == {"s1"}


def test_two_limits_at_once(checkpointer: BaseCheckpointSaver[Any]) -> None:
    llm = FakeLLM([LLMResponse(text=f"reply-{i}") for i in range(10)])
    agent = make_react_agent(llm, tools=[], checkpointer=checkpointer)
    agent.run("a1", session_id="s1", user_id="u1")
    agent.run("a2", session_id="s2", user_id="u1")
    agent.run("a3", session_id="s3", user_id="u1")
    agent.run("r1", run_id="r1", user_id="u1")

    now = datetime.now(UTC) + timedelta(days=2)
    policy = ExpiryPolicy(keep_last=2, run_ttl=timedelta(days=1))
    expiry = ThreadExpiry(checkpointer, policy)
    rep = expiry.run(now=now)

    reasons = {e.thread_id: e.reason for e in rep.expired}
    assert reasons == {"s1": "keep_last", "r1": "run_ttl"}
    threads = {t.thread_id for t in expiry.threads(now=now)}
    assert threads == {"s2", "s3"}


def test_thread_under_limit_is_untouched_while_another_expires(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    llm = FakeLLM([LLMResponse(text=f"reply-{i}") for i in range(10)])
    agent = make_react_agent(llm, tools=[], checkpointer=checkpointer)

    agent.run("s", session_id="s1")
    agent.run("r", run_id="r1")

    now = datetime.now(UTC) + timedelta(days=2)
    policy = ExpiryPolicy(idle_ttl=timedelta(days=10), run_ttl=timedelta(days=1))
    expiry = ThreadExpiry(checkpointer, policy)

    rep = expiry.run(now=now)
    assert [e.thread_id for e in rep.expired] == ["r1"]
    threads = {t.thread_id for t in expiry.threads(now=now)}
    assert threads == {"s1"}


