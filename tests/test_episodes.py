"""Durable episode ownership (docs/proposals/memory-scope-promotion.md §B.2)."""

from pathlib import Path

import pytest

from vsigma import (
    FakeLLM,
    InMemoryStore,
    Kind,
    Services,
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
from vsigma.episodes import EPISODES_NS, episode_owner, episode_owners, record_episode
from vsigma.memory import MemoryStore
from vsigma.run import Agent


def _agent(store: MemoryStore) -> Agent:
    svc = Services(memory=store, observer=TrajectoryLog())
    return make_react_agent(FakeLLM([say("done")]), [], services=svc)


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
    svc = Services(memory=store, observer=TrajectoryLog())
    agent = make_guarded_react_agent(
        FakeLLM([call("missing_tool"), say("done")]), [], services=svc, limit=1
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
