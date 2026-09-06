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
does not rewrite one, since a resumed run keeps its `run_id` and its ownership.
Ownership is immutable: re-running a used `run_id` under a different owner is
rejected, because the trajectory would land in the first owner's episode.
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
    """Record who this episode belongs to. **Ownership is immutable.**

    Re-recording the same ownership is a no-op returning the existing entry, so
    a resumed or re-driven run keeps its original `created_at`. Re-recording
    *different* ownership raises `ValueError`: episodic context is keyed on
    `run_id` alone (`seam.py`), so a second tenant running under a used
    `run_id` would write its trajectory into the first tenant's episode. The
    index would then attribute it to the first tenant — a false attribution,
    which is worse than the unknown one §B.3 already fails closed on. Reject it
    at the boundary rather than record a boundary that is not true.
    """
    existing = _from_stored(run_id, store.read(EPISODES_NS, run_id))
    if existing is not None:
        if (existing.session_id, existing.user_id) != (session_id, user_id):
            raise ValueError(
                f"episode {run_id!r} is already owned by "
                f"session={existing.session_id!r} user={existing.user_id!r}; "
                f"refusing to re-run it as session={session_id!r} user={user_id!r}. "
                "Ownership is immutable — use a fresh run_id."
            )
        return existing
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
