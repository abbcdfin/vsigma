"""Tests for the cross-cutting service defaults."""

from vsigma import InMemoryStore, TrajectoryEvent, TrajectoryLog


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
