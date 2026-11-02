"""Tests for Phase 4.17: Session Admission — A Session Belongs to the User Who Started It."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from vsigma import (
    Agent,
    ExpiryPolicy,
    FakeLLM,
    InMemoryStore,
    Interceptor,
    Kind,
    PersistentStore,
    ReActState,
    SessionDenied,
    ThreadExpiry,
    TrajectoryLog,
    call,
    make_guarded_react_agent,
    make_react_agent,
    say,
    session,
    visible_scopes,
)
from vsigma.consolidation import episode_trajectory
from vsigma.episodes import (
    EPISODE_STATUS_NS,
    EPISODES_NS,
    SESSIONS_FORGET_NS,
    SESSIONS_OWNERS_NS,
    episode_owners,
    record_episode,
)
from vsigma.scope import recall


def _make_sqlite_saver() -> SqliteSaver:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def _make_pause_agent(saver: Any, store: Any, responses: list[Any] | None = None) -> Agent:
    llm = FakeLLM(responses or [call("missing_tool"), say("resumed"), say("next")])
    return make_guarded_react_agent(
        llm,
        [],
        interceptor=Interceptor(memory=store, observer=TrajectoryLog()),
        checkpointer=saver,
        limit=1,
    )


@pytest.mark.parametrize("saver_factory", [MemorySaver, _make_sqlite_saver])
def test_bob_refused_on_all_session_operations_leaving_alice_intact(
    saver_factory: Any,
) -> None:
    """Bob's dialog, paused, interrupt, interrupt_id, send, send(abandon=True),
    resume, end, and end(forget=True) all raise SessionDenied on Alice's paused
    session (even when has_context=True and Bob passes run_context=None, proving
    owner check precedes _require_context and SessionPaused), leaving her thread,
    pause, and episode status unchanged.
    """
    saver = saver_factory()
    store = InMemoryStore()
    agent = _make_pause_agent(saver, store)

    alice = agent.session("s1", user_id="alice", run_context={"secret": "alice_tok"})
    run_a = alice.send("please pause")
    assert run_a.interrupted
    assert alice.paused
    assert alice.interrupt is not None
    orig_interrupt = alice.interrupt
    orig_interrupt_id = alice.interrupt_id
    assert orig_interrupt_id is not None
    assert store.read(EPISODE_STATUS_NS, run_a.run_id) == "escalated"

    # Constructing Bob's Session handle does not fail
    bob = agent.session("s1", user_id="bob")

    ops: list[Callable[[], object]] = [
        lambda: bob.dialog,
        lambda: bob.paused,
        lambda: bob.interrupt,
        lambda: bob.interrupt_id,
        lambda: bob.send("bob sneaks in"),
        lambda: bob.send("bob abandons", abandon=True),
        lambda: bob.resume("bob approves"),
        lambda: bob.end(),
        lambda: bob.end(forget=True),
    ]
    for op in ops:
        with pytest.raises(SessionDenied) as exc_info:
            op()
        err = exc_info.value
        assert err.session_id == "s1"
        assert err.user_id == "bob"
        assert "s1" in str(err)
        assert "bob" in str(err)
        # Never leaks the owner's user_id
        assert "alice" not in str(err)
        assert not hasattr(err, "owner")

    # Alice's thread, pause, and episode status are completely unchanged, and no
    # episode was recorded for Bob
    assert set(episode_owners(store).keys()) == {run_a.run_id}
    assert store.read(EPISODE_STATUS_NS, run_a.run_id) == "escalated"
    assert alice.paused
    assert alice.interrupt == orig_interrupt
    assert alice.interrupt_id == orig_interrupt_id
    assert len(alice.dialog) >= 1

    # Alice can still resume via her Session or her Run handle
    resumed = alice.resume("approved")
    assert resumed.result == "resumed"
    assert not alice.paused
    assert store.read(EPISODE_STATUS_NS, run_a.run_id) == "done"


@pytest.mark.parametrize("saver_factory", [MemorySaver, _make_sqlite_saver])
def test_agent_run_refuses_bob_with_no_episode_recorded(saver_factory: Any) -> None:
    """agent.run(session_id=...) refuses Bob the same way (before _require_context
    and SessionPaused), with no episode recorded for him.
    """
    saver = saver_factory()
    store = InMemoryStore()
    agent = _make_pause_agent(saver, store)

    run_a = agent.run(
        "pause me",
        session_id="s1",
        user_id="alice",
        run_context={"token": "t"},
    )
    assert run_a.interrupted

    cases: list[tuple[str | None, bool]] = [
        ("bob", False),
        ("bob", True),
        (None, False),
        (None, True),
    ]
    for bad_uid, ab in cases:
        with pytest.raises(SessionDenied) as exc_info:
            agent.run("intrude", session_id="s1", user_id=bad_uid, abandon=ab)
        assert exc_info.value.session_id == "s1"
        assert exc_info.value.user_id == bad_uid
        assert "alice" not in str(exc_info.value)

    # Only Alice's episode exists in episodes/index and episodes/status
    assert {k for k, _ in store.query(EPISODES_NS)} == {run_a.run_id}
    assert {k for k, _ in store.query(EPISODE_STATUS_NS)} == {run_a.run_id}
    assert store.read(EPISODE_STATUS_NS, run_a.run_id) == "escalated"

    # Run.resume on the Run handle issued to Alice keeps working
    resumed = run_a.resume("yes")
    assert resumed.result == "resumed"


@pytest.mark.parametrize("saver_factory", [MemorySaver, _make_sqlite_saver])
def test_none_user_id_and_named_user_id_equality_rules(saver_factory: Any) -> None:
    """A handle with no user_id is refused on an owned session, and a named one
    on an open session (started with user_id=None).
    """
    saver = saver_factory()
    store = InMemoryStore()
    agent = make_react_agent(
        FakeLLM([say("r1"), say("r2"), say("r3"), say("r4")]),
        [],
        interceptor=Interceptor(memory=store),
        checkpointer=saver,
    )

    # 1. Owned session ("s_owned", user_id="alice"): user_id=None is refused
    agent.session("s_owned", user_id="alice").send("hello from alice")
    anon_on_owned = agent.session("s_owned", user_id=None)
    anon_fns: list[Callable[[], object]] = [
        lambda: anon_on_owned.dialog,
        lambda: anon_on_owned.paused,
        lambda: anon_on_owned.send("anon turn"),
        lambda: anon_on_owned.end(),
        lambda: anon_on_owned.end(forget=True),
    ]
    for fn in anon_fns:
        with pytest.raises(SessionDenied) as exc_info:
            fn()
        assert exc_info.value.session_id == "s_owned"
        assert exc_info.value.user_id is None
        assert "alice" not in str(exc_info.value)

    # 2. Open session ("s_open", user_id=None): user_id=None works, named user_id is refused
    open_sess = agent.session("s_open", user_id=None)
    assert open_sess.send("open turn 1").result == "r2"
    assert open_sess.send("open turn 2").result == "r3"
    assert len(open_sess.dialog) == 4

    named_on_open = agent.session("s_open", user_id="alice")
    named_fns: list[Callable[[], object]] = [
        lambda: named_on_open.dialog,
        lambda: named_on_open.paused,
        lambda: named_on_open.send("alice claims open session"),
        lambda: named_on_open.end(),
        lambda: named_on_open.end(forget=True),
        lambda: agent.run("alice via run", session_id="s_open", user_id="alice"),
    ]
    for fn in named_fns:
        with pytest.raises(SessionDenied) as exc_info:
            fn()
        assert exc_info.value.session_id == "s_open"
        assert exc_info.value.user_id == "alice"


@pytest.mark.parametrize("saver_factory", [MemorySaver, _make_sqlite_saver])
def test_checkpoint_without_user_id_key_passes_and_next_turn_records_owner(
    saver_factory: Any,
) -> None:
    """A checkpoint with no user_id key in its metadata (e.g. written by
    graph.update_state) has no owner on record: the check passes, and the next
    turn records its own user_id.
    """
    saver = saver_factory()
    store = InMemoryStore()
    agent = make_react_agent(
        FakeLLM([say("claimed")]),
        [],
        interceptor=Interceptor(memory=store),
        checkpointer=saver,
    )

    cfg: Any = {"configurable": {"thread_id": "s_raw"}}
    agent.graph.update_state(cfg, {"messages": [{"role": "user", "content": "seeded"}]})
    snap = agent.graph.get_state(cfg)
    assert snap.metadata is not None
    assert "user_id" not in snap.metadata

    # Check passes for Alice and records user_id="alice" on the turn
    alice = agent.session("s_raw", user_id="alice")
    assert len(alice.dialog) == 1
    assert alice.send("take ownership").result == "claimed"

    # Now Bob is refused
    with pytest.raises(SessionDenied):
        _ = agent.session("s_raw", user_id="bob").dialog


@pytest.mark.parametrize("saver_factory", [MemorySaver, _make_sqlite_saver])
def test_alice_works_across_restart_resume_and_checkpoints_latest(
    saver_factory: Any,
) -> None:
    """Alice's session works across a restart, a resume, and checkpoints='latest',
    while Bob remains refused at every stage.
    """
    saver = saver_factory()
    store = InMemoryStore()

    def build_agent(llm: FakeLLM, checkpoints: str = "all") -> Agent:
        return make_guarded_react_agent(
            llm,
            [],
            interceptor=Interceptor(memory=store),
            checkpointer=saver,
            limit=1,
            checkpoints=checkpoints,  # type: ignore[arg-type]
        )

    agent1 = build_agent(FakeLLM([call("missing_tool")]), checkpoints="all")
    r1 = agent1.session("s_persist", user_id="alice").send("start")
    assert r1.interrupted

    # Simulate restart: new Agent instance over the same checkpointer and store
    agent2 = build_agent(FakeLLM([say("after_resume"), say("latest_turn")]), checkpoints="latest")
    with pytest.raises(SessionDenied):
        agent2.session("s_persist", user_id="bob").resume("hijack")

    alice2 = agent2.session("s_persist", user_id="alice")
    assert alice2.paused
    r2 = alice2.resume("confirmed")
    assert r2.result == "after_resume"

    # Next turn under checkpoints="latest"
    r3 = alice2.send("follow up")
    assert r3.result == "latest_turn"

    with pytest.raises(SessionDenied):
        _ = agent2.session("s_persist", user_id="bob").dialog


@pytest.mark.parametrize("saver_factory", [MemorySaver, _make_sqlite_saver])
def test_v0_3_1_thread_protected_with_no_migration(saver_factory: Any) -> None:
    """A thread written by v0.3.1 (checkpoint metadata has user_id='alice', no
    sessions/owners record) is protected from the first turn after upgrading.
    """
    saver = saver_factory()
    store = InMemoryStore()

    # Build a raw graph writing a v0.3.1-style checkpoint with configurable/metadata user_id
    builder: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    builder.add_node("step", lambda s: {"messages": [{"role": "assistant", "content": "v031"}]})
    builder.add_edge(START, "step")
    builder.add_edge("step", END)
    raw_graph = builder.compile(checkpointer=saver)
    raw_graph.invoke(
        {"messages": [{"role": "user", "content": "hi"}]},
        config={
            "configurable": {"thread_id": "s_v031"},
            "metadata": {"user_id": "alice", "session_id": "s_v031", "has_context": False},
        },
    )
    assert store.read(SESSIONS_OWNERS_NS, "s_v031") is None

    agent = make_react_agent(
        FakeLLM([say("v040")]),
        [],
        interceptor=Interceptor(memory=store),
        checkpointer=saver,
    )

    with pytest.raises(SessionDenied):
        _ = agent.session("s_v031", user_id="bob").dialog
    with pytest.raises(SessionDenied):
        _ = agent.session("s_v031", user_id=None).dialog

    alice = agent.session("s_v031", user_id="alice")
    assert len(alice.dialog) == 2
    assert alice.send("continue").result == "v040"


@pytest.mark.parametrize("saver_factory", [MemorySaver, _make_sqlite_saver])
def test_plain_end_persists_owner_blocks_bob_and_lets_alice_continue_with_memory(
    saver_factory: Any,
) -> None:
    """After Alice's plain end(), Bob (and user_id=None) is refused on dialog,
    send, end, and end(forget=True), while Alice may read dialog ([]) and
    continue with her session memory intact.
    """
    saver = saver_factory()
    store = InMemoryStore()
    agent = make_react_agent(
        FakeLLM([say("turn1"), say("turn2")]),
        [],
        interceptor=Interceptor(memory=store, observer=TrajectoryLog()),
        checkpointer=saver,
    )

    alice = agent.session("s_end", user_id="alice")
    alice.send("hello")
    store.write(session("s_end").namespace("knowledge"), "k1", {"fact": "alice secret"})

    alice.end()
    # Thread checkpoint is gone, owner record is written in sessions/owners
    assert agent.graph.get_state({"configurable": {"thread_id": "s_end"}}).metadata is None
    assert store.read(SESSIONS_OWNERS_NS, "s_end") == "alice"

    # Bob and user_id=None are refused on dialog, send, end, and end(forget=True)
    def _check_refused(bad_user: str | None) -> None:
        bad_sess = agent.session("s_end", user_id=bad_user)
        bad_fns: list[Callable[[], object]] = [
            lambda: bad_sess.dialog,
            lambda: bad_sess.paused,
            lambda: bad_sess.send("steal ended session"),
            lambda: bad_sess.end(),
            lambda: bad_sess.end(forget=True),
            lambda: agent.run("steal via run", session_id="s_end", user_id=bad_user),
        ]
        for fn in bad_fns:
            with pytest.raises(SessionDenied) as exc_info:
                fn()
            assert exc_info.value.session_id == "s_end"
            assert exc_info.value.user_id == bad_user
            assert "alice" not in str(exc_info.value)

    _check_refused("bob")
    _check_refused(None)

    # Alice can inspect her empty dialog and continue on s_end with her session memory intact
    assert alice.dialog == []
    assert alice.send("back again").result == "turn2"
    assert store.read(session("s_end").namespace("knowledge"), "k1") == {"fact": "alice secret"}


@pytest.mark.parametrize("saver_factory", [MemorySaver, _make_sqlite_saver])
def test_forget_and_expiry_free_session_id_for_new_owner(
    saver_factory: Any, tmp_path: Path
) -> None:
    """After end(forget=True) (both directly and after a plain end()) or
    ThreadExpiry with a store, the id is free and the new owner sees nothing
    of the old one. Also verifies leftover forget record deletes sessions/owners.
    """
    saver = saver_factory()
    store = PersistentStore(tmp_path / f"forget_{saver_factory.__name__}.db")
    agent = make_react_agent(
        FakeLLM([say("r") for _ in range(10)]),
        [],
        interceptor=Interceptor(memory=store, observer=TrajectoryLog()),
        checkpointer=saver,
    )

    # Case 0: direct end(forget=True) on a live session
    alice0 = agent.session("s_f0", user_id="alice")
    alice0.send("alice msg 0")
    store.write(session("s_f0").namespace("knowledge"), "k1", {"fact": "a0"})
    alice0.end(forget=True)
    assert store.read(SESSIONS_OWNERS_NS, "s_f0") is None
    bob0 = agent.session("s_f0", user_id="bob")
    assert bob0.dialog == []
    bob_run0 = bob0.send("bob msg 0")
    assert bob_run0.result == "r"
    assert store.query(session("s_f0").namespace("knowledge")) == []
    _, traj0 = episode_trajectory(store, bob_run0.run_id)
    assert "alice msg 0" not in traj0
    assert "prior_context:" not in traj0

    # Case 1: plain end() followed by end(forget=True)
    alice1 = agent.session("s_f1", user_id="alice")
    alice1.send("alice msg 1")
    store.write(session("s_f1").namespace("knowledge"), "k1", {"fact": "a1"})
    alice1.end()
    assert store.read(SESSIONS_OWNERS_NS, "s_f1") == "alice"

    alice1.end(forget=True)
    assert store.read(SESSIONS_OWNERS_NS, "s_f1") is None

    bob1 = agent.session("s_f1", user_id="bob")
    assert bob1.dialog == []
    bob_run1 = bob1.send("bob msg 1")
    assert bob_run1.result == "r"
    assert store.query(session("s_f1").namespace("knowledge")) == []
    _, traj1 = episode_trajectory(store, bob_run1.run_id)
    assert "alice msg 1" not in traj1
    assert "prior_context:" not in traj1

    # Case 2: crash recovery via leftover SESSIONS_FORGET_NS record also deletes sessions/owners
    store.write(SESSIONS_OWNERS_NS, "s_crash_forget", "alice")
    store.write(SESSIONS_FORGET_NS, "s_crash_forget", {"session_id": "s_crash_forget"})
    bob_crash = agent.session("s_crash_forget", user_id="bob")
    assert bob_crash.send("bob claims after crash").result == "r"
    assert store.read(SESSIONS_OWNERS_NS, "s_crash_forget") is None

    # Case 3: ThreadExpiry with store frees the session id
    expiry = ThreadExpiry(
        saver,
        policy=ExpiryPolicy(idle_ttl=timedelta(seconds=60)),
        store=store,
    )
    alice2 = agent.session("s_exp", user_id="alice")
    alice2.send("alice expiring msg")
    store.write(session("s_exp").namespace("knowledge"), "k1", {"fact": "exp_secret"})

    future = datetime.now(UTC) + timedelta(seconds=120)
    rep = expiry.run(now=future)
    assert "s_exp" in [t.thread_id for t in rep.expired]
    assert store.read(SESSIONS_OWNERS_NS, "s_exp") is None

    bob2 = agent.session("s_exp", user_id="bob")
    assert bob2.dialog == []
    bob_run2 = bob2.send("bob on expired session")
    assert bob_run2.result == "r"
    assert store.query(session("s_exp").namespace("knowledge")) == []
    _, traj2 = episode_trajectory(store, bob_run2.run_id)
    assert "alice expiring msg" not in traj2
    assert "prior_context:" not in traj2

    store.close()


@pytest.mark.parametrize("saver_factory", [MemorySaver, _make_sqlite_saver])
def test_bobs_distill_source_on_reused_or_crashed_id_holds_none_of_alices_turns(
    saver_factory: Any,
) -> None:
    """Bob's distill source (episode_trajectory) on a reused id or a crashed
    first turn holds none of Alice's turns.
    """
    saver = saver_factory()
    store = InMemoryStore()
    agent = make_react_agent(
        FakeLLM([say("bob_reply")]),
        [],
        interceptor=Interceptor(memory=store, observer=TrajectoryLog()),
        checkpointer=saver,
    )

    # Simulate Alice's first turn on "s_crashed" crashing after recording her
    # episode and transcript in the store, before any checkpoint was written.
    record_episode(store, "run_alice_crashed", task_id="t", session_id="s_crashed", user_id="alice")
    store.write(
        "episode:run_alice_crashed/context",
        "0001:turn",
        {"messages": [{"role": "user", "content": "alice crashed secret"}]},
    )

    # Because no checkpoint was written, Bob can start "s_crashed"
    bob_run = agent.run("bob fresh turn", session_id="s_crashed", user_id="bob")
    assert bob_run.result == "bob_reply"

    _, traj = episode_trajectory(store, bob_run.run_id)
    assert "alice crashed secret" not in traj
    assert "run_alice_crashed" not in traj
    assert "prior_context:" not in traj


def test_sessions_owners_never_reaches_visible_scopes_or_recall() -> None:
    """sessions/owners is an operational namespace and never reaches
    visible_scopes or recall.
    """
    store = InMemoryStore()
    store.write(SESSIONS_OWNERS_NS, "s1", "alice")
    store.write(session("s1").namespace("knowledge"), "k1", {"content": "session fact"})

    state: dict[str, Any] = {
        "session_id": "s1",
        "user_id": "alice",
        "task_id": "t1",
        "run_id": "r1",
    }
    scopes = visible_scopes(state)
    kinds: tuple[Kind, ...] = ("context", "knowledge")
    namespaces_read = [s.namespace(kind) for s in scopes for kind in kinds]
    assert SESSIONS_OWNERS_NS not in namespaces_read
    assert all(not ns.startswith("sessions/") for ns in namespaces_read)

    recalled = recall(store, state)
    assert all(ns != SESSIONS_OWNERS_NS for ns, _, _ in recalled)
    assert all("alice" not in str(val) for _, _, val in recalled)
    assert any("session fact" in str(val) for _, _, val in recalled)
