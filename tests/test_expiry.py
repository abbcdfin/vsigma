"""Tests for thread expiry (Phase 4.12, Step 2): ThreadInfo and ThreadExpiry.threads()."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Any, cast

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
    assert diff_ms < 500.0, f"Decoded timestamp drifted by {diff_ms:.2f}ms"


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
    assert diff_root < 500.0

    # Subgraph namespace checkpoint
    history = list(checkpointer.list({"configurable": {"thread_id": "s-sub"}}))
    sub_tuples = [t for t in history if t.config.get("configurable", {}).get("checkpoint_ns")]
    assert len(sub_tuples) > 0
    sub_cid = sub_tuples[0].checkpoint["id"]
    sub_ts = datetime.fromisoformat(sub_tuples[0].checkpoint["ts"])
    diff_sub = abs((checkpoint_id_to_datetime(sub_cid) - sub_ts).total_seconds()) * 1000
    assert diff_sub < 500.0


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


# ---------------------------------------------------------------------------
# Phase 4.16: Forgetting session memory (scope-lifespans §C.2, thread-expiry §B.3.2)
# ---------------------------------------------------------------------------


def test_thread_expiry_unsupported_store_raises_type_error(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    from vsigma.memory import MemoryStore

    class NonMaintainableStore(MemoryStore):
        def read(self, namespace: str, key: str) -> object | None:
            return None

        def write(self, namespace: str, key: str, value: object) -> None:
            pass

        def query(self, namespace: str) -> list[tuple[str, object]]:
            return []

    with pytest.raises(TypeError, match="MaintainableStore"):
        ThreadExpiry(
            checkpointer,
            ExpiryPolicy(idle_ttl=timedelta(days=1)),
            store=NonMaintainableStore(),
        )


def test_expiry_with_store_forgets_expired_session_and_detaches_episodes(
    checkpointer: BaseCheckpointSaver[Any],
    tmp_path: Any,
) -> None:
    from vsigma import PersistentStore, reflexive_consolidator, session
    from vsigma.episodes import (
        SESSIONS_FORGET_NS,
        episode_owner,
    )

    store = PersistentStore(tmp_path / "forget.db")
    distiller = FakeLLM([
        say('{"content": "session fact", "source_ref": "trajectory:r1"}'),
    ])
    itc = Interceptor(
        memory=store,
        run_end=reflexive_consolidator(distiller),
    )
    agent = make_react_agent(
        FakeLLM([say("s1 reply"), say("r1 reply")]),
        [],
        interceptor=itc,
        checkpointer=checkpointer,
    )

    run_s1 = agent.run("hello s1", session_id="s1", user_id="alice", task_id="t_s1")
    run_r1 = agent.run("hello r1", run_id="r1", user_id="alice")

    # Before expiry: session context and knowledge exist, and episode is attached to s1
    assert len(store.query(session("s1").namespace("context"))) == 1
    assert len(store.query(session("s1").namespace("knowledge"))) == 1
    owner_before = episode_owner(store, run_s1.run_id)
    assert owner_before is not None and owner_before.session_id == "s1"

    now = datetime.now(UTC) + timedelta(days=2)
    expiry = ThreadExpiry(
        checkpointer,
        ExpiryPolicy(idle_ttl=timedelta(days=1)),
        store=store,
    )
    report = expiry.run(now=now)

    # s1 was forgotten; one-shot r1 expired without being in forgotten_sessions
    assert report.forgotten_sessions == ["s1"]
    assert {e.thread_id for e in report.expired} == {"s1", "r1"}
    assert store.read(SESSIONS_FORGET_NS, "s1") is None

    # Session context and knowledge are deleted
    assert store.query(session("s1").namespace("context")) == []
    assert store.query(session("s1").namespace("knowledge")) == []

    # Episode transcript survives, and episode is detached from s1 (keeping user_id and task_id)
    owner_after = episode_owner(store, run_s1.run_id)
    assert owner_after is not None
    assert owner_after.session_id is None
    assert owner_after.user_id == "alice"
    assert owner_after.task_id == "t_s1"
    assert len(store.query(f"episode:{run_s1.run_id}/context")) > 0
    assert episode_owner(store, run_r1.run_id) is not None
    store.close()


def test_forget_failure_after_thread_delete_completed_by_next_run(
    checkpointer: BaseCheckpointSaver[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vsigma.expiry as expiry_mod
    from vsigma import reflexive_consolidator, session
    from vsigma.episodes import (
        SESSIONS_FORGET_NS,
        episode_owner,
    )

    store = InMemoryStore()
    distiller = FakeLLM([
        say('{"content": "s1 fact", "source_ref": "trajectory:r"}'),
    ])
    itc = Interceptor(memory=store, run_end=reflexive_consolidator(distiller))
    agent = make_react_agent(
        FakeLLM([say("done")]),
        [],
        interceptor=itc,
        checkpointer=checkpointer,
    )
    run1 = agent.run("task 1", session_id="s1", user_id="alice")

    assert len(store.query(session("s1").namespace("context"))) == 1
    assert len(store.query(session("s1").namespace("knowledge"))) == 1

    orig_del_mem = expiry_mod._delete_session_memory

    def crash_after_thread_delete(st: Any, sid: str) -> None:
        raise RuntimeError("simulated crash after thread delete")

    monkeypatch.setattr(expiry_mod, "_delete_session_memory", crash_after_thread_delete)

    now = datetime.now(UTC) + timedelta(days=2)
    expiry = ThreadExpiry(
        checkpointer,
        ExpiryPolicy(idle_ttl=timedelta(days=1)),
        store=store,
    )
    with pytest.raises(RuntimeError, match="simulated crash after thread delete"):
        expiry.run(now=now)

    # Thread is already deleted, forget record remains, memory & episode attachment still present
    assert list(expiry.threads(now=now)) == []
    assert store.read(SESSIONS_FORGET_NS, "s1") is not None
    assert len(store.query(session("s1").namespace("context"))) == 1
    assert len(store.query(session("s1").namespace("knowledge"))) == 1
    owner_mid = episode_owner(store, run1.run_id)
    assert owner_mid is not None and owner_mid.session_id == "s1"

    # Restore and run next expiry.run() — completes the forget record
    monkeypatch.setattr(expiry_mod, "_delete_session_memory", orig_del_mem)
    report = expiry.run(now=now)

    assert report.forgotten_sessions == ["s1"]
    assert store.read(SESSIONS_FORGET_NS, "s1") is None
    assert store.query(session("s1").namespace("context")) == []
    assert store.query(session("s1").namespace("knowledge")) == []
    owner_done = episode_owner(store, run1.run_id)
    assert owner_done is not None and owner_done.session_id is None


def test_forget_failure_before_thread_delete_completed_by_turn(
    checkpointer: BaseCheckpointSaver[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vsigma import RecallContext, reflexive_consolidator, session
    from vsigma.episodes import (
        SESSIONS_FORGET_NS,
        episode_owner,
    )

    store = InMemoryStore()
    distiller = FakeLLM([
        say('{"content": "secret old fact", "source_ref": "trajectory:r1"}'),
        say('{"content": "new fact", "source_ref": "trajectory:r2"}'),
    ])
    seen_prompts: list[list[dict[str, object]]] = []

    class InspectingLLM:
        def __init__(self, replies: list[str]) -> None:
            self._replies = list(replies)

        def complete(
            self,
            messages: list[dict[str, object]],
            tools: list[dict[str, object]],
        ) -> LLMResponse:
            seen_prompts.append(list(messages))
            return LLMResponse(text=self._replies.pop(0))

    itc = Interceptor(
        memory=store,
        context=RecallContext(max_facts=8, min_confidence=0.1),
        run_end=reflexive_consolidator(distiller),
    )
    agent = make_react_agent(
        InspectingLLM(["old answer", "new answer"]),
        [],
        interceptor=itc,
        checkpointer=checkpointer,
    )
    sess = agent.session("s1", user_id="alice")
    run1 = sess.send("old question")
    assert len(sess.dialog) == 2
    assert len(store.query(session("s1").namespace("knowledge"))) == 1

    # Simulate crash before thread delete during expiry.run()
    now = datetime.now(UTC) + timedelta(days=2)
    expiry = ThreadExpiry(
        checkpointer,
        ExpiryPolicy(idle_ttl=timedelta(days=1)),
        store=store,
    )

    def crash_before_thread_delete(locked: Any, expected_cid: str) -> bool:
        raise RuntimeError("simulated crash before thread delete")

    monkeypatch.setattr(expiry, "_delete_thread_locked", crash_before_thread_delete)
    with pytest.raises(RuntimeError, match="simulated crash before thread delete"):
        expiry.run(now=now)

    # Forget record was written, but thread and old memory are still present
    assert store.read(SESSIONS_FORGET_NS, "s1") is not None
    assert len(list(expiry.threads(now=now))) == 1
    assert len(store.query(session("s1").namespace("knowledge"))) == 1

    # Now a turn on that session completes the record first,
    # starting with no thread and no old memory
    run2 = sess.send("new question")

    assert store.read(SESSIONS_FORGET_NS, "s1") is None
    # Dialog has ONLY the new turn (2 messages, not 4)
    assert sess.dialog == [
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new answer"},
    ]
    # Prompt sent to LLM on turn 2 had NO old messages and NO recalled "secret old fact"
    turn2_prompt_text = str(seen_prompts[-1])
    assert "old question" not in turn2_prompt_text
    assert "secret old fact" not in turn2_prompt_text

    # Old episode is detached; new episode belongs to s1
    o1 = episode_owner(store, run1.run_id)
    o2 = episode_owner(store, run2.run_id)
    assert o1 is not None and o1.session_id is None
    assert o2 is not None and o2.session_id == "s1"


def test_forget_failure_before_thread_delete_completed_by_resume(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    from vsigma import PauseChanged, Tool, ToolCall
    from vsigma.episodes import (
        SESSIONS_FORGET_NS,
        episode_owner,
    )

    store = InMemoryStore()
    fail_tool = Tool(
        name="fail",
        description="fails",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "err",
    )
    agent = make_guarded_react_agent(
        FakeLLM([
            LLMResponse(
                text="", tool_calls=[ToolCall(id="c1", name="fail", arguments={})]
            )
        ]),
        tools=[fail_tool],
        interceptor=Interceptor(memory=store),
        checkpointer=checkpointer,
        limit=1,
    )
    sess = agent.session("s-paused-forget", user_id="alice")
    run1 = sess.send("pause me")
    assert sess.paused
    qid = sess.interrupt_id

    # Write a forget record with the paused thread's current checkpoint id
    # (simulating a crash before step 2)
    info = next(
        t
        for t in ThreadExpiry(checkpointer).threads()
        if t.thread_id == "s-paused-forget"
    )
    store.write(SESSIONS_FORGET_NS, "s-paused-forget", info.checkpoint_id)

    # Returning user calls resume -> completes forget record first, finds session ended
    with pytest.raises(PauseChanged) as exc_info:
        sess.resume("answer", interrupt_id=qid)
    assert exc_info.value.interrupt_id is None

    assert store.read(SESSIONS_FORGET_NS, "s-paused-forget") is None
    assert not sess.paused
    assert get_episode_status(store, run1.run_id) == "abandoned"
    owner = episode_owner(store, run1.run_id)
    assert owner is not None and owner.session_id is None


def test_forget_record_whose_thread_moved_on_is_dropped_and_deletes_nothing(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    from vsigma import reflexive_consolidator, session
    from vsigma.episodes import (
        SESSIONS_FORGET_NS,
        episode_owner,
    )

    store = InMemoryStore()
    distiller = FakeLLM([
        say('{"content": "fact 1", "source_ref": "trajectory:r1"}'),
        say('{"content": "fact 2", "source_ref": "trajectory:r2"}'),
        say('{"content": "fact 3", "source_ref": "trajectory:r3"}'),
    ])
    itc = Interceptor(memory=store, run_end=reflexive_consolidator(distiller))
    agent = make_react_agent(
        FakeLLM([say("reply 1"), say("reply 2"), say("reply 3")]),
        [],
        interceptor=itc,
        checkpointer=checkpointer,
    )
    sess = agent.session("s-moved", user_id="alice")
    run1 = sess.send("turn 1")

    expiry = ThreadExpiry(
        checkpointer,
        ExpiryPolicy(idle_ttl=timedelta(days=10)),
        store=store,
    )
    cid1 = next(t.checkpoint_id for t in expiry.threads() if t.thread_id == "s-moved")

    # Thread moves on to turn 2 (cid2 != cid1)
    run2 = sess.send("turn 2")
    cid2 = next(t.checkpoint_id for t in expiry.threads() if t.thread_id == "s-moved")
    assert cid2 != cid1

    # Stale forget record pointing at cid1
    store.write(SESSIONS_FORGET_NS, "s-moved", cid1)

    # 1. expiry.run() drops the stale record and deletes nothing
    report = expiry.run()
    assert "s-moved" not in report.forgotten_sessions
    assert store.read(SESSIONS_FORGET_NS, "s-moved") is None
    assert len(sess.dialog) == 4
    assert len(store.query(session("s-moved").namespace("context"))) == 2
    assert len(store.query(session("s-moved").namespace("knowledge"))) == 2
    o1 = episode_owner(store, run1.run_id)
    o2 = episode_owner(store, run2.run_id)
    assert o1 is not None and o1.session_id == "s-moved"
    assert o2 is not None and o2.session_id == "s-moved"

    # 2. A turn on the session also drops a stale record and deletes nothing
    store.write(SESSIONS_FORGET_NS, "s-moved", cid1)
    sess.send("turn 3")
    assert store.read(SESSIONS_FORGET_NS, "s-moved") is None
    assert len(sess.dialog) == 6
    assert len(store.query(session("s-moved").namespace("context"))) == 3


def test_session_id_reused_before_completion_starts_with_no_old_memory(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    import vsigma.expiry as expiry_mod
    from vsigma import RecallContext, reflexive_consolidator
    from vsigma.episodes import SESSIONS_FORGET_NS

    store = InMemoryStore()
    distiller = FakeLLM([
        say('{"content": "stale session secret", "source_ref": "trajectory:r1"}'),
        say('{"content": "fresh session fact", "source_ref": "trajectory:r2"}'),
    ])
    prompts: list[list[dict[str, object]]] = []

    class RecordingLLM:
        def __init__(self, replies: list[str]) -> None:
            self._replies = list(replies)

        def complete(
            self,
            messages: list[dict[str, object]],
            tools: list[dict[str, object]],
        ) -> LLMResponse:
            prompts.append(list(messages))
            return LLMResponse(text=self._replies.pop(0))

    itc = Interceptor(
        memory=store,
        context=RecallContext(max_facts=8, min_confidence=0.1),
        run_end=reflexive_consolidator(distiller),
    )
    agent = make_react_agent(
        RecordingLLM(["first reply", "reused reply"]),
        [],
        interceptor=itc,
        checkpointer=checkpointer,
    )
    agent.session("s-reuse", user_id="alice").send("first turn")

    # Crash expiry after thread delete (step 2), leaving old session memory and forget record
    orig_del_mem = expiry_mod._delete_session_memory
    expiry_mod._delete_session_memory = lambda st, sid: (_ for _ in ()).throw(  # type: ignore[assignment]
        RuntimeError("crash after step 2")
    )
    try:
        expiry = ThreadExpiry(
            checkpointer,
            ExpiryPolicy(idle_ttl=timedelta(days=1)),
            store=store,
        )
        with pytest.raises(RuntimeError, match="crash after step 2"):
            expiry.run(now=datetime.now(UTC) + timedelta(days=2))
    finally:
        expiry_mod._delete_session_memory = orig_del_mem

    assert store.read(SESSIONS_FORGET_NS, "s-reuse") is not None

    # Reuse session id before expiry.run() completes the record
    agent.session("s-reuse", user_id="alice").send("reused turn")

    assert store.read(SESSIONS_FORGET_NS, "s-reuse") is None
    reused_prompt = str(prompts[-1])
    assert "stale session secret" not in reused_prompt
    assert "first turn" not in reused_prompt


def test_after_forgetting_turn_on_reused_id_has_no_old_turns_in_distill_source(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    from vsigma import episode_trajectory, reflexive_consolidator

    store = InMemoryStore()
    itc = Interceptor(memory=store, run_end=reflexive_consolidator(None))
    agent = make_react_agent(
        FakeLLM([say("ans 1"), say("ans 2"), say("ans 3")]),
        [],
        interceptor=itc,
        checkpointer=checkpointer,
    )

    sess = agent.session("s-distill", user_id="alice")
    run1 = sess.send("secret old turn 1")

    # Forget s-distill via expiry
    expiry = ThreadExpiry(
        checkpointer,
        ExpiryPolicy(idle_ttl=timedelta(days=1)),
        store=store,
    )
    expiry.run(now=datetime.now(UTC) + timedelta(days=2))

    # Reuse s-distill for new turns
    run2 = sess.send("new turn 2")
    run3 = sess.send("new turn 3")

    _, traj2 = episode_trajectory(store, run2.run_id)
    assert f"prior_context:{run1.run_id}" not in traj2
    assert "secret old turn 1" not in traj2

    _, traj3 = episode_trajectory(store, run3.run_id)
    assert f"prior_context:{run1.run_id}" not in traj3
    assert "secret old turn 1" not in traj3
    assert f"prior_context:{run2.run_id}" in traj3


def test_after_forgetting_memory_pass_writes_nothing_to_session_and_routes_user_to_persona(
    checkpointer: BaseCheckpointSaver[Any],
    tmp_path: Any,
) -> None:
    from vsigma import (
        PersistentStore,
        RetentionPolicy,
        persona,
        reflexive_consolidator,
        session,
    )
    from vsigma.episodes import episode_owner
    from vsigma.retention_episodes import _capture_rows

    store = PersistentStore(tmp_path / "pass_after_forget.db")
    reflexive_llm = FakeLLM([
        say('{"content": "anon reflexive", "source_ref": "trajectory:r1"}'),
        say('{"content": "alice reflexive", "source_ref": "trajectory:r2"}'),
    ])
    itc = Interceptor(memory=store, run_end=reflexive_consolidator(reflexive_llm))
    agent = make_react_agent(
        FakeLLM([say("anon reply"), say("alice reply")]),
        [],
        interceptor=itc,
        checkpointer=checkpointer,
    )

    run_anon = agent.run("anon task", session_id="s-anon", user_id=None)
    run_alice = agent.run("alice task", session_id="s-alice", user_id="alice")

    # Both sessions have reflexive knowledge before expiry
    assert len(store.query(session("s-anon").namespace("knowledge"))) == 1
    assert len(store.query(session("s-alice").namespace("knowledge"))) == 1

    now = datetime.now(UTC) + timedelta(days=2)
    distiller = FakeLLM([
        say('{"content": "alice deep persona fact", "source_ref": "trajectory:r2"}'),
    ])

    # Run maintain with expiry + distiller + retention
    # (max_age=1s so both episodes are due for pruning)
    report = agent.maintain(
        expiry=ExpiryPolicy(idle_ttl=timedelta(days=1)),
        distiller=distiller,
        retention=RetentionPolicy(max_age=timedelta(seconds=1), max_bytes=None),
        now=now,
        out_dir=tmp_path / "bundles",
    )

    assert report.expiry is not None
    assert set(report.expiry.forgotten_sessions) == {"s-anon", "s-alice"}

    # Both episodes were detached from their forgotten sessions
    o_anon = episode_owner(store, run_anon.run_id)
    o_alice = episode_owner(store, run_alice.run_id)
    assert o_anon is not None and o_anon.session_id is None and o_anon.user_id is None
    assert o_alice is not None and o_alice.session_id is None and o_alice.user_id == "alice"

    # Memory pass wrote nothing back to session:<id>/knowledge
    assert store.query(session("s-anon").namespace("knowledge")) == []
    assert store.query(session("s-alice").namespace("knowledge")) == []
    assert store.query(session("s-anon").namespace("context")) == []
    assert store.query(session("s-alice").namespace("context")) == []

    # Detached episode without user_id counted as unowned and skipped;
    # detached episode with user_id consolidated to persona("alice")
    assert report.memory is not None
    assert report.memory.unowned == 1
    assert report.memory.episodes == [run_alice.run_id]
    alice_knowledge = store.query(persona("alice").namespace("knowledge"))
    assert len(alice_knowledge) == 1
    assert isinstance(alice_knowledge[0][1], dict)
    assert alice_knowledge[0][1]["content"] == "alice deep persona fact"

    # Retention pruned both: run_alice as consolidated, run_anon as unconsolidated (unowned)
    assert report.retention is not None
    assert report.retention.pruned == 1
    assert report.retention.pruned_unconsolidated == 1
    assert _capture_rows(store, run_anon.run_id) == []
    assert _capture_rows(store, run_alice.run_id) == []
    store.close()


def test_forget_dry_run_writes_and_deletes_nothing(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    from vsigma import reflexive_consolidator, session
    from vsigma.episodes import (
        SESSIONS_FORGET_NS,
        episode_owner,
    )

    store = InMemoryStore()
    distiller = FakeLLM([
        say('{"content": "fact s1", "source_ref": "trajectory:r1"}'),
        say('{"content": "fact s2", "source_ref": "trajectory:r2"}'),
    ])
    itc = Interceptor(memory=store, run_end=reflexive_consolidator(distiller))
    agent = make_react_agent(
        FakeLLM([say("r1"), say("r2")]),
        [],
        interceptor=itc,
        checkpointer=checkpointer,
    )
    run1 = agent.run("t1", session_id="s1", user_id="alice")
    run2 = agent.run("t2", session_id="s2", user_id="alice")

    # Simulate s2 having a pending forget record (after thread delete)
    cast("Any", checkpointer).delete_thread("s2")
    store.write(SESSIONS_FORGET_NS, "s2", "some-cid")

    now = datetime.now(UTC) + timedelta(days=2)
    expiry = ThreadExpiry(
        checkpointer,
        ExpiryPolicy(idle_ttl=timedelta(days=1)),
        store=store,
    )

    rep = expiry.run(now=now, dry_run=True)
    assert set(rep.forgotten_sessions) == {"s1", "s2"}
    assert [e.thread_id for e in rep.expired] == ["s1"]

    # Nothing written or deleted
    assert store.read(SESSIONS_FORGET_NS, "s1") is None
    assert store.read(SESSIONS_FORGET_NS, "s2") == "some-cid"
    assert len(list(expiry.threads(now=now))) == 1
    assert len(store.query(session("s1").namespace("knowledge"))) == 1
    assert len(store.query(session("s2").namespace("knowledge"))) == 1
    o1 = episode_owner(store, run1.run_id)
    o2 = episode_owner(store, run2.run_id)
    assert o1 is not None and o1.session_id == "s1"
    assert o2 is not None and o2.session_id == "s2"


def test_session_end_forget_paused_and_after_plain_end(
    checkpointer: BaseCheckpointSaver[Any],
) -> None:
    from vsigma import SessionPaused, Tool, ToolCall, reflexive_consolidator, session
    from vsigma.episodes import (
        SESSIONS_FORGET_NS,
        episode_owner,
    )

    store = InMemoryStore()
    fail_tool = Tool(
        name="fail",
        description="fails",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "err",
    )
    distiller = FakeLLM([
        say('{"content": "fin fact", "source_ref": "trajectory:r"}'),
    ])
    itc = Interceptor(memory=store, run_end=reflexive_consolidator(distiller))

    # 1. Paused session refuses end(forget=True) without abandon=True
    agent_p = make_guarded_react_agent(
        FakeLLM([
            LLMResponse(
                text="", tool_calls=[ToolCall(id="c1", name="fail", arguments={})]
            )
        ]),
        tools=[fail_tool],
        interceptor=itc,
        checkpointer=checkpointer,
        limit=1,
    )
    sess_p = agent_p.session("s-paused", user_id="alice")
    run_p = sess_p.send("pause")
    assert sess_p.paused

    with pytest.raises(SessionPaused):
        sess_p.end(forget=True)

    # Nothing was forgotten or deleted when SessionPaused raised
    assert sess_p.paused
    assert store.read(SESSIONS_FORGET_NS, "s-paused") is None
    assert len(store.query(session("s-paused").namespace("context"))) == 1
    op_before = episode_owner(store, run_p.run_id)
    assert op_before is not None and op_before.session_id == "s-paused"
    assert get_episode_status(store, run_p.run_id) == "escalated"

    # With abandon=True, end(abandon=True, forget=True) succeeds
    sess_p.end(abandon=True, forget=True)
    assert not sess_p.paused
    assert store.read(SESSIONS_FORGET_NS, "s-paused") is None
    assert store.query(session("s-paused").namespace("context")) == []
    op_after = episode_owner(store, run_p.run_id)
    assert op_after is not None and op_after.session_id is None
    assert get_episode_status(store, run_p.run_id) == "abandoned"

    # 2. Plain end() followed by end(forget=True)
    agent_f = make_react_agent(
        FakeLLM([say("done")]),
        [],
        interceptor=itc,
        checkpointer=checkpointer,
    )
    sess_f = agent_f.session("s-fin", user_id="alice")
    run_f = sess_f.send("work")

    # Plain end() deletes thread only, keeps memory and episode session_id, writes no forget record
    sess_f.end()
    assert sess_f.dialog == []
    assert store.read(SESSIONS_FORGET_NS, "s-fin") is None
    assert len(store.query(session("s-fin").namespace("context"))) == 1
    assert len(store.query(session("s-fin").namespace("knowledge"))) == 1
    of_mid = episode_owner(store, run_f.run_id)
    assert of_mid is not None and of_mid.session_id == "s-fin"

    # Calling end(forget=True) after plain end() forgets memory and detaches episodes
    sess_f.end(forget=True)
    assert store.read(SESSIONS_FORGET_NS, "s-fin") is None
    assert store.query(session("s-fin").namespace("context")) == []
    assert store.query(session("s-fin").namespace("knowledge")) == []
    of_after = episode_owner(store, run_f.run_id)
    assert of_after is not None and of_after.session_id is None

    # 3. With memory=None, end(forget=True) is a plain end()
    agent_no_mem = make_react_agent(
        FakeLLM([say("no mem")]),
        [],
        checkpointer=checkpointer,
    )
    sess_nm = agent_no_mem.session("s-no-mem")
    sess_nm.send("hi")
    sess_nm.end(forget=True)
    assert sess_nm.dialog == []

    # 4. With a store that cannot delete, end(forget=True) raises TypeError before deleting thread
    from vsigma.memory import MemoryStore

    class ReadWriteOnlyStore(MemoryStore):
        def read(self, namespace: str, key: str) -> object | None:
            return None

        def write(self, namespace: str, key: str, value: object) -> None:
            pass

        def query(self, namespace: str) -> list[tuple[str, object]]:
            return []

    agent_ro = make_react_agent(
        FakeLLM([say("ro")]),
        [],
        interceptor=Interceptor(memory=ReadWriteOnlyStore()),
        checkpointer=checkpointer,
    )
    sess_ro = agent_ro.session("s-ro")
    sess_ro.send("hi")
    with pytest.raises(TypeError, match="MaintainableStore"):
        sess_ro.end(forget=True)
    assert len(sess_ro.dialog) == 2


def test_paused_run_recorded_abandoned_before_detaching_and_sweep_on_detached(
    checkpointer: BaseCheckpointSaver[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vsigma.expiry as expiry_mod
    from vsigma import Tool, ToolCall
    from vsigma.episodes import detach_session_episodes as real_detach

    store = InMemoryStore()
    fail_tool = Tool(
        name="fail",
        description="fails",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "err",
    )
    agent = make_guarded_react_agent(
        FakeLLM([
            LLMResponse(
                text="", tool_calls=[ToolCall(id="c1", name="fail", arguments={})]
            )
        ]),
        tools=[fail_tool],
        interceptor=Interceptor(memory=store),
        checkpointer=checkpointer,
        limit=1,
    )
    sess = agent.session("s-order", user_id="alice")
    run_paused = sess.send("pause")
    assert get_episode_status(store, run_paused.run_id) == "escalated"

    status_at_detach: list[str | None] = []

    def spy_detach(st: Any, sid: str) -> None:
        status_at_detach.append(get_episode_status(st, run_paused.run_id))
        real_detach(st, sid)

    monkeypatch.setattr(expiry_mod, "detach_session_episodes", spy_detach)

    now = datetime.now(UTC) + timedelta(days=2)
    expiry = ThreadExpiry(
        checkpointer,
        ExpiryPolicy(paused_idle_ttl=timedelta(seconds=1)),
        store=store,
    )
    report = expiry.run(now=now)

    # Paused run was recorded abandoned BEFORE detach_session_episodes ran
    assert status_at_detach == ["abandoned"]
    assert run_paused.run_id in report.abandoned_runs
    assert report.forgotten_sessions == ["s-order"]

    # Also check recovery sweep on a detached episode whose status is still escalated or running
    record_episode(store, "detached-esc", session_id=None, user_id="alice")
    store.write(EPISODE_STATUS_NS, "detached-esc", "escalated")
    record_episode(store, "detached-run", session_id=None, user_id="alice")
    store.write(EPISODE_STATUS_NS, "detached-run", "running")

    rep2 = expiry.run(now=now)
    assert "detached-esc" in rep2.abandoned_runs
    assert "detached-run" in rep2.abandoned_runs
    assert get_episode_status(store, "detached-esc") == "abandoned"
    assert get_episode_status(store, "detached-run") == "abandoned"


def test_completing_forget_record_idempotent_across_all_interruption_points(
    checkpointer: BaseCheckpointSaver[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vsigma.expiry as expiry_mod
    from vsigma import reflexive_consolidator, session
    from vsigma.episodes import (
        SESSIONS_FORGET_NS,
        detach_session_episodes,
        episode_owner,
    )
    from vsigma.expiry import complete_forget_locked

    store = InMemoryStore()
    distiller = FakeLLM([
        say('{"content": "fact", "source_ref": "trajectory:r1"}'),
        say('{"content": "fact2", "source_ref": "trajectory:r2"}'),
    ])
    itc = Interceptor(memory=store, run_end=reflexive_consolidator(distiller))
    agent = make_react_agent(
        FakeLLM([say("r1"), say("r2")]),
        [],
        interceptor=itc,
        checkpointer=checkpointer,
    )
    r1 = agent.run("t1", session_id="s-idem", user_id="alice")
    r2 = agent.run("t2", session_id="s-idem", user_id="alice")

    expiry = ThreadExpiry(
        checkpointer,
        ExpiryPolicy(idle_ttl=timedelta(days=1)),
        store=store,
    )
    cid = next(t.checkpoint_id for t in expiry.threads() if t.thread_id == "s-idem")

    # Step 1 written manually
    store.write(SESSIONS_FORGET_NS, "s-idem", cid)

    # Interruption point A: fail in _delete_session_memory (after step 2 deleted the thread)
    orig_del_mem = expiry_mod._delete_session_memory
    monkeypatch.setattr(
        expiry_mod,
        "_delete_session_memory",
        lambda st, sid: (_ for _ in ()).throw(RuntimeError("stop at step 3")),
    )
    with (
        thread_lock(checkpointer, "s-idem") as locked,
        pytest.raises(RuntimeError, match="stop at step 3"),
    ):
        complete_forget_locked(checkpointer, store, locked)

    # Thread is gone, forget record still there
    assert list(expiry.threads()) == []
    assert store.read(SESSIONS_FORGET_NS, "s-idem") == cid
    monkeypatch.setattr(expiry_mod, "_delete_session_memory", orig_del_mem)

    # Interruption point B: fail in detach_session_episodes (after step 3 deleted session memory)
    orig_detach = detach_session_episodes
    monkeypatch.setattr(
        expiry_mod,
        "detach_session_episodes",
        lambda st, sid: (_ for _ in ()).throw(RuntimeError("stop at step 4")),
    )
    with (
        thread_lock(checkpointer, "s-idem") as locked,
        pytest.raises(RuntimeError, match="stop at step 4"),
    ):
        complete_forget_locked(checkpointer, store, locked)

    # Session memory is gone, episodes still attached, forget record still there
    assert store.query(session("s-idem").namespace("context")) == []
    assert store.query(session("s-idem").namespace("knowledge")) == []
    o1_mid = episode_owner(store, r1.run_id)
    assert o1_mid is not None and o1_mid.session_id == "s-idem"
    assert store.read(SESSIONS_FORGET_NS, "s-idem") == cid
    monkeypatch.setattr(expiry_mod, "detach_session_episodes", orig_detach)

    # Now run complete_forget_locked to completion
    with thread_lock(checkpointer, "s-idem") as locked:
        assert complete_forget_locked(checkpointer, store, locked) is True
        # Repeating it again when record is gone is a safe no-op returning False
        assert complete_forget_locked(checkpointer, store, locked) is False

    assert store.read(SESSIONS_FORGET_NS, "s-idem") is None
    o1_done = episode_owner(store, r1.run_id)
    o2_done = episode_owner(store, r2.run_id)
    assert o1_done is not None and o1_done.session_id is None
    assert o2_done is not None and o2_done.session_id is None



