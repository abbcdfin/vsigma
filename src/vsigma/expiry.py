"""Thread expiry: bounding what the checkpointer keeps across sessions and runs."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver as _SqliteSaver

from .episodes import (
    SESSIONS_FORGET_NS,
    SESSIONS_OWNERS_NS,
    detach_session_episodes,
    episode_owner,
    get_episode_status,
    list_episode_statuses,
    record_episode_status,
)
from .memory import MaintainableStore, MemoryStore
from .scope import session
from .threads import LockedThread, SessionBusy, thread_lock

logger = logging.getLogger(__name__)

__all__ = [
    "ExpiredThread",
    "ExpiryPolicy",
    "ExpiryReport",
    "ThreadExpiry",
    "ThreadInfo",
    "checkpoint_id_to_datetime",
    "complete_forget_locked",
    "forget_session_locked",
]


def checkpoint_id_to_datetime(checkpoint_id: str) -> datetime:
    """Decode UTC timestamp from a UUIDv6 checkpoint ID.

    UUIDv6 bit layout:
    - Top 48 bits: time_high (32) and time_mid (16)
    - Next 4 bits: version (0110)
    - Next 12 bits: time_low
    Epoch: 1582-10-15 00:00:00 UTC (100ns ticks).
    """
    u = UUID(checkpoint_id)
    ticks = ((u.int >> 80) << 12) | ((u.int >> 64) & 0x0FFF)
    ts_sec = (ticks - 0x01B21DD213814000) / 1e7
    return datetime.fromtimestamp(ts_sec, tz=UTC)


@dataclass(frozen=True)
class ThreadInfo:
    """Metadata and activity state for one thread on a checkpointer."""

    thread_id: str
    kind: Literal["session", "run"]
    user_id: str | None
    run_id: str
    last_active: datetime
    idle: timedelta
    paused: bool
    checkpoint_id: str = ""


@dataclass(frozen=True)
class ExpiryPolicy:
    """Declarative rules for expiring idle threads on a checkpointer.

    Note: `keep_last` on its own limits only unpaused sessions. One-shot
    runs and paused threads do not expire under `keep_last` alone.
    """

    idle_ttl: timedelta | None = None
    keep_last: int | None = None
    paused_idle_ttl: timedelta | None = None
    run_ttl: timedelta | None = None

    def __post_init__(self) -> None:
        if (
            self.idle_ttl is None
            and self.keep_last is None
            and self.paused_idle_ttl is None
            and self.run_ttl is None
        ):
            raise ValueError(
                "ExpiryPolicy must set at least one limit "
                "(idle_ttl, keep_last, paused_idle_ttl, or run_ttl)"
            )
        for name, val in [
            ("idle_ttl", self.idle_ttl),
            ("paused_idle_ttl", self.paused_idle_ttl),
            ("run_ttl", self.run_ttl),
        ]:
            if val is not None and val <= timedelta(0):
                raise ValueError(f"{name} must be positive, got {val}")
        if self.keep_last is not None and self.keep_last < 1:
            raise ValueError(f"keep_last must be >= 1, got {self.keep_last}")


@dataclass(frozen=True)
class ExpiredThread:
    """A thread identified for expiry."""

    thread_id: str
    kind: Literal["session", "run"]
    user_id: str | None
    run_id: str
    reason: str


@dataclass(frozen=True)
class ExpiryReport:
    """Summary of operations performed by ThreadExpiry.run()."""

    expired: list[ExpiredThread]
    skipped_busy: list[str]
    skipped_changed: list[str]
    abandoned_runs: list[str]
    forgotten_sessions: list[str] = field(default_factory=list)


def _extract_recorded_cid(raw_val: object) -> str:
    if isinstance(raw_val, dict):
        cid = raw_val.get("checkpoint_id")
        return "" if cid is None else str(cid)
    return "" if raw_val is None else str(raw_val)


def _thread_checkpoint_info_locked(
    checkpointer: Any, thread_id: str
) -> tuple[str | None, str | None, bool]:
    """Read (latest_checkpoint_id, latest_run_id, is_paused) under proof-of-lock."""
    if checkpointer is None:
        return None, None, False

    if isinstance(checkpointer, InMemorySaver):
        root_cps = checkpointer.storage.get(thread_id, {}).get("", {})
        if not root_cps:
            return None, None, False
        latest_cid = str(max(root_cps.keys()))
        raw_meta = root_cps[latest_cid][1]
        meta: dict[str, Any] = {}
        if raw_meta:
            meta = cast("dict[str, Any]", checkpointer.serde.loads_typed(raw_meta))
        writes = checkpointer.writes.get((thread_id, "", latest_cid), {})
        is_paused = any(w[1] == "__interrupt__" for w in writes.values())
        run_id = str(meta.get("run_id")) if "run_id" in meta else None
        return latest_cid, run_id, is_paused

    if isinstance(checkpointer, _SqliteSaver):
        with checkpointer.lock:
            if (
                hasattr(checkpointer, "setup")
                and hasattr(checkpointer, "is_setup")
                and not checkpointer.is_setup
            ):
                checkpointer.setup()
            cursor = checkpointer.conn.cursor()
            cursor.execute(
                """
                SELECT
                    c.checkpoint_id,
                    c.metadata,
                    EXISTS(
                        SELECT 1 FROM writes w
                        WHERE w.thread_id = c.thread_id
                          AND w.checkpoint_ns = ''
                          AND w.checkpoint_id = c.checkpoint_id
                          AND w.channel = '__interrupt__'
                    ) AS is_paused
                FROM checkpoints c
                WHERE c.thread_id = ? AND c.checkpoint_ns = ''
                ORDER BY c.checkpoint_id DESC
                LIMIT 1
                """,
                (thread_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None, None, False
            cid = str(row[0])
            raw_meta, is_paused = row[1], bool(row[2])
            meta = {}
            if raw_meta:
                meta = json.loads(
                    raw_meta.decode("utf-8")
                    if isinstance(raw_meta, (bytes, bytearray))
                    else str(raw_meta)
                )
            run_id = str(meta.get("run_id")) if "run_id" in meta else None
            return cid, run_id, is_paused

    cp_tuple = checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
    if cp_tuple is None or not cp_tuple.checkpoint:
        return None, None, False
    cid = str(cp_tuple.checkpoint.get("id", ""))
    meta = dict(cp_tuple.metadata or {})
    pending = cp_tuple.pending_writes or []
    is_paused = any(w[1] == "__interrupt__" for w in pending)
    run_id = str(meta.get("run_id")) if "run_id" in meta else None
    return (cid or None), run_id, is_paused


def _delete_checkpointer_thread_locked(
    checkpointer: Any,
    locked: LockedThread,
    expected_checkpoint_id: str | None = None,
) -> bool:
    """Delete a thread from `checkpointer` under `locked`."""
    locked.check_active()
    if locked.checkpointer is not checkpointer:
        raise ValueError(
            f"LockedThread checkpointer {locked.checkpointer!r} "
            f"does not match {checkpointer!r}"
        )
    if checkpointer is None:
        return True

    if isinstance(checkpointer, InMemorySaver):
        root_cps = checkpointer.storage.get(locked.thread_id, {}).get("", {})
        latest_cid = str(max(root_cps.keys())) if root_cps else None
        if expected_checkpoint_id is not None and latest_cid != expected_checkpoint_id:
            return False
        checkpointer.delete_thread(locked.thread_id)
        return True

    if isinstance(checkpointer, _SqliteSaver):
        with checkpointer.lock:
            if (
                hasattr(checkpointer, "setup")
                and hasattr(checkpointer, "is_setup")
                and not checkpointer.is_setup
            ):
                checkpointer.setup()
            if checkpointer.conn.in_transaction:
                checkpointer.conn.commit()
            cursor = checkpointer.conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                if expected_checkpoint_id is not None:
                    cursor.execute(
                        "SELECT MAX(checkpoint_id) FROM checkpoints "
                        "WHERE thread_id = ? AND checkpoint_ns = ''",
                        (locked.thread_id,),
                    )
                    row = cursor.fetchone()
                    max_id = str(row[0]) if row and row[0] is not None else None
                    if max_id != expected_checkpoint_id:
                        checkpointer.conn.rollback()
                        return False
                cursor.execute(
                    "DELETE FROM checkpoints WHERE thread_id = ?",
                    (locked.thread_id,),
                )
                cursor.execute(
                    "DELETE FROM writes WHERE thread_id = ?",
                    (locked.thread_id,),
                )
                checkpointer.conn.commit()
                return True
            except Exception:
                checkpointer.conn.rollback()
                raise

    checkpointer.delete_thread(locked.thread_id)
    return True


def _delete_session_memory(store: MaintainableStore, session_id: str) -> None:
    """Delete all records in `session:<id>/context` and `session:<id>/knowledge`."""
    s_scope = session(session_id)
    for kind in ("context", "knowledge"):
        ns = s_scope.namespace(kind)
        keys = [k for k, _ in store.query(ns)]
        if keys:
            store.delete(ns, keys)


def complete_forget_locked(
    checkpointer: Any,
    store: MaintainableStore,
    locked: LockedThread,
    *,
    abandoned_runs: list[str] | None = None,
) -> bool:
    """Complete a pending `sessions/forget/<session_id>` record under `locked`.

    Branches on the thread's latest root checkpoint (scope-lifespans §C.2):
    - matches recorded checkpoint id: crash happened before step 2 -> run steps 2–5
    - none (thread is gone): step 2 already ran -> run steps 3–5
    - another checkpoint id: thread moved on -> drop the record, delete nothing

    Returns `True` when the session was forgotten (steps 2–5 or 3–5), `False` otherwise.
    Safe to repeat if interrupted at any step.
    """
    locked.check_active()
    session_id = locked.thread_id
    raw_val = store.read(SESSIONS_FORGET_NS, session_id)
    if raw_val is None:
        return False

    recorded_cid = _extract_recorded_cid(raw_val)
    current_cid, latest_run_id, is_paused = _thread_checkpoint_info_locked(
        checkpointer, session_id
    )

    if current_cid is not None and current_cid != recorded_cid:
        store.delete(SESSIONS_FORGET_NS, [session_id])
        return False

    if current_cid is not None:
        if not _delete_checkpointer_thread_locked(
            checkpointer, locked, expected_checkpoint_id=current_cid
        ):
            raise RuntimeError(
                f"thread {session_id!r} changed while holding thread_lock"
            )
        if is_paused and latest_run_id:
            record_episode_status(store, latest_run_id, "abandoned", locked=locked)
            if abandoned_runs is not None and latest_run_id not in abandoned_runs:
                abandoned_runs.append(latest_run_id)

    _delete_session_memory(store, session_id)
    detach_session_episodes(store, session_id)
    store.delete(SESSIONS_OWNERS_NS, [session_id])
    store.delete(SESSIONS_FORGET_NS, [session_id])
    return True


def forget_session_locked(
    checkpointer: Any,
    store: MaintainableStore,
    locked: LockedThread,
    *,
    expected_checkpoint_id: str | None = None,
    abandoned_runs: list[str] | None = None,
) -> bool:
    """Execute the 5-step session-forget sequence under `locked` (scope-lifespans §C.2).

    1. Write `sessions/forget/<session_id>` with the thread's latest checkpoint id.
    2. Delete the thread (and record an expired/ended paused run as `abandoned`).
    3. Delete `session:<id>/context` and `session:<id>/knowledge`.
    4. Detach the session's episodes in `episodes/index` (`session_id = None`) and
       delete `sessions/owners/<session_id>`.
    5. Delete the `sessions/forget/<session_id>` record.
    """
    locked.check_active()
    session_id = locked.thread_id
    current_cid, latest_run_id, is_paused = _thread_checkpoint_info_locked(
        checkpointer, session_id
    )
    if expected_checkpoint_id is not None and current_cid != expected_checkpoint_id:
        return False

    # Step 1: record intent before deleting anything
    store.write(SESSIONS_FORGET_NS, session_id, current_cid or "")

    # Step 2: delete the thread, and record a paused run as abandoned before detaching
    if current_cid is not None:
        if not _delete_checkpointer_thread_locked(
            checkpointer, locked, expected_checkpoint_id=current_cid
        ):
            raise RuntimeError(
                f"thread {session_id!r} changed while holding thread_lock"
            )
        if is_paused and latest_run_id:
            record_episode_status(store, latest_run_id, "abandoned", locked=locked)
            if abandoned_runs is not None and latest_run_id not in abandoned_runs:
                abandoned_runs.append(latest_run_id)

    # Step 3: delete session context and knowledge
    _delete_session_memory(store, session_id)

    # Step 4: detach session episodes and remove any saved session owner
    detach_session_episodes(store, session_id)
    store.delete(SESSIONS_OWNERS_NS, [session_id])

    # Step 5: delete the forget record
    store.delete(SESSIONS_FORGET_NS, [session_id])
    return True


class ThreadExpiry:
    """Scans and expires idle or obsolete threads on a checkpointer."""

    def __init__(
        self,
        checkpointer: Any,
        policy: ExpiryPolicy | None = None,
        store: MemoryStore | None = None,
    ) -> None:
        if isinstance(checkpointer, InMemorySaver):
            self._backend: Literal["memory", "sqlite"] = "memory"
        elif isinstance(checkpointer, _SqliteSaver):
            self._backend = "sqlite"
        else:
            name = type(checkpointer).__name__
            raise NotImplementedError(
                f"ThreadExpiry is not supported for checkpointer {name}"
            )
        if store is not None and not isinstance(store, MaintainableStore):
            raise TypeError(
                f"ThreadExpiry requires a MaintainableStore, got {type(store).__name__}"
            )
        self.checkpointer = checkpointer
        self.policy = policy
        self.store: MaintainableStore | None = store

    def threads(self, *, now: datetime | None = None) -> Iterator[ThreadInfo]:
        """Scan threads on the checkpointer without deserializing state blobs."""
        ref_now = now or datetime.now(UTC)
        if self._backend == "memory":
            yield from self._threads_in_memory(ref_now)
        else:
            yield from self._threads_sqlite(ref_now)

    def _threads_in_memory(self, now: datetime) -> Iterator[ThreadInfo]:
        cp = cast(InMemorySaver, self.checkpointer)
        for thread_id, ns_dict in list(cp.storage.items()):
            root_cps = ns_dict.get("", {})
            if not root_cps:
                continue
            latest_cid = max(root_cps.keys())
            cp_tuple = root_cps[latest_cid]
            raw_meta = cp_tuple[1]
            meta: dict[str, Any] = {}
            if raw_meta:
                meta = cast("dict[str, Any]", cp.serde.loads_typed(raw_meta))

            last_active = checkpoint_id_to_datetime(latest_cid)
            idle = max(now - last_active, timedelta(0))

            writes = cp.writes.get((thread_id, "", latest_cid), {})
            is_paused = any(w[1] == "__interrupt__" for w in writes.values())

            kind: Literal["session", "run"] = (
                "session" if meta.get("session_id") is not None else "run"
            )
            user_id = meta.get("user_id")
            run_id = str(meta.get("run_id", ""))

            yield ThreadInfo(
                thread_id=thread_id,
                kind=kind,
                user_id=None if user_id is None else str(user_id),
                run_id=run_id,
                last_active=last_active,
                idle=idle,
                paused=is_paused,
                checkpoint_id=latest_cid,
            )

    def _threads_sqlite(self, now: datetime) -> Iterator[ThreadInfo]:
        cp = self.checkpointer
        with cp.lock:
            if hasattr(cp, "setup") and hasattr(cp, "is_setup") and not cp.is_setup:
                cp.setup()
            cursor = cp.conn.cursor()
            cursor.execute(
                """
                SELECT
                    c.thread_id,
                    c.checkpoint_id,
                    c.metadata,
                    EXISTS(
                        SELECT 1 FROM writes w
                        WHERE w.thread_id = c.thread_id
                          AND w.checkpoint_ns = ''
                          AND w.checkpoint_id = c.checkpoint_id
                          AND w.channel = '__interrupt__'
                    ) AS is_paused
                FROM checkpoints c
                JOIN (
                    SELECT thread_id, MAX(checkpoint_id) AS max_cid
                    FROM checkpoints
                    WHERE checkpoint_ns = ''
                    GROUP BY thread_id
                ) m ON c.thread_id = m.thread_id AND c.checkpoint_id = m.max_cid
                WHERE c.checkpoint_ns = ''
                ORDER BY c.thread_id
                """
            )
            rows = cursor.fetchall()

        for row in rows:
            thread_id, checkpoint_id, raw_meta, is_paused = (
                str(row[0]),
                str(row[1]),
                row[2],
                bool(row[3]),
            )
            meta: dict[str, Any] = {}
            if raw_meta:
                meta = json.loads(raw_meta)

            last_active = checkpoint_id_to_datetime(checkpoint_id)
            idle = max(now - last_active, timedelta(0))
            kind: Literal["session", "run"] = (
                "session" if meta.get("session_id") is not None else "run"
            )
            user_id = meta.get("user_id")
            run_id = str(meta.get("run_id", ""))

            yield ThreadInfo(
                thread_id=thread_id,
                kind=kind,
                user_id=None if user_id is None else str(user_id),
                run_id=run_id,
                last_active=last_active,
                idle=idle,
                paused=is_paused,
                checkpoint_id=checkpoint_id,
            )

    def _evaluate_policy(
        self, threads: list[ThreadInfo]
    ) -> list[tuple[ThreadInfo, str]]:
        if self.policy is None:
            return []
        policy = self.policy
        candidates: list[tuple[ThreadInfo, str]] = []
        already_candidate: set[str] = set()

        for t in threads:
            if t.paused:
                paused_ttl = policy.paused_idle_ttl or policy.idle_ttl
                if paused_ttl is not None and t.idle > paused_ttl:
                    reason = (
                        "paused_idle_ttl"
                        if policy.paused_idle_ttl is not None
                        else "idle_ttl"
                    )
                    candidates.append((t, reason))
                    already_candidate.add(t.thread_id)
            elif t.kind == "run":
                run_ttl = policy.run_ttl or policy.idle_ttl
                if run_ttl is not None and t.idle > run_ttl:
                    reason = (
                        "run_ttl" if policy.run_ttl is not None else "idle_ttl"
                    )
                    candidates.append((t, reason))
                    already_candidate.add(t.thread_id)
            else:
                if policy.idle_ttl is not None and t.idle > policy.idle_ttl:
                    candidates.append((t, "idle_ttl"))
                    already_candidate.add(t.thread_id)

        if policy.keep_last is not None:
            user_groups: dict[str | None, list[ThreadInfo]] = {}
            for t in threads:
                if t.kind == "session" and not t.paused:
                    user_groups.setdefault(t.user_id, []).append(t)

            for group in user_groups.values():
                sorted_group = sorted(group, key=lambda x: x.last_active, reverse=True)
                for excess in sorted_group[policy.keep_last :]:
                    if excess.thread_id not in already_candidate:
                        candidates.append((excess, "keep_last"))
                        already_candidate.add(excess.thread_id)

        return candidates

    def _delete_thread_locked(
        self, locked: LockedThread, expected_checkpoint_id: str
    ) -> bool:
        return _delete_checkpointer_thread_locked(
            self.checkpointer, locked, expected_checkpoint_id=expected_checkpoint_id
        )

    def _thread_state_locked(
        self, thread_id: str
    ) -> tuple[bool, str | None, bool]:
        """Check (exists, latest_run_id, is_paused) under proof-of-lock."""
        cid, run_id, is_paused = _thread_checkpoint_info_locked(
            self.checkpointer, thread_id
        )
        return cid is not None, run_id, is_paused

    def run(
        self,
        *,
        now: datetime | None = None,
        dry_run: bool = False,
    ) -> ExpiryReport:
        """Scan checkpointer, evaluate policy, and delete expired threads."""
        expired_list: list[ExpiredThread] = []
        skipped_busy: list[str] = []
        skipped_changed: list[str] = []
        abandoned_runs: list[str] = []
        forgotten_sessions: list[str] = []

        # 1. Complete any pending forget records left by an earlier run
        if self.store is not None:
            for session_id, _raw_val in list(self.store.query(SESSIONS_FORGET_NS)):
                try:
                    with thread_lock(self.checkpointer, session_id) as locked:
                        if dry_run:
                            cur_val = self.store.read(SESSIONS_FORGET_NS, session_id)
                            if cur_val is None:
                                continue
                            rec_cid = _extract_recorded_cid(cur_val)
                            cur_cid, lat_rid, is_p = _thread_checkpoint_info_locked(
                                self.checkpointer, session_id
                            )
                            if cur_cid is None or cur_cid == rec_cid:
                                if session_id not in forgotten_sessions:
                                    forgotten_sessions.append(session_id)
                                if (
                                    cur_cid is not None
                                    and is_p
                                    and lat_rid
                                    and lat_rid not in abandoned_runs
                                ):
                                    abandoned_runs.append(lat_rid)
                        else:
                            forgotten = complete_forget_locked(
                                self.checkpointer,
                                self.store,
                                locked,
                                abandoned_runs=abandoned_runs,
                            )
                            if forgotten and session_id not in forgotten_sessions:
                                forgotten_sessions.append(session_id)
                except SessionBusy:
                    continue

        ref_now = now or datetime.now(UTC)
        all_threads = list(self.threads(now=ref_now))
        if dry_run and forgotten_sessions:
            all_threads = [
                t for t in all_threads if t.thread_id not in forgotten_sessions
            ]
        to_expire = (
            self._evaluate_policy(all_threads) if self.policy is not None else []
        )

        if dry_run:
            for t, reason in to_expire:
                expired_list.append(
                    ExpiredThread(
                        thread_id=t.thread_id,
                        kind=t.kind,
                        user_id=t.user_id,
                        run_id=t.run_id,
                        reason=reason,
                    )
                )
                if self.store is not None:
                    if t.paused and t.run_id and t.run_id not in abandoned_runs:
                        abandoned_runs.append(t.run_id)
                    if t.kind == "session" and t.thread_id not in forgotten_sessions:
                        forgotten_sessions.append(t.thread_id)
        else:
            for t, reason in to_expire:
                try:
                    with thread_lock(self.checkpointer, t.thread_id) as locked:
                        if self.store is not None and t.kind == "session":
                            complete_forget_locked(
                                self.checkpointer,
                                self.store,
                                locked,
                                abandoned_runs=abandoned_runs,
                            )
                            deleted = forget_session_locked(
                                self.checkpointer,
                                self.store,
                                locked,
                                expected_checkpoint_id=t.checkpoint_id,
                                abandoned_runs=abandoned_runs,
                            )
                            if deleted:
                                expired_list.append(
                                    ExpiredThread(
                                        thread_id=t.thread_id,
                                        kind=t.kind,
                                        user_id=t.user_id,
                                        run_id=t.run_id,
                                        reason=reason,
                                    )
                                )
                                if t.thread_id not in forgotten_sessions:
                                    forgotten_sessions.append(t.thread_id)
                            else:
                                skipped_changed.append(t.thread_id)
                        else:
                            deleted = self._delete_thread_locked(
                                locked, t.checkpoint_id
                            )
                            if deleted:
                                expired_list.append(
                                    ExpiredThread(
                                        thread_id=t.thread_id,
                                        kind=t.kind,
                                        user_id=t.user_id,
                                        run_id=t.run_id,
                                        reason=reason,
                                    )
                                )
                                if (
                                    self.store is not None
                                    and t.paused
                                    and t.run_id
                                    and t.run_id not in abandoned_runs
                                ):
                                    record_episode_status(
                                        self.store,
                                        t.run_id,
                                        "abandoned",
                                        locked=locked,
                                    )
                                    abandoned_runs.append(t.run_id)
                            else:
                                skipped_changed.append(t.thread_id)
                except SessionBusy:
                    skipped_busy.append(t.thread_id)

        # Sweep unfinished episodes if store is configured
        if self.store is not None:
            statuses = list_episode_statuses(self.store)
            unfinished = [
                (rid, st)
                for rid, st in statuses.items()
                if st in ("running", "escalated")
            ]
            for rid, _listed_st in unfinished:
                owner = episode_owner(self.store, rid)
                if owner is None:
                    logger.warning(
                        "Episode %r has status %r but no episodes/index entry; skipping",
                        rid,
                        _listed_st,
                    )
                    continue

                thread_id = owner.session_id or owner.run_id
                try:
                    with thread_lock(self.checkpointer, thread_id) as locked:
                        # Re-read status under the lock to prevent acting on stale state
                        st = get_episode_status(self.store, rid)
                        if st == "running":
                            should_abandon = True
                        elif st == "escalated":
                            exists, latest_run_id, is_paused = (
                                self._thread_state_locked(thread_id)
                            )
                            should_abandon = (
                                not exists or latest_run_id != rid or not is_paused
                            )
                        else:
                            should_abandon = False

                        if should_abandon:
                            if rid not in abandoned_runs:
                                abandoned_runs.append(rid)
                            if not dry_run:
                                record_episode_status(
                                    self.store, rid, "abandoned", locked=locked
                                )
                except SessionBusy:
                    continue

        return ExpiryReport(
            expired=expired_list,
            skipped_busy=skipped_busy,
            skipped_changed=skipped_changed,
            abandoned_runs=abandoned_runs,
            forgotten_sessions=forgotten_sessions,
        )
