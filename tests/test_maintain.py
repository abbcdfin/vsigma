"""Tests for agent.maintain and CLI memory maintain (docs/proposals/maintenance.md)."""

import gc
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START, StateGraph
from langgraph.types import interrupt

from vsigma import (
    AGENT,
    Agent,
    ExpiryPolicy,
    FakeLLM,
    InMemoryStore,
    Interceptor,
    PersistentStore,
    RetentionPolicy,
    TrajectoryLog,
    make_react_agent,
    persona,
    say,
)
from vsigma.cli import main
from vsigma.episodes import (
    record_episode,
    record_episode_status,
)
from vsigma.maintenance import maintain
from vsigma.memory import MemoryStore
from vsigma.memory_pass import store_guard
from vsigma.retention_episodes import _capture_rows
from vsigma.state import ReActState
from vsigma.threads import thread_lock


class _DummyStore(MemoryStore):
    """A store that cannot delete (not MaintainableStore)."""

    def __init__(self) -> None:
        self._d: dict[str, dict[str, Any]] = {}

    def read(self, namespace: str, key: str) -> Any:
        return self._d.get(namespace, {}).get(key)

    def write(self, namespace: str, key: str, value: Any) -> None:
        self._d.setdefault(namespace, {})[key] = value

    def query(self, namespace: str) -> list[tuple[str, Any]]:
        return list(self._d.get(namespace, {}).items())


def test_default_maintain_runs_sweep_checkpoints_and_budget_no_llm(tmp_path: Path) -> None:
    db = tmp_path / "app.db"
    store = PersistentStore(db)
    conn = sqlite3.connect(tmp_path / "cp.db", check_same_thread=False)
    cp = SqliteSaver(conn)
    cp.setup()

    itc = Interceptor(memory=store, observer=TrajectoryLog())
    agent = make_react_agent(
        FakeLLM([say("done"), say("done")]),
        [],
        interceptor=itc,
        checkpointer=cp,
    )

    agent.run("t1", session_id="s1", task_id="task", user_id="alice")
    agent.run("t2", session_id="s1", task_id="task", user_id="alice")

    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id = 's1' AND checkpoint_ns = ''"
    )
    cnt_before = cur.fetchone()[0]
    assert cnt_before > 1

    # Add an abandoned episode in store
    record_episode(store, "r_dead", task_id="dead", session_id="s_dead", user_id="alice")
    with thread_lock(cp, "s_dead") as locked:
        record_episode_status(store, "r_dead", "running", locked=locked)

    # Calling maintain() with no arguments (default):
    report = agent.maintain()

    assert report.expiry is not None
    # Dead run was recovered as abandoned by sweep
    assert "r_dead" in report.expiry.abandoned_runs
    # No distiller was passed, so memory is None (0 spent on LLM)
    assert report.memory is None
    # Retention ran
    assert report.retention is not None
    assert report.retention.checkpoints_pruned >= 1
    assert report.retention.over_budget == 0

    cur.execute(
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id = 's1' AND checkpoint_ns = ''"
    )
    assert cur.fetchone()[0] == 1
    store.close()
    conn.close()


def test_upfront_raises(tmp_path: Path) -> None:
    # 1. Neither checkpointer nor store
    sg: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    sg.add_node("act", lambda s: {"status": "done"})
    sg.add_edge(START, "act")
    agent_empty = Agent(sg.compile(), Interceptor())
    with pytest.raises(
        ValueError, match="maintain requires an agent with a checkpointer or a store"
    ):
        agent_empty.maintain()

    # 2. Expiry with no checkpointer
    store = PersistentStore(tmp_path / "s.db")
    sg_store: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    sg_store.add_node("act", lambda s: {"status": "done"})
    sg_store.add_edge(START, "act")
    agent_store_only = Agent(sg_store.compile(), Interceptor(memory=store))
    with pytest.raises(ValueError, match="expiry requires an agent with a checkpointer"):
        agent_store_only.maintain(expiry=ExpiryPolicy(idle_ttl=timedelta(days=1)))
    del agent_store_only
    gc.collect()

    # 3. Unsupported checkpointer
    class FakeCP:
        pass

    class FakeGraph:
        checkpointer = FakeCP()

    class FakeAgent:
        graph = FakeGraph()
        memory = store

    with pytest.raises(NotImplementedError, match="is not supported"):
        maintain(cast(Agent, FakeAgent()))

    # 4. Distiller with non-MaintainableStore
    dummy = _DummyStore()
    agent_dummy = make_react_agent(FakeLLM([]), [], interceptor=Interceptor(memory=dummy))
    with pytest.raises(ValueError, match="distiller requires a MaintainableStore"):
        agent_dummy.maintain(distiller=FakeLLM([]))

    # 5. Distiller with pathless store (InMemoryStore) and no out_dir
    im = InMemoryStore()
    agent_im = make_react_agent(FakeLLM([]), [], interceptor=Interceptor(memory=im))
    with pytest.raises(ValueError, match="distiller with no out_dir requires a store with a path"):
        agent_im.maintain(distiller=FakeLLM([]))
    store.close()


def test_retention_none_skips_jobs_3_and_4(tmp_path: Path) -> None:
    db = tmp_path / "skip.db"
    store = PersistentStore(db)
    cp = InMemorySaver()
    agent = make_react_agent(
        FakeLLM([say("done")]),
        [],
        checkpointer=cp,
        interceptor=Interceptor(memory=store),
    )
    agent.run("t1", session_id="s1")

    report = agent.maintain(retention=None)
    assert report.retention is None
    store.close()


def test_maintain_owner_routing_and_tenant_isolation(tmp_path: Path) -> None:
    db = tmp_path / "tenant.db"
    store = PersistentStore(db)
    agent = make_react_agent(
        FakeLLM([say("done") for _ in range(10)]),
        [],
        interceptor=Interceptor(memory=store, observer=TrajectoryLog()),
    )

    # Alice in session a, Bob in session b
    agent.run("alice task", session_id="sa", user_id="alice")
    agent.run("bob task", session_id="sb", user_id="bob")

    # Distiller returns fact
    distiller = FakeLLM([
        say('{"content": "alice fact", "source_ref": "trajectory:r"}'),
        say('{"content": "bob fact", "source_ref": "trajectory:r"}'),
    ])

    report = agent.maintain(distiller=distiller, out_dir=tmp_path / "bundles")

    assert report.memory is not None
    assert report.memory.records == 2
    assert len(report.memory.bundles) == 2  # Tenants never share a bundle!

    # Alice knowledge in persona(alice)
    alice_k = store.query(persona("alice").namespace("knowledge"))
    assert len(alice_k) == 1
    assert isinstance(alice_k[0][1], dict)
    assert alice_k[0][1]["content"] == "alice fact"

    # Bob knowledge in persona(bob)
    bob_k = store.query(persona("bob").namespace("knowledge"))
    assert len(bob_k) == 1
    assert isinstance(bob_k[0][1], dict)
    assert bob_k[0][1]["content"] == "bob fact"

    # AGENT knowledge is completely untouched
    assert store.query(AGENT.namespace("knowledge")) == []
    store.close()


def test_shared_session_only_owner_filters_prior_context(tmp_path: Path) -> None:
    db = tmp_path / "shared.db"
    store = PersistentStore(db)
    agent = make_react_agent(
        FakeLLM([say("done") for _ in range(10)]),
        [],
        interceptor=Interceptor(memory=store, observer=TrajectoryLog()),
    )

    # On a storeless agent or after thread deletion (e.g. a crashed first turn
    # or reused session ID), Alice's episode and Bob's episode share session_id="s1".
    r1 = agent.run("alice turn", session_id="s1", user_id="alice").run_id
    cast("Any", agent.graph.checkpointer).delete_thread("s1")
    r2 = agent.run("bob turn", session_id="s1", user_id="bob").run_id

    # episode_trajectory always filters prior context to the same user_id,
    # so Alice's turn is excluded from Bob's prior context:
    from vsigma.consolidation import episode_trajectory

    _, traj_owner = episode_trajectory(store, r2)
    assert f"prior_context:{r1}" not in traj_owner
    store.close()


def test_unowned_episodes_counted_and_do_not_starve_limit(tmp_path: Path) -> None:
    db = tmp_path / "unowned.db"
    store = PersistentStore(db)

    # 20 unowned episodes
    for i in range(20):
        rid = f"unowned_{i}"
        record_episode(store, rid, task_id="t", session_id=None, user_id=None)
        store.write(f"episode:{rid}/context", "input:1", {"msg": "hi"})
        with thread_lock(None, rid) as locked:
            record_episode_status(store, rid, "done", locked=locked)

    # 1 owned episode by Alice
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    agent = make_react_agent(FakeLLM([say("done")]), [], interceptor=itc)
    agent.run("alice task", session_id="sa", user_id="alice")

    distiller = FakeLLM([say('{"content": "alice fact", "source_ref": "trajectory:r"}')])
    report = agent.maintain(distiller=distiller, limit=5, out_dir=tmp_path / "bundles")

    assert report.memory is not None
    # 20 unowned counted
    assert report.memory.unowned == 20
    # Alice was still consolidated even though limit was 5!
    assert report.memory.records == 1
    store.close()


def test_one_owner_failure_does_not_block_others_and_raises_exception_group(tmp_path: Path) -> None:
    db = tmp_path / "failure_iso.db"
    store = PersistentStore(db)
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    agent = make_react_agent(FakeLLM([say("done"), say("done")]), [], interceptor=itc)

    agent.run("alice task", session_id="sa", user_id="alice")
    agent.run("bob task", session_id="sb", user_id="bob")

    # Alice's batch fails (malformed JSON), Bob's succeeds
    distiller = FakeLLM([
        say("invalid json here"),
        say('{"content": "bob fact", "source_ref": "trajectory:r"}'),
    ])

    with pytest.raises(ExceptionGroup):
        agent.maintain(distiller=distiller, out_dir=tmp_path / "bundles")

    # Bob fact was ingested despite Alice failing
    bob_k = store.query(persona("bob").namespace("knowledge"))
    assert len(bob_k) == 1
    assert isinstance(bob_k[0][1], dict)
    assert bob_k[0][1]["content"] == "bob fact"
    store.close()


def test_failing_owner_episodes_do_not_starve_limit(tmp_path: Path) -> None:
    db = tmp_path / "starve.db"
    store = PersistentStore(db)
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    agent = make_react_agent(FakeLLM([say("done") for _ in range(10)]), [], interceptor=itc)

    # Alice has 5 episodes, Bob has 1 episode
    for i in range(5):
        agent.run(f"alice {i}", session_id="sa", user_id="alice")
    agent.run("bob 1", session_id="sb", user_id="bob")

    # Alice fails on first batch, but Bob is still reached in same call
    distiller = FakeLLM([
        say("broken output for alice"),
        say('{"content": "bob fact", "source_ref": "trajectory:r"}'),
    ])

    with pytest.raises(ExceptionGroup):
        # limit=2: Alice picks 2, fails; Alice is blocked; loop picks Bob!
        agent.maintain(distiller=distiller, limit=2, out_dir=tmp_path / "bundles")

    bob_k = store.query(persona("bob").namespace("knowledge"))
    assert len(bob_k) == 1
    assert isinstance(bob_k[0][1], dict)
    assert bob_k[0][1]["content"] == "bob fact"
    store.close()


def test_maintain_with_distiller_does_not_report_busy(tmp_path: Path) -> None:
    db = tmp_path / "busy.db"
    store = PersistentStore(db)
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    agent = make_react_agent(FakeLLM([say("done")]), [], interceptor=itc)
    agent.run("task", session_id="s1", user_id="alice")

    distiller = FakeLLM([say('{"content": "fact", "source_ref": "trajectory:r"}')])
    report = agent.maintain(distiller=distiller, out_dir=tmp_path / "bundles")

    assert report.memory is not None
    assert not report.memory.busy
    assert report.memory.records == 1
    store.close()


@pytest.mark.parametrize("saver_type", ["memory", "sqlite"])
def test_old_checkpoints_skip_paused_and_busy_threads(tmp_path: Path, saver_type: str) -> None:
    db = tmp_path / f"cp_test_{saver_type}.db"
    store = PersistentStore(db)

    conn = None
    if saver_type == "memory":
        cp: BaseCheckpointSaver[Any] = InMemorySaver()
    else:
        conn = sqlite3.connect(tmp_path / f"cp_{saver_type}.db", check_same_thread=False)
        cp = SqliteSaver(conn)
        cp.setup()

    sub: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    finished_steps: list[str] = []

    def step1(state: ReActState) -> dict[str, object]:
        finished_steps.append("step1_done")
        return {}

    def pause_node(state: ReActState) -> dict[str, object]:
        msgs = state.get("messages", [])
        if len(msgs) > 2:
            return {"status": "done"}
        val = interrupt("sub_pause")
        return {
            "messages": [{"role": "assistant", "content": f"sub_ans={val}"}],
            "status": "done",
        }

    sub.add_node("step1", step1)
    sub.add_node("step2", pause_node)
    sub.add_edge(START, "step1")
    sub.add_edge("step1", "step2")

    parent: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    parent.add_node("sub_node", sub.compile())
    parent.add_edge(START, "sub_node")

    agent = Agent(parent.compile(checkpointer=cp), Interceptor(memory=store))

    # Thread 1: paused at subgraph interrupt
    sess_paused = agent.session("s_paused")
    run_paused = sess_paused.send("start")
    assert run_paused.interrupted
    assert sess_paused.paused
    assert finished_steps == ["step1_done"]

    # Thread 2: busy (another thread holding thread_lock)
    agent_busy = make_react_agent(FakeLLM([say("done"), say("done")]), [], checkpointer=cp)
    agent_busy.run("busy turn 1", session_id="s_busy")
    agent_busy.run("busy turn 2", session_id="s_busy")

    with thread_lock(cp, "s_busy"):
        report = agent.maintain()

    assert report.retention is not None
    assert report.retention.skipped_paused == 1
    assert report.retention.skipped_busy == 1

    # Resume the paused thread
    run_res = sess_paused.resume("my_feedback")
    assert run_res.status == "done"
    # The finished subgraph step was NOT rerun from the beginning (executed once)
    assert finished_steps == ["step1_done"]

    store.close()
    if conn is not None:
        conn.close()


def test_cli_memory_maintain_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "cli.db"
    store = PersistentStore(db)
    agent = make_react_agent(
        FakeLLM([say("done"), say("done")]),
        [],
        interceptor=Interceptor(memory=store, observer=TrajectoryLog()),
    )
    r1 = agent.run("t1", session_id="sa", task_id="t", user_id="alice").run_id
    r2 = agent.run("t2", session_id="sa", task_id="t", user_id="alice").run_id
    store.close()

    monkeypatch.setattr(
        "vsigma.cli.default_llm",
        lambda: FakeLLM([say('{"content": "cli fact", "source_ref": "trajectory:r1"}')]),
    )

    # Run cli memory maintain
    args = [
        "memory",
        "maintain",
        "--db",
        str(db),
        "--source",
        "session:sa",
        "--scope",
        "persona:alice",
        "--out",
        str(tmp_path / "bundles"),
        "--keep-last",
        "1",
    ]
    code = main(args)
    assert code == 0

    s2 = PersistentStore(db)
    # 1 episode pruned, 1 consolidated
    assert _capture_rows(s2, r1) == []
    assert len(_capture_rows(s2, r2)) > 0
    s2.close()


def test_maintain_fails_when_two_agents_share_store(tmp_path: Path) -> None:
    store = PersistentStore(tmp_path / "shared.db")
    a1 = make_react_agent(FakeLLM([]), [], interceptor=Interceptor(memory=store))
    _a2 = make_react_agent(FakeLLM([]), [], interceptor=Interceptor(memory=store))
    with pytest.raises(ValueError, match="only live Agent using its store"):
        a1.maintain()
    store.close()


def test_two_agents_sharing_store_run_normally_without_maintain(tmp_path: Path) -> None:
    store = PersistentStore(tmp_path / "shared_run.db")
    a1 = make_react_agent(FakeLLM([say("done")]), [], interceptor=Interceptor(memory=store))
    a2 = make_react_agent(FakeLLM([say("done")]), [], interceptor=Interceptor(memory=store))
    r1 = a1.run("task1").status
    r2 = a2.run("task2").status
    assert r1 == "done"
    assert r2 == "done"
    store.close()


def test_two_agents_with_no_store_can_both_call_maintain() -> None:
    cp1 = InMemorySaver()
    cp2 = InMemorySaver()
    a1 = make_react_agent(FakeLLM([]), [], checkpointer=cp1)
    a2 = make_react_agent(FakeLLM([]), [], checkpointer=cp2)
    rep1 = a1.maintain()
    rep2 = a2.maintain()
    assert rep1 is not None
    assert rep2 is not None


def test_dropped_agent_stops_counting_for_maintain(tmp_path: Path) -> None:
    import gc

    store = PersistentStore(tmp_path / "gc.db")
    a1 = make_react_agent(FakeLLM([]), [], interceptor=Interceptor(memory=store))
    a2 = make_react_agent(FakeLLM([]), [], interceptor=Interceptor(memory=store))

    del a2
    gc.collect()

    rep = a1.maintain()
    assert rep is not None
    store.close()


def test_maintain_with_delta_channel_checkpoints_all_and_none() -> None:
    from vsigma.retention import _DeltaChannel

    sg: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    if _DeltaChannel is not None:
        sg.channels["delta"] = _DeltaChannel(reducer=lambda a, b: b)
    sg.add_node("act", lambda s: {"status": "done"})
    sg.add_edge(START, "act")
    cp = InMemorySaver()
    im_store = InMemoryStore()
    agent = Agent(sg.compile(checkpointer=cp), Interceptor(memory=im_store))

    if _DeltaChannel is not None:
        distiller_called = False

        class TrackingLLM(FakeLLM):
            def complete(
                self, messages: list[dict[str, object]], tools: list[dict[str, object]]
            ) -> Any:
                nonlocal distiller_called
                distiller_called = True
                return super().complete(messages, tools)

        distiller = TrackingLLM([])

        # Default retention (checkpoints="latest") raises ValueError upfront
        with pytest.raises(ValueError, match="does not support DeltaChannel"):
            agent.maintain(distiller=distiller, out_dir="/tmp/b")
        assert not distiller_called

        # checkpoints="all" succeeds
        rep_all = agent.maintain(retention=RetentionPolicy(checkpoints="all"))
        assert rep_all is not None

        # retention=None succeeds
        rep_none = agent.maintain(retention=None)
        assert rep_none is not None


def test_batch_per_destination_not_per_user_session(tmp_path: Path) -> None:
    db = tmp_path / "dest.db"
    store = PersistentStore(db)
    agent = make_react_agent(
        FakeLLM([say("done") for _ in range(10)]),
        [],
        interceptor=Interceptor(memory=store, observer=TrajectoryLog()),
    )

    # Alice has 3 episodes across 3 sessions
    agent.run("t1", session_id="s1", user_id="alice")
    agent.run("t2", session_id="s2", user_id="alice")
    agent.run("t3", session_id="s3", user_id="alice")

    distiller = FakeLLM([
        say('{"content": "alice consolidated", "source_ref": "t"}'),
    ])

    report = agent.maintain(distiller=distiller, out_dir=tmp_path / "bundles")
    assert report.memory is not None
    # Single bundle and single batch for persona(alice), not 3!
    assert len(report.memory.bundles) == 1
    assert report.memory.records == 1
    store.close()


def test_prior_context_session_last_three_then_filter_owner(tmp_path: Path) -> None:
    db = tmp_path / "prior_reach.db"
    store = PersistentStore(db)
    agent = make_react_agent(
        FakeLLM([say("done") for _ in range(20)]),
        [],
        interceptor=Interceptor(memory=store, observer=TrajectoryLog()),
    )

    # In session s1 (across thread deletions on a storeless agent):
    # A1, A2, A3, B1, B2, B3, A4
    agent.run("a1", session_id="s1", user_id="alice")
    agent.run("a2", session_id="s1", user_id="alice")
    agent.run("a3", session_id="s1", user_id="alice")
    cast("Any", agent.graph.checkpointer).delete_thread("s1")
    agent.run("b1", session_id="s1", user_id="bob")
    agent.run("b2", session_id="s1", user_id="bob")
    agent.run("b3", session_id="s1", user_id="bob")
    cast("Any", agent.graph.checkpointer).delete_thread("s1")
    r_a4 = agent.run("a4", session_id="s1", user_id="alice").run_id

    from vsigma.consolidation import episode_trajectory

    # Last 3 turns of session are B1, B2, B3. None belong to Alice.
    # So Alice's prior context must be empty and must NOT reach back to A1, A2, A3!
    _, traj = episode_trajectory(store, r_a4)
    assert "prior_context:" not in traj
    store.close()


def test_in_memory_and_persistent_hash_identical_with_complex_types(tmp_path: Path) -> None:
    from datetime import UTC

    from vsigma.retention_episodes import TOMBSTONE_NS, EpisodeRetention

    dt = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    complex_val = {
        "messages": [{"role": "user", "content": "hello"}],
        "created_at": dt,
        "count": 2**65,
        "surrogate": "test \ud83d\ude00",
    }

    im_store = InMemoryStore()
    p_store = PersistentStore(tmp_path / "p_hash.db")

    for s in (im_store, p_store):
        record_episode(s, "r1", task_id="t1", session_id="s1", user_id="alice")
        with thread_lock(InMemorySaver(), "s1") as locked:
            record_episode_status(s, "r1", "done", locked=locked)
        s.write("episode:r1/context", "agent:1", complex_val)

    # Prune both
    ret_im = EpisodeRetention(
        im_store,
        RetentionPolicy(max_bytes=1, prune_unconsolidated=True),
        sources=None,
    )
    ret_p = EpisodeRetention(
        p_store,
        RetentionPolicy(max_bytes=1, prune_unconsolidated=True),
        sources=None,
    )

    rep_im = ret_im.run()
    rep_p = ret_p.run()
    assert rep_im.pruned_unconsolidated == 1
    assert rep_p.pruned_unconsolidated == 1

    tomb_im = im_store.read(TOMBSTONE_NS, "r1")
    tomb_p = p_store.read(TOMBSTONE_NS, "r1")
    assert isinstance(tomb_im, dict) and isinstance(tomb_p, dict)
    assert tomb_im["content_sha256"] == tomb_p["content_sha256"]
    p_store.close()


def test_maintain_naive_now_no_type_error(tmp_path: Path) -> None:
    db = tmp_path / "naive_now.db"
    store = PersistentStore(db)
    cp = InMemorySaver()
    agent = make_react_agent(
        FakeLLM([say("done")]),
        [],
        checkpointer=cp,
        interceptor=Interceptor(memory=store),
    )
    agent.run("t1", session_id="s1")

    # Pass naive datetime
    rep = agent.maintain(now=datetime(2026, 11, 1, 12, 0, 0))
    assert rep is not None
    store.close()


def test_store_guard_unhashable_and_yields_outside_lock() -> None:
    class UnhashableStore:
        __hash__ = None  # type: ignore[assignment]

    s = UnhashableStore()
    with store_guard(s) as acquired:
        assert acquired is True
        # While acquired, second check yields False immediately without blocking
        with store_guard(s) as second:
            assert second is False


def test_maintain_wrong_retention_type_raises() -> None:
    agent = make_react_agent(FakeLLM([]), [], checkpointer=InMemorySaver())
    with pytest.raises(TypeError, match="retention must be a RetentionPolicy"):
        agent.maintain(retention="invalid_string")  # type: ignore[arg-type]


def test_episode_retention_raises_at_construction_on_unsupported_store() -> None:
    from vsigma.retention_episodes import EpisodeRetention

    class FakeStore:
        pass

    with pytest.raises(ValueError, match="MaintainableStore"):
        EpisodeRetention(cast(Any, FakeStore()), RetentionPolicy(), sources=None)


def test_report_expiry_none_when_no_expiry_and_no_store() -> None:
    agent = make_react_agent(FakeLLM([]), [], checkpointer=InMemorySaver())
    rep = agent.maintain(expiry=None)
    assert rep.expiry is None


def test_cli_maintain_prints_pruned_unconsolidated_and_over_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "cli_uncons.db"
    store = PersistentStore(db)
    agent = make_react_agent(FakeLLM([say("done")]), [], interceptor=Interceptor(memory=store))
    agent.run("t1", session_id="s1", task_id="t1", user_id="alice")
    store.close()

    monkeypatch.setattr(
        "vsigma.cli.default_llm",
        lambda: FakeLLM([]),
    )

    args = [
        "memory",
        "maintain",
        "--db",
        str(db),
        "--source",
        "session:s1",
        "--scope",
        "persona:alice",
        "--limit",
        "0",
        "--max-bytes",
        "1",
        "--prune-unconsolidated",
    ]
    code = main(args)
    assert code == 0
    captured = capsys.readouterr().out
    assert "unconsolidated pruned" in captured


def test_second_pass_returns_busy_true(tmp_path: Path) -> None:
    db = tmp_path / "busy_guard.db"
    store = PersistentStore(db)
    agent = make_react_agent(FakeLLM([]), [], interceptor=Interceptor(memory=store))

    with store_guard(store):
        rep = agent.maintain(distiller=FakeLLM([]))
        assert rep.memory is not None
        assert rep.memory.busy is True
    store.close()


def test_job_failure_isolation_and_exception_group(tmp_path: Path) -> None:
    db = tmp_path / "fail_iso.db"
    store = PersistentStore(db)
    cp = InMemorySaver()
    agent = make_react_agent(
        FakeLLM([say("done"), say("done")]),
        [],
        checkpointer=cp,
        interceptor=Interceptor(memory=store),
    )
    agent.run("t1", session_id="s1", user_id="alice")
    agent.run("t2", session_id="s1", user_id="alice")
    assert len(cp.storage["s1"][""]) > 1

    distiller = FakeLLM([say("broken json")])

    with pytest.raises(ExceptionGroup):
        agent.maintain(distiller=distiller, out_dir=tmp_path / "b")

    # Retention job (Job 3 & 4) still ran despite Job 2 failing:
    # Check that old checkpoints are actually gone (only latest root remains)
    assert len(cp.storage["s1"][""]) == 1
    store.close()


def test_maintain_dry_run_deletes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "dry_run.db"
    store = PersistentStore(db)
    conn = sqlite3.connect(tmp_path / "cp.db", check_same_thread=False)
    cp = SqliteSaver(conn)
    cp.setup()

    agent = make_react_agent(
        FakeLLM([say("done"), say("done")]),
        [],
        checkpointer=cp,
        interceptor=Interceptor(memory=store),
    )
    r1 = agent.run("t1", session_id="s1", task_id="t", user_id="alice").run_id
    agent.run("t2", session_id="s1", task_id="t", user_id="alice")

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = 's1' AND checkpoint_ns = ''")
    cnt_before = cur.fetchone()[0]
    assert cnt_before > 1

    rep = agent.maintain(dry_run=True)
    assert rep.retention is not None
    assert rep.retention.checkpoints_pruned >= 1

    cur.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = 's1' AND checkpoint_ns = ''")
    assert cur.fetchone()[0] == cnt_before

    assert len(_capture_rows(store, r1)) > 0
    store.close()
    conn.close()


def test_retention_none_leaves_checkpoints_in_place(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "cp_none.db", check_same_thread=False)
    cp = SqliteSaver(conn)
    cp.setup()

    agent = make_react_agent(FakeLLM([say("done"), say("done")]), [], checkpointer=cp)
    agent.run("t1", session_id="s1")
    agent.run("t2", session_id="s1")

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = 's1' AND checkpoint_ns = ''")
    cnt_before = cur.fetchone()[0]
    assert cnt_before > 1

    rep = agent.maintain(retention=None)
    assert rep.retention is None

    cur.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = 's1' AND checkpoint_ns = ''")
    assert cur.fetchone()[0] == cnt_before
    conn.close()


def test_job1_failure_does_not_abort_jobs_2_3_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vsigma.expiry import ThreadExpiry

    db = tmp_path / "job1_fail.db"
    store = PersistentStore(db)
    cp = InMemorySaver()
    agent = make_react_agent(
        FakeLLM([say("done"), say("done")]),
        [],
        checkpointer=cp,
        interceptor=Interceptor(memory=store, observer=TrajectoryLog()),
    )
    r1 = agent.run("t1", session_id="s1", task_id="t", user_id="alice").run_id
    r2 = agent.run("t2", session_id="s1", task_id="t", user_id="alice").run_id

    assert len(cp.storage["s1"][""]) > 1

    def broken_run(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Job 1 sweep failed unexpectedly")

    monkeypatch.setattr(ThreadExpiry, "run", broken_run)

    distiller = FakeLLM([say('{"content": "alice fact", "source_ref": "t"}')])

    # With keep_last=1, Job 4 will prune r1
    with pytest.raises(ExceptionGroup) as exc_info:
        agent.maintain(
            distiller=distiller,
            retention=RetentionPolicy(keep_last=1),
            out_dir=tmp_path / "b",
        )

    # ExceptionGroup holds Job 1 failure
    assert any("Job 1 sweep failed unexpectedly" in str(e) for e in exc_info.value.exceptions)

    # Job 2 ran: knowledge consolidated
    alice_k = store.query(persona("alice").namespace("knowledge"))
    assert len(alice_k) == 1
    assert isinstance(alice_k[0][1], dict)
    assert alice_k[0][1]["content"] == "alice fact"

    # Job 3 ran: old checkpoints are gone
    assert len(cp.storage["s1"][""]) == 1

    # Job 4 ran: transcript budget enforced (r1 capture rows pruned)
    assert _capture_rows(store, r1) == []
    assert len(_capture_rows(store, r2)) > 0
    store.close()


def test_agents_registry_lock_and_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from weakref import WeakSet

    import vsigma.maintenance as maint_module
    import vsigma.run as run_module

    class CheckedRegistry(WeakSet[Any]):
        def add(self, item: Any) -> None:
            assert run_module._AGENTS_LOCK.locked()
            assert hasattr(item, "checkpoints")  # the last attribute __init__ sets
            super().add(item)

        def __iter__(self) -> Any:
            assert run_module._AGENTS_LOCK.locked()
            return super().__iter__()

    reg: CheckedRegistry = CheckedRegistry()
    monkeypatch.setattr(run_module, "_AGENTS", reg)
    monkeypatch.setattr(maint_module, "_AGENTS", reg)

    store = PersistentStore(tmp_path / "reg.db")
    agent = make_react_agent(FakeLLM([]), [], interceptor=Interceptor(memory=store))
    agent.maintain(retention=None)
    assert len(reg) == 1
    store.close()


def test_agent_invalid_checkpoints_raises_and_leaves_registry_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from weakref import WeakSet

    import vsigma.maintenance as maint_module
    import vsigma.run as run_module

    class CheckedRegistry(WeakSet[Any]):
        def add(self, item: Any) -> None:
            assert run_module._AGENTS_LOCK.locked()
            assert hasattr(item, "checkpoints")
            super().add(item)

        def __iter__(self) -> Any:
            assert run_module._AGENTS_LOCK.locked()
            return super().__iter__()

    reg: CheckedRegistry = CheckedRegistry()
    monkeypatch.setattr(run_module, "_AGENTS", reg)
    monkeypatch.setattr(maint_module, "_AGENTS", reg)

    sg: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    sg.add_node("act", lambda s: {"status": "done"})
    sg.add_edge(START, "act")
    compiled = sg.compile()

    with pytest.raises(ValueError, match="checkpoints must be 'all' or 'latest'"):
        Agent(compiled, Interceptor(), checkpoints="bad")  # type: ignore[arg-type]

    assert len(reg) == 0


def test_job2_failure_does_not_abort_jobs_3_and_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vsigma.maintenance as maint_module

    db = tmp_path / "job2_fail.db"
    store = PersistentStore(db)
    cp = InMemorySaver()
    agent = make_react_agent(
        FakeLLM([say("done"), say("done")]),
        [],
        checkpointer=cp,
        interceptor=Interceptor(memory=store),
    )
    agent.run("t1", session_id="s1", task_id="t", user_id="alice")
    agent.run("t2", session_id="s1", task_id="t", user_id="alice")

    assert len(cp.storage["s1"][""]) > 1

    def broken_unconsolidated(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Job 2 consolidation failed")

    monkeypatch.setattr(maint_module, "unconsolidated_episodes", broken_unconsolidated)

    distiller = FakeLLM([say('{"content": "fact", "source_ref": "t"}')])

    with pytest.raises(ExceptionGroup) as exc_info:
        agent.maintain(distiller=distiller, out_dir=tmp_path / "b")

    # ExceptionGroup holds the failure from Job 2
    assert any("Job 2 consolidation failed" in str(e) for e in exc_info.value.exceptions)

    # Job 3 still ran: old checkpoints are gone
    assert len(cp.storage["s1"][""]) == 1
    store.close()


def test_job3_failure_does_not_abort_job_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vsigma.maintenance as maint_module
    from vsigma.retention_episodes import EpisodeRetention

    db = tmp_path / "job3_fail.db"
    store = PersistentStore(db)
    cp = InMemorySaver()
    agent = make_react_agent(
        FakeLLM([say("done"), say("done")]),
        [],
        checkpointer=cp,
        interceptor=Interceptor(memory=store),
    )
    r1 = agent.run("t1", session_id="s1", task_id="t", user_id="alice").run_id
    agent.run("t2", session_id="s1", task_id="t", user_id="alice")

    assert len(cp.storage["s1"][""]) > 1

    def broken_prune(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Job 3 checkpoint prune failed")

    monkeypatch.setattr(maint_module, "prune_checkpoints", broken_prune)

    retention_called = False
    orig_ret_run = EpisodeRetention.run

    def tracking_ret_run(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal retention_called
        retention_called = True
        return orig_ret_run(self, *args, **kwargs)

    monkeypatch.setattr(EpisodeRetention, "run", tracking_ret_run)

    with pytest.raises(ExceptionGroup) as exc_info:
        agent.maintain(retention=RetentionPolicy(keep_last=1))

    # ExceptionGroup holds the failure from Job 3
    assert any("Job 3 checkpoint prune failed" in str(e) for e in exc_info.value.exceptions)

    # EpisodeRetention.run was still called
    assert retention_called is True
    # And transcript budget was enforced
    assert _capture_rows(store, r1) == []
    store.close()


def test_unconsolidated_episodes_reselects_on_namespace_size_drift(tmp_path: Path) -> None:
    from vsigma.memory_pass import MAINTENANCE_NS, unconsolidated_episodes

    store = PersistentStore(tmp_path / "drift.db")
    run_id = "ep_drift"
    record_episode(store, run_id, task_id="t", session_id="s1", user_id="alice")
    store.write(f"episode:{run_id}/context", "input:1", {"msg": "first turn"})
    with thread_lock(None, run_id) as locked:
        record_episode_status(store, run_id, "done", locked=locked)

    assert unconsolidated_episodes(store, sources=None) == [run_id]

    # Write marker with current size
    cur_size = store.namespace_sizes()[f"episode:{run_id}/context"]
    store.write(
        MAINTENANCE_NS,
        f"deep:{run_id}",
        {"at": "2026-11-01T00:00:00", "constructor": "c/deep", "size": cur_size},
    )

    # Marker size matches current size -> skipped
    assert unconsolidated_episodes(store, sources=None) == []

    # Namespace changes size after marker (e.g. late arrival)
    store.write(f"episode:{run_id}/context", "extra:1", {"msg": "late arrival"})

    # Graduation criterion: episode whose namespace changed size is selected again!
    assert unconsolidated_episodes(store, sources=None) == [run_id]
    store.close()


def test_reading_sizes_before_transcript_leaves_concurrent_write_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vsigma.memory_pass as mp_module
    from vsigma.memory_pass import _consolidate_batch, unconsolidated_episodes

    store = PersistentStore(tmp_path / "concurrent_write.db")
    run_id = "ep_concurrent"
    record_episode(store, run_id, task_id="t", session_id="s1", user_id="alice")
    store.write(f"episode:{run_id}/context", "input:1", {"msg": "initial"})
    with thread_lock(None, run_id) as locked:
        record_episode_status(store, run_id, "done", locked=locked)

    orig_trajectory = mp_module.episode_trajectory  # type: ignore[attr-defined]

    def wrapped_trajectory(st: Any, rid: str) -> Any:
        # Simulate a concurrent write arriving while the transcript is being read
        st.write(
            f"episode:{rid}/context",
            "concurrent:1",
            {"msg": "concurrent write during read"},
        )
        return orig_trajectory(st, rid)

    monkeypatch.setattr(mp_module, "episode_trajectory", wrapped_trajectory)

    # Run consolidation batch
    llm = FakeLLM([say('{"content": "fact", "source_ref": "t"}')])
    _consolidate_batch(
        store,
        llm,
        episodes=[run_id],
        scope=persona("alice"),
        constructor="test/deep",
        out_dir=tmp_path / "b",
    )

    # Because sizes were read BEFORE the transcript read, the marker recorded the OLD size.
    # Therefore the concurrent write left the marker stale, and the episode is selected again:
    assert unconsolidated_episodes(store, sources=None) == [run_id]
    store.close()
