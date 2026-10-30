"""Durable episode ownership (docs/proposals/memory-scope-promotion.md §B.2)."""

from pathlib import Path
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from vsigma import (
    FakeLLM,
    InMemoryStore,
    Interceptor,
    Kind,
    SQLiteStore,
    State,
    TrajectoryLog,
    call,
    episode,
    make_guarded_react_agent,
    make_react_agent,
    say,
    visible_scopes,
)
from vsigma.episodes import (
    EPISODE_STATUS_NS,
    EPISODES_NS,
    episode_owner,
    episode_owners,
    get_episode_status,
    list_episode_statuses,
    record_episode,
    record_episode_status,
)
from vsigma.memory import MemoryStore
from vsigma.run import Agent, PauseChanged
from vsigma.threads import thread_lock


def _agent(store: MemoryStore) -> Agent:
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    return make_react_agent(FakeLLM([say("done")]), [], interceptor=itc)


def test_run_records_session_and_persona() -> None:
    store = InMemoryStore()
    run = _agent(store).run("task", session_id="s1", user_id="alice")

    owner = episode_owner(store, run.run_id)
    assert owner is not None
    assert (owner.session_id, owner.user_id) == ("s1", "alice")
    assert owner.task_id == run.task_id
    assert owner.created_at  # stamped at run start


def test_ownerless_run_is_indexed_with_null_ids() -> None:
    """An indexed episode with no owner is *not* the same as an unindexed one:
    ownership is known to be absent, rather than unknown."""
    store = InMemoryStore()
    run = _agent(store).run("task")

    owner = episode_owner(store, run.run_id)
    assert owner is not None
    assert (owner.session_id, owner.user_id) == (None, None)
    assert owner.task_id == run.run_id  # defaults to the run's own id
    assert episode_owner(store, "never-ran") is None


def test_task_id_is_persisted_for_correlation() -> None:
    """The correlation key must survive a restart: TrajectoryEvent carries it to
    the observer, but a TrajectoryLog is in-process."""
    store = InMemoryStore()
    ids = {_agent(store).run("nightly", task_id="nightly-report").run_id for _ in range(2)}

    owners = episode_owners(store)
    assert set(owners) == ids
    assert {o.task_id for o in owners.values()} == {"nightly-report"}


def test_resume_does_not_duplicate_or_rewrite_the_entry() -> None:
    store = InMemoryStore()
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    agent = make_guarded_react_agent(
        FakeLLM([call("missing_tool"), say("done")]), [], interceptor=itc, limit=1
    )
    run = agent.run("task", session_id="s1", user_id="alice")
    assert run.status == "escalated"
    before = episode_owner(store, run.run_id)

    run.resume("carry on")

    assert len(store.query(EPISODES_NS)) == 1
    assert episode_owner(store, run.run_id) == before


@pytest.mark.parametrize(
    "session_id,user_id",
    [("s2", "bob"), ("s1", "alice")],
    ids=["another-owner", "same-owner"],
)
def test_a_run_id_is_never_recycled(session_id: str, user_id: str) -> None:
    """Not only across owners. A used id also collides with the LangGraph thread,
    inherits any `deep:` marker (so the episode is born marked consolidated), and
    makes `trajectory:<run_id>` provenance ambiguous."""
    store = InMemoryStore()
    record_episode(store, "r1", session_id="s1", user_id="alice")

    with pytest.raises(ValueError, match="already exists"):
        record_episode(store, "r1", session_id=session_id, user_id=user_id)

    owner = episode_owner(store, "r1")
    assert owner is not None and (owner.session_id, owner.user_id) == ("s1", "alice")


def test_agent_run_rejects_a_used_run_id_before_the_graph_runs() -> None:
    store = InMemoryStore()
    first = _agent(store).run("task", session_id="s1", user_id="alice")

    with pytest.raises(ValueError, match="already exists"):
        _agent(store).run("task", run_id=first.run_id, session_id="s2", user_id="bob")

    trajectory = store.query(episode(first.run_id).namespace("context"))
    assert trajectory and all("bob" not in str(value) for _, value in trajectory)
    owner = episode_owner(store, first.run_id)
    assert owner is not None and owner.session_id == "s1"


def test_index_survives_a_restart_and_lists_every_episode(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    ids = {
        _agent(SQLiteStore(db)).run(f"task {n}", session_id=f"s{n}").run_id for n in (1, 2)
    }

    owners = episode_owners(SQLiteStore(db))
    assert set(owners) == ids
    assert {o.session_id for o in owners.values()} == {"s1", "s2"}


def test_index_is_operational_and_never_recallable() -> None:
    """`episodes/index` is outside the scope ladder, like `maintenance/state`."""
    state: State = {"run_id": "r1", "session_id": "s1", "user_id": "alice"}
    kinds: list[Kind] = ["knowledge", "context"]
    namespaces = {
        scope.namespace(kind) for scope in visible_scopes(state) for kind in kinds
    }
    assert EPISODES_NS not in namespaces


def test_episode_status_lifecycle_normal() -> None:
    store = InMemoryStore()
    agent = _agent(store)
    run = agent.run("task")
    assert get_episode_status(store, run.run_id) == "done"
    statuses = list_episode_statuses(store)
    assert statuses.get(run.run_id) == "done"


def test_episode_status_lifecycle_interrupt_and_resume() -> None:
    store = InMemoryStore()
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    agent = make_guarded_react_agent(
        FakeLLM([call("missing_tool"), say("done")]), [], interceptor=itc, limit=1
    )
    run = agent.run("task", session_id="s1", user_id="alice")
    assert run.status == "escalated"
    assert get_episode_status(store, run.run_id) == "escalated"

    run.resume("carry on")
    assert run.status == "done"
    assert get_episode_status(store, run.run_id) == "done"


def test_resume_never_writes_running() -> None:
    class TrackingStore(InMemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.status_writes: list[tuple[str, str]] = []

        def write(self, namespace: str, key: str, value: object) -> None:
            if namespace == EPISODE_STATUS_NS:
                self.status_writes.append((key, str(value)))
            super().write(namespace, key, value)

    store = TrackingStore()
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    agent = make_guarded_react_agent(
        FakeLLM([call("missing_tool"), say("done")]), [], interceptor=itc, limit=1
    )
    run = agent.run("task", session_id="s1", user_id="alice")
    assert run.status == "escalated"

    writes_before_resume = list(store.status_writes)
    assert writes_before_resume == [(run.run_id, "running"), (run.run_id, "escalated")]

    run.resume("carry on")
    assert run.status == "done"
    assert store.status_writes == [
        (run.run_id, "running"),
        (run.run_id, "escalated"),
        (run.run_id, "done"),
    ]


def test_episode_status_failed_on_run_exception() -> None:
    class ExplodingLLM:
        def complete(self, *args: object, **kwargs: object) -> Any:
            raise RuntimeError("llm crashed")

    store = InMemoryStore()
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    agent = make_react_agent(cast("Any", ExplodingLLM()), [], interceptor=itc)

    with pytest.raises(RuntimeError, match="llm crashed"):
        agent.run("task", run_id="exploding-run")

    assert get_episode_status(store, "exploding-run") == "failed"


def test_episode_status_resume_exception_leaves_escalated_if_pause_held() -> None:
    store = InMemoryStore()
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    agent = make_guarded_react_agent(
        FakeLLM([call("missing_tool")]), [], interceptor=itc, limit=1
    )
    run = agent.run("task", session_id="s1")
    assert run.status == "escalated"
    assert get_episode_status(store, run.run_id) == "escalated"

    with pytest.raises(PauseChanged):
        run.resume("carry on", interrupt_id="wrong-interrupt-id")

    assert get_episode_status(store, run.run_id) == "escalated"


def test_discarded_pause_recorded_abandoned_after_new_turn() -> None:
    store = InMemoryStore()
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    agent = make_guarded_react_agent(
        FakeLLM([call("missing_tool"), say("turn 2 done")]), [], interceptor=itc, limit=1
    )
    session = agent.session(session_id="s1")
    run1 = session.send("task 1")
    assert run1.status == "escalated"
    assert get_episode_status(store, run1.run_id) == "escalated"

    run2 = session.send("task 2", abandon=True)
    assert run2.status == "done"
    assert get_episode_status(store, run2.run_id) == "done"
    assert get_episode_status(store, run1.run_id) == "abandoned"


def test_run_end_and_session_end_records_abandoned() -> None:
    store = InMemoryStore()
    itc = Interceptor(memory=store, observer=TrajectoryLog())
    agent = make_guarded_react_agent(
        FakeLLM([call("missing_tool"), call("missing_tool")]), [], interceptor=itc, limit=1
    )

    # Paused one-shot run ends with abandon=True -> abandoned
    run = agent.run("oneshot task")
    assert get_episode_status(store, run.run_id) == "escalated"
    run.end(abandon=True)
    assert get_episode_status(store, run.run_id) == "abandoned"

    # Paused session ends with abandon=True -> abandoned
    sess = agent.session()
    r = sess.send("session task")
    assert get_episode_status(store, r.run_id) == "escalated"
    sess.end(abandon=True)
    assert get_episode_status(store, r.run_id) == "abandoned"

    # Completed run stays done when end() is called
    done_run = _agent(store).run("finished oneshot")
    assert get_episode_status(store, done_run.run_id) == "done"
    done_run.end()
    assert get_episode_status(store, done_run.run_id) == "done"


def test_final_status_never_overwritten_and_logs_warning() -> None:
    store = InMemoryStore()
    cp = InMemorySaver()
    with thread_lock(cp, "t1") as locked:
        record_episode_status(store, "r1", "done", locked=locked)
        result = record_episode_status(store, "r1", "failed", locked=locked)
        assert result == "done"
        assert get_episode_status(store, "r1") == "done"


def test_record_episode_status_requires_active_lock() -> None:
    store = InMemoryStore()
    cp = InMemorySaver()
    with thread_lock(cp, "t1") as locked:
        pass

    with pytest.raises(RuntimeError, match="released"):
        record_episode_status(store, "r1", "running", locked=locked)

