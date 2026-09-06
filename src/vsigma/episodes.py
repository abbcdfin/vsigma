"""Durable episode ownership — the `episodes/index` operational namespace.

Design: docs/proposals/memory-scope-promotion.md §B.2. Episodic context is
keyed on `run_id` alone (`seam.py`), so *who* an episode belonged to — which
session, which persona — is not reconstructable from the store. Without that,
the maintenance pass cannot enforce a source boundary: one pass over "every
unprocessed episode" reads every tenant's trajectory.

This index records ownership at run start:

    run_id -> {session_id, user_id, created_at}

Four fields, deliberately. A separate participant/tenant axis was **rejected
for v0** (§B.2): persona and session *are* vsigma's boundaries.

Like `maintenance/state` this is an *operational* namespace — it is not a rung
of the scope ladder, `visible_scopes()` never yields it, and so an index entry
can never reach a prompt.

Entries are written by `Agent.run`, which is the actual run boundary; `resume`
does not rewrite one, since a resumed run *is* the same episode
(running-context-model.md: an episode is one state-graph run for one task arc,
**including its resumes**).

**A `run_id` is never recycled** — registering a used one is rejected here, and
not only across owners. The id is four things at once, and reuse breaks each:
the LangGraph thread (a second run merges into the first's checkpoint), the
episodic namespace (two runs' trajectory interleaves under one key), the
maintenance cursor (a `deep:<run_id>` marker already present means the new
episode is born marked consolidated and is silently never distilled), and
`source_ref="trajectory:<run_id>"` on every record already derived from the
first episode. Nothing releases an id: the deep pass *adds* a permanent marker
rather than retiring anything. Correlate related episodes with `task_id`;
continue one with `resume()`.
An episode driven by invoking a wrapped graph directly, without `Agent`, still
gets trajectory but no index entry — its ownership is *unknown*, which the
source filter treats as fails-closed (§B.3: unindexed episodes are matched only
by `--all-sources`, never by a specific `--source`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .memory import MemoryStore

#: Operational bookkeeping namespace, outside the scope ladder — see the
#: module docstring and `maintenance.MAINTENANCE_NS`, which it mirrors.
EPISODES_NS = "episodes/index"


@dataclass(frozen=True)
class EpisodeOwner:
    """Who an episode belonged to. `None` ids mean the run genuinely had no
    session / no serving persona — distinct from *unindexed*, which is a
    missing entry and means ownership is unknown."""

    run_id: str
    session_id: str | None
    user_id: str | None
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "created_at": self.created_at,
        }


def _from_stored(run_id: str, value: object) -> EpisodeOwner | None:
    if not isinstance(value, dict):
        return None
    session_id = value.get("session_id")
    user_id = value.get("user_id")
    return EpisodeOwner(
        run_id=run_id,
        session_id=None if session_id is None else str(session_id),
        user_id=None if user_id is None else str(user_id),
        created_at=str(value.get("created_at", "")),
    )


def record_episode(
    store: MemoryStore,
    run_id: str,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
) -> EpisodeOwner:
    """Register a new episode. **A `run_id` is never reused, by any owner.**

    Registering an id that already exists raises `ValueError`, whatever the
    ownership. A `run_id` is one episode for its whole lifetime — it is the
    LangGraph thread id, the episodic namespace key, the maintenance cursor
    entry, and the `source_ref` of every record distilled from the episode — so
    reuse corrupts four things at once (see the module docstring). `resume()`
    continues an episode without re-registering it; a retry is a *new* episode
    with a new id, correlated to the first by `task_id`.

    TODO(concurrency): this read-then-write is not atomic — `SQLiteStore` locks
    each operation separately, so two concurrent registrations of the same
    caller-supplied id can both observe it as free. Harmless for generated ids
    (uuid4), and the real fix is a create-if-absent store operation, which
    means widening the deliberately-minimal `MemoryStore` protocol. Out of
    scope for Phase 4.7 (proposal §B.2: do not widen a safety fix).
    """
    existing = _from_stored(run_id, store.read(EPISODES_NS, run_id))
    if existing is not None:
        raise ValueError(
            f"episode {run_id!r} already exists "
            f"(session={existing.session_id!r} user={existing.user_id!r}, "
            f"created {existing.created_at}). A run_id is one episode for its "
            "whole lifetime and is never recycled: use a fresh run_id, pass "
            "task_id= to correlate related episodes, or resume() to continue "
            "this one."
        )
    owner = EpisodeOwner(
        run_id=run_id,
        session_id=session_id,
        user_id=user_id,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    store.write(EPISODES_NS, run_id, owner.to_dict())
    return owner


def episode_owner(store: MemoryStore, run_id: str) -> EpisodeOwner | None:
    """Ownership of one episode, or `None` when it is unindexed (unknown)."""
    return _from_stored(run_id, store.read(EPISODES_NS, run_id))


def episode_owners(store: MemoryStore) -> dict[str, EpisodeOwner]:
    """Every indexed episode, by `run_id`."""
    owners = {}
    for run_id, value in store.query(EPISODES_NS):
        owner = _from_stored(run_id, value)
        if owner is not None:
            owners[run_id] = owner
    return owners
