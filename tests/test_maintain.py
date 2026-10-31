"""Tests for agent.maintain and CLI memory maintain (docs/proposals/maintenance.md)."""

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from vsigma import (
    AGENT,
    ExpiryPolicy,
    FakeLLM,
    InMemoryStore,
    Interceptor,
    PersistentStore,
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
from vsigma.retention_episodes import _capture_rows
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
    store.close()


def test_upfront_raises(tmp_path: Path) -> None:
    # 1. Neither checkpointer nor store
    with pytest.raises(ValueError, match="requires an agent with a checkpointer or a store"):
        maintain()

    # 2. Expiry with no checkpointer
    store = PersistentStore(tmp_path / "s.db")
    with pytest.raises(ValueError, match="expiry requires an agent with a checkpointer"):
        maintain(store=store, expiry=ExpiryPolicy(idle_ttl=timedelta(days=1)))

    # 3. Unsupported checkpointer
    class FakeCP:
        pass

    with pytest.raises(NotImplementedError, match="is not supported"):
        maintain(checkpointer=FakeCP(), store=store)

    # 4. Distiller with non-MaintainableStore
    dummy = _DummyStore()
    with pytest.raises(ValueError, match="distiller requires a MaintainableStore"):
        maintain(store=dummy, distiller=FakeLLM([]))

    # 5. Distiller with pathless store (InMemoryStore) and no out_dir
    im = InMemoryStore()
    with pytest.raises(ValueError, match="distiller with no out_dir requires a store with a path"):
        maintain(store=im, distiller=FakeLLM([]))
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

    # In shared session "s1", Alice runs first turn, then Bob runs second turn
    r1 = agent.run("alice turn", session_id="s1", user_id="alice").run_id
    r2 = agent.run("bob turn", session_id="s1", user_id="bob").run_id

    # When maintaining Bob's turn with only_owner=True,
    # Alice's turn is excluded from Bob's prior context!
    from vsigma.consolidation import episode_trajectory

    # With only_owner=False (default), Alice's turn is in prior context:
    _, traj_all = episode_trajectory(store, r2, only_owner=False)
    assert f"prior_context:{r1}" in traj_all

    # With only_owner=True, Alice's turn is excluded:
    _, traj_owner = episode_trajectory(store, r2, only_owner=True)
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


def test_old_checkpoints_skip_paused_and_busy_threads(tmp_path: Path) -> None:
    db = tmp_path / "cp_test.db"
    store = PersistentStore(db)
    cp = InMemorySaver()

    itc = Interceptor(memory=store, observer=TrajectoryLog())
    agent = make_react_agent(FakeLLM([say("done")]), [], checkpointer=cp, interceptor=itc)
    agent.run("task 1", session_id="s1", user_id="alice")

    # Simulate thread paused
    latest_cid = max(cp.storage["s1"][""].keys())
    cast(dict[Any, Any], cp.writes)[("s1", "", latest_cid)] = {
        ("s1", "", latest_cid, "__interrupt__", 0): (0, "__interrupt__", "paused")
    }

    report = agent.maintain()
    assert report.retention is not None
    assert report.retention.skipped_paused == 1
    store.close()


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
