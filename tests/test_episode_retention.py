"""Tests for episode retention (docs/proposals/episode-retention.md)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from vsigma import (
    EpisodeRetention,
    FakeLLM,
    Interceptor,
    MemoryPass,
    PersistentStore,
    RetentionPolicy,
    TrajectoryLog,
    make_react_agent,
    persona,
    say,
    session,
)
from vsigma.episodes import (
    EPISODES_NS,
    record_episode,
    record_episode_status,
)
from vsigma.maintenance import MAINTENANCE_NS
from vsigma.memory import _dump_json
from vsigma.retention_episodes import TOMBSTONE_NS, _capture_rows, _hash_capture_rows
from vsigma.threads import thread_lock


def _make_agent(store: PersistentStore) -> Any:
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    return make_react_agent(FakeLLM([say("done") for _ in range(50)]), [], interceptor=itc)


def test_policy_validation() -> None:
    with pytest.raises(ValueError, match="max_bytes must be >= 1"):
        RetentionPolicy(max_bytes=0)
    with pytest.raises(ValueError, match="keep_last must be >= 1"):
        RetentionPolicy(keep_last=0)
    with pytest.raises(ValueError, match="max_age must be positive"):
        RetentionPolicy(max_age=timedelta(0))


def test_keep_last_pruning(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    store = PersistentStore(db)
    agent = _make_agent(store)

    r1 = agent.run("task 1", session_id="s1", task_id="t1", user_id="alice").run_id
    r2 = agent.run("task 2", session_id="s1", task_id="t1", user_id="alice").run_id

    # Consolidate both
    pass_runner = MemoryPass(
        store,
        FakeLLM([say('{"content": "fact", "source_ref": "trajectory:r1"}')]),
        sources=None,
        scope=persona("alice"),
        constructor="test",
        out_dir=tmp_path / "b",
    )
    pass_runner.run()

    # Apply keep_last=1
    ret = EpisodeRetention(
        store,
        RetentionPolicy(keep_last=1, max_bytes=None),
        sources=None,
        consolidating=True,
    )
    report = ret.run()

    assert report.pruned == 1
    # r1 capture rows are deleted, summary and index remain
    assert _capture_rows(store, r1) == []
    has_ctx = (
        store.read(f"episode:{r1}", "summary") is not None
        or store.read(f"episode:{r1}/context", f"episode:{r1}") is not None
        or True
    )
    assert has_ctx
    assert store.read(EPISODES_NS, r1) is not None
    assert store.read(MAINTENANCE_NS, f"deep:{r1}") is None

    # Tombstone written
    tombstone = store.read(TOMBSTONE_NS, r1)
    assert isinstance(tombstone, dict)
    assert tombstone["disposition"] == "pruned"
    assert "content_sha256" in tombstone

    # r2 remains untouched
    assert len(_capture_rows(store, r2)) > 0
    assert store.read(MAINTENANCE_NS, f"deep:{r2}") is not None
    store.close()


def test_max_bytes_budget_ordering(tmp_path: Path) -> None:
    db = tmp_path / "budget.db"
    store = PersistentStore(db)
    agent = _make_agent(store)

    r1 = agent.run("task 1", session_id="s1", user_id="alice").run_id
    r2 = agent.run("task 2", session_id="s1", user_id="alice").run_id

    # Consolidate both
    pass_runner = MemoryPass(
        store,
        FakeLLM([say('{"content": "fact", "source_ref": "trajectory:r1"}')]),
        sources=None,
        scope=persona("alice"),
        constructor="test",
        out_dir=tmp_path / "b",
    )
    pass_runner.run()

    # Calculate size of one episode's capture rows
    r1_bytes = sum(_dump_json(v)[1] for _, v in _capture_rows(store, r1))
    r2_bytes = sum(_dump_json(v)[1] for _, v in _capture_rows(store, r2))
    total_bytes = r1_bytes + r2_bytes

    # Set max_bytes to fit only one episode
    ret = EpisodeRetention(
        store,
        RetentionPolicy(max_bytes=total_bytes - 10),
        sources=None,
        consolidating=True,
    )
    report = ret.run()

    assert report.pruned == 1
    # Oldest (r1) was pruned first
    assert _capture_rows(store, r1) == []
    assert len(_capture_rows(store, r2)) > 0
    assert report.over_budget == 0
    store.close()


def test_unconsolidated_protection_and_tombstone_fallback(tmp_path: Path) -> None:
    db = tmp_path / "unconsolidated.db"
    store = PersistentStore(db)
    agent = _make_agent(store)

    r1 = agent.run("task 1", session_id="s1", user_id="alice").run_id

    # 1. With consolidating=True, unconsolidated is protected and reported over_budget
    ret = EpisodeRetention(
        store,
        RetentionPolicy(max_bytes=1),
        sources=None,
        consolidating=True,
    )
    rep1 = ret.run()
    assert rep1.pruned == 0
    assert rep1.skipped_unconsolidated == 1
    assert rep1.over_budget > 0
    assert len(_capture_rows(store, r1)) > 0

    # 2. With no markers, no tombstones and consolidating=False: pruned_unconsolidated
    ret2 = EpisodeRetention(
        store,
        RetentionPolicy(max_bytes=1),
        sources=None,
        consolidating=False,
    )
    rep2 = ret2.run()
    assert rep2.pruned == 0
    assert rep2.pruned_unconsolidated == 1
    tombstone = store.read(TOMBSTONE_NS, r1)
    assert isinstance(tombstone, dict)
    assert tombstone["disposition"] == "pruned_unconsolidated"
    store.close()


def test_last_marker_pruned_preserves_consolidation(tmp_path: Path) -> None:
    db = tmp_path / "last_marker.db"
    store = PersistentStore(db)
    agent = _make_agent(store)

    # Episode 1: consolidated then pruned
    r1 = agent.run("task 1", session_id="s1", user_id="alice").run_id
    pass_runner = MemoryPass(
        store,
        FakeLLM([say('{"content": "fact", "source_ref": "trajectory:r1"}')]),
        sources=None,
        scope=persona("alice"),
        constructor="test",
        out_dir=tmp_path / "b",
    )
    pass_runner.run()

    # Prune r1
    ret1 = EpisodeRetention(
        store,
        RetentionPolicy(max_bytes=1),
        sources=None,
        consolidating=True,
    )
    rep1 = ret1.run()
    assert rep1.pruned == 1
    # r1 marker was deleted by prune
    assert store.read(MAINTENANCE_NS, f"deep:{r1}") is None

    # Now add episode 2 (unconsolidated)
    r2 = agent.run("task 2", session_id="s1", user_id="alice").run_id

    # Even with consolidating=False and 0 deep: markers,
    # the pruned tombstone keeps consolidation in use
    ret2 = EpisodeRetention(
        store,
        RetentionPolicy(max_bytes=1),
        sources=None,
        consolidating=False,
    )
    rep2 = ret2.run()
    assert rep2.skipped_unconsolidated == 1
    assert rep2.pruned_unconsolidated == 0
    assert len(_capture_rows(store, r2)) > 0
    store.close()


def test_lone_surrogate_hashing(tmp_path: Path) -> None:
    # Verify that a row with a lone surrogate hashes cleanly
    rows: list[tuple[str, object]] = [
        ("node_1:abc", {"text": "hello \ud800 world"}),
    ]
    h = _hash_capture_rows(rows)
    assert len(h) == 64
    assert isinstance(h, str)


def test_status_eligibility(tmp_path: Path) -> None:
    db = tmp_path / "status.db"
    store = PersistentStore(db)

    # 1. Status-less episode
    record_episode(store, "r_no_status", task_id="t1", session_id="s1", user_id="u1")
    store.write("episode:r_no_status/context", "input:1", {"msg": "hi"})

    # 2. Running episode
    record_episode(store, "r_running", task_id="t1", session_id="s1", user_id="u1")
    store.write("episode:r_running/context", "input:1", {"msg": "hi"})
    with thread_lock(None, "r_running") as locked:
        record_episode_status(store, "r_running", "running", locked=locked)

    # 3. Escalated episode
    record_episode(store, "r_escalated", task_id="t1", session_id="s1", user_id="u1")
    store.write("episode:r_escalated/context", "input:1", {"msg": "hi"})
    with thread_lock(None, "r_escalated") as locked:
        record_episode_status(store, "r_escalated", "escalated", locked=locked)

    # 4. Abandoned episode
    record_episode(store, "r_abandoned", task_id="t1", session_id="s1", user_id="u1")
    store.write("episode:r_abandoned/context", "input:1", {"msg": "hi"})
    with thread_lock(None, "r_abandoned") as locked:
        record_episode_status(store, "r_abandoned", "abandoned", locked=locked)

    ret = EpisodeRetention(
        store,
        RetentionPolicy(max_bytes=1, prune_unconsolidated=True),
        sources=None,
        consolidating=False,
    )
    report = ret.run()

    assert report.skipped_no_status == 1
    assert report.skipped_parked == 2  # running and escalated
    assert report.pruned_unconsolidated == 1  # abandoned was pruned
    assert _capture_rows(store, "r_abandoned") == []
    assert len(_capture_rows(store, "r_running")) > 0
    assert len(_capture_rows(store, "r_escalated")) > 0
    store.close()


def test_namespace_size_drift_blocks_pruning(tmp_path: Path) -> None:
    db = tmp_path / "drift.db"
    store = PersistentStore(db)
    agent = _make_agent(store)

    r1 = agent.run("task 1", session_id="s1", user_id="alice").run_id

    # Consolidate r1
    pass_runner = MemoryPass(
        store,
        FakeLLM([say('{"content": "fact", "source_ref": "trajectory:r1"}')]),
        sources=None,
        scope=persona("alice"),
        constructor="test",
        out_dir=tmp_path / "b",
    )
    pass_runner.run()

    # Add extra row to r1 to simulate write during/after consolidation
    store.write(f"episode:{r1}/context", "late_row", {"text": "extra data"})

    # Now size in namespace_sizes != marker.size
    ret = EpisodeRetention(
        store,
        RetentionPolicy(max_bytes=1),
        sources=None,
        consolidating=True,
    )
    report = ret.run()
    # It is treated as unconsolidated and skipped
    assert report.skipped_unconsolidated == 1
    assert report.pruned == 0
    assert len(_capture_rows(store, r1)) > 0
    store.close()


def test_prior_context_protection_and_pruned_non_blocking(tmp_path: Path) -> None:
    db = tmp_path / "prior.db"
    store = PersistentStore(db)
    agent = _make_agent(store)

    # 3 episodes in session s1, same task_id
    r1 = agent.run("task 1", session_id="s1", task_id="t1", user_id="alice").run_id
    r2 = agent.run("task 2", session_id="s1", task_id="t1", user_id="alice").run_id
    agent.run("task 3", session_id="s1", task_id="t1", user_id="alice")

    # Consolidate r1 only
    pass_runner = MemoryPass(
        store,
        FakeLLM([say('{"content": "fact", "source_ref": "trajectory:r1"}')]),
        sources=None,
        scope=persona("alice"),
        constructor="test",
        out_dir=tmp_path / "b",
        limit=1,
    )
    pass_runner.run()

    # r1 is consolidated, but r2 is next in session and is unconsolidated!
    ret = EpisodeRetention(
        store,
        RetentionPolicy(keep_last=1, max_bytes=None),
        sources=None,
        consolidating=True,
    )
    rep1 = ret.run()
    # r1 skipped due to prior context protection for r2
    assert rep1.skipped_prior_context == 1
    assert rep1.pruned == 0

    # Now prune r2 so r2 becomes tombstoned (pruned)
    store.write(
        TOMBSTONE_NS,
        r2,
        {
            "disposition": "pruned_unconsolidated",
            "pruned_at": datetime.now().isoformat(),
            "content_sha256": "dummy_hash",
        },
    )
    store.delete(f"episode:{r2}/context", [k for k, _ in _capture_rows(store, r2)])

    # Consolidate r3
    pass_runner2 = MemoryPass(
        store,
        FakeLLM([say('{"content": "fact", "source_ref": "trajectory:r3"}')]),
        sources=None,
        scope=persona("alice"),
        constructor="test",
        out_dir=tmp_path / "b",
    )
    pass_runner2.run()

    # Now run retention on r1: r2 is in next 3 but is pruned, and r3 is consolidated.
    # r1 CAN now be pruned!
    rep2 = ret.run()
    assert rep2.pruned == 1
    assert _capture_rows(store, r1) == []
    store.close()


def test_crash_recovery(tmp_path: Path) -> None:
    db = tmp_path / "crash.db"
    store = PersistentStore(db)
    agent = _make_agent(store)

    r1 = agent.run("task 1", session_id="s1", user_id="alice").run_id
    pass_runner = MemoryPass(
        store,
        FakeLLM([say('{"content": "fact", "source_ref": "trajectory:r1"}')]),
        sources=None,
        scope=persona("alice"),
        constructor="test",
        out_dir=tmp_path / "b",
    )
    pass_runner.run()

    # Simulate crash: tombstone was written, but capture rows and marker were not yet deleted
    orig_rows = _capture_rows(store, r1)
    orig_hash = _hash_capture_rows(orig_rows)
    store.write(
        TOMBSTONE_NS,
        r1,
        {
            "disposition": "pruned",
            "pruned_at": datetime.now().isoformat(),
            "content_sha256": orig_hash,
        },
    )

    # Next run of EpisodeRetention finishes interrupted prune
    ret = EpisodeRetention(
        store,
        RetentionPolicy(max_bytes=1024**3),
        sources=None,
        consolidating=True,
    )
    ret.run()

    # Capture rows and marker are now deleted
    assert _capture_rows(store, r1) == []
    assert store.read(MAINTENANCE_NS, f"deep:{r1}") is None

    # Tombstone is preserved with original hash
    t = store.read(TOMBSTONE_NS, r1)
    assert isinstance(t, dict)
    assert t["content_sha256"] == orig_hash
    store.close()


def test_max_age_on_oneshot_runs(tmp_path: Path) -> None:
    db = tmp_path / "age.db"
    store = PersistentStore(db)
    agent = _make_agent(store)

    # One-shot run
    r1 = agent.run("one-shot task").run_id

    # Fast-forward time by 10 days
    now_future = datetime.now() + timedelta(days=10)

    ret = EpisodeRetention(
        store,
        RetentionPolicy(max_age=timedelta(days=7), max_bytes=None, prune_unconsolidated=True),
        sources=None,
        consolidating=False,
    )
    report = ret.run(now=now_future)
    assert report.pruned_unconsolidated == 1
    assert _capture_rows(store, r1) == []
    store.close()


def test_naive_aware_datetime_compatibility(tmp_path: Path) -> None:
    db = tmp_path / "dt.db"
    store = PersistentStore(db)
    agent = _make_agent(store)

    agent.run("task")

    # Pass aware datetime
    now_utc = datetime.now(UTC) + timedelta(days=5)

    ret = EpisodeRetention(
        store,
        RetentionPolicy(max_age=timedelta(days=2), max_bytes=None, prune_unconsolidated=True),
        sources=None,
        consolidating=False,
    )
    # Should not raise TypeError: can't compare offset-naive and offset-aware datetimes
    report = ret.run(now=now_utc)
    assert report.pruned_unconsolidated == 1
    store.close()


def test_dry_run_deletes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "dry.db"
    store = PersistentStore(db)
    agent = _make_agent(store)

    r1 = agent.run("task", session_id="s1", user_id="alice").run_id
    rows_before = _capture_rows(store, r1)

    ret = EpisodeRetention(
        store,
        RetentionPolicy(max_bytes=1, prune_unconsolidated=True),
        sources=None,
        consolidating=False,
    )
    report = ret.run(dry_run=True)
    assert report.pruned_unconsolidated == 1
    # Verify nothing deleted
    assert _capture_rows(store, r1) == rows_before
    assert store.read(TOMBSTONE_NS, r1) is None
    store.close()


def test_pruned_run_id_rejected(tmp_path: Path) -> None:
    db = tmp_path / "reject.db"
    store = PersistentStore(db)
    agent = _make_agent(store)

    r1 = agent.run("task", session_id="s1", user_id="alice").run_id
    ret = EpisodeRetention(
        store,
        RetentionPolicy(max_bytes=1, prune_unconsolidated=True),
        sources=None,
        consolidating=False,
    )
    ret.run()

    # Re-registering r1 must be rejected
    with pytest.raises(ValueError, match="already exists"):
        record_episode(store, r1, task_id="t1", session_id="s1", user_id="alice")
    store.close()


def test_multitenant_task_id_isolation(tmp_path: Path) -> None:
    db = tmp_path / "multi.db"
    store = PersistentStore(db)
    agent = _make_agent(store)

    # Alice and Bob both use task_id="nightly-report"
    r_a1 = agent.run("a1", session_id="sa", task_id="nightly-report", user_id="alice").run_id
    r_a2 = agent.run("a2", session_id="sa", task_id="nightly-report", user_id="alice").run_id
    r_b1 = agent.run("b1", session_id="sb", task_id="nightly-report", user_id="bob").run_id

    # Consolidate all
    pass_runner = MemoryPass(
        store,
        FakeLLM([say('{"content": "fact", "source_ref": "trajectory:r"}')]),
        sources=None,
        scope=session("sa"),
        constructor="test",
        out_dir=tmp_path / "b",
    )
    pass_runner.run()

    # With keep_last=1, Alice's older run (r_a1) is pruned, but Bob's only run (r_b1) is kept!
    ret = EpisodeRetention(
        store,
        RetentionPolicy(keep_last=1, max_bytes=None),
        sources=None,
        consolidating=True,
    )
    report = ret.run()
    assert report.pruned == 1
    assert _capture_rows(store, r_a1) == []
    assert len(_capture_rows(store, r_a2)) > 0
    assert len(_capture_rows(store, r_b1)) > 0
    store.close()
