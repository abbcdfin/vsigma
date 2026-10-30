"""Thread expiry: bounding what the checkpointer keeps across sessions and runs."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from langgraph.checkpoint.memory import InMemorySaver

from .episodes import episode_owner, list_episode_statuses, record_episode_status
from .memory import MemoryStore
from .threads import LockedThread, SessionBusy, thread_lock

logger = logging.getLogger(__name__)

try:
    from langgraph.checkpoint.sqlite import SqliteSaver as _SqliteSaver
except ImportError:
    _SqliteSaver = None  # type: ignore[misc,assignment]

__all__ = [
    "ExpiredThread",
    "ExpiryPolicy",
    "ExpiryReport",
    "ThreadExpiry",
    "ThreadInfo",
    "checkpoint_id_to_datetime",
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
    recovered_episodes: list[str]

    @property
    def abandoned_runs(self) -> list[str]:
        """All run IDs recorded as abandoned by this run."""
        return self.recovered_episodes


class ThreadExpiry:
    """Scans and expires idle or obsolete threads on a checkpointer."""

    def __init__(
        self,
        checkpointer: Any,
        policy: ExpiryPolicy,
        store: MemoryStore | None = None,
    ) -> None:
        if isinstance(checkpointer, InMemorySaver):
            self._backend: Literal["memory", "sqlite"] = "memory"
        elif _SqliteSaver is not None and isinstance(checkpointer, _SqliteSaver):
            self._backend = "sqlite"
        else:
            name = type(checkpointer).__name__
            raise NotImplementedError(
                f"ThreadExpiry is not supported for checkpointer {name}"
            )
        self.checkpointer = checkpointer
        self.policy = policy
        self.store = store

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
        candidates: list[tuple[ThreadInfo, str]] = []
        already_candidate: set[str] = set()

        for t in threads:
            if t.paused:
                paused_ttl = self.policy.paused_idle_ttl or self.policy.idle_ttl
                if paused_ttl is not None and t.idle > paused_ttl:
                    reason = (
                        "paused_idle_ttl"
                        if self.policy.paused_idle_ttl is not None
                        else "idle_ttl"
                    )
                    candidates.append((t, reason))
                    already_candidate.add(t.thread_id)
            elif t.kind == "run":
                run_ttl = self.policy.run_ttl or self.policy.idle_ttl
                if run_ttl is not None and t.idle > run_ttl:
                    reason = (
                        "run_ttl" if self.policy.run_ttl is not None else "idle_ttl"
                    )
                    candidates.append((t, reason))
                    already_candidate.add(t.thread_id)
            else:
                if self.policy.idle_ttl is not None and t.idle > self.policy.idle_ttl:
                    candidates.append((t, "idle_ttl"))
                    already_candidate.add(t.thread_id)

        if self.policy.keep_last is not None:
            user_groups: dict[str | None, list[ThreadInfo]] = {}
            for t in threads:
                if t.kind == "session" and not t.paused:
                    user_groups.setdefault(t.user_id, []).append(t)

            for group in user_groups.values():
                sorted_group = sorted(group, key=lambda x: x.last_active, reverse=True)
                for excess in sorted_group[self.policy.keep_last :]:
                    if excess.thread_id not in already_candidate:
                        candidates.append((excess, "keep_last"))
                        already_candidate.add(excess.thread_id)

        return candidates

    def _delete_thread_locked(
        self, locked: LockedThread, expected_checkpoint_id: str
    ) -> bool:
        locked.check_active()
        if locked.checkpointer is not self.checkpointer:
            raise ValueError(
                f"LockedThread checkpointer {locked.checkpointer!r} "
                f"does not match {self.checkpointer!r}"
            )

        if self._backend == "memory":
            cp = cast(InMemorySaver, self.checkpointer)
            root_cps = cp.storage.get(locked.thread_id, {}).get("", {})
            latest_cid = max(root_cps.keys()) if root_cps else None
            if latest_cid != expected_checkpoint_id:
                return False
            cp.delete_thread(locked.thread_id)
            return True

        cp = self.checkpointer
        with cp.lock:
            if hasattr(cp, "setup") and hasattr(cp, "is_setup") and not cp.is_setup:
                cp.setup()
            if cp.conn.in_transaction:
                cp.conn.commit()
            cursor = cp.conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                cursor.execute(
                    "SELECT MAX(checkpoint_id) FROM checkpoints "
                    "WHERE thread_id = ? AND checkpoint_ns = ''",
                    (locked.thread_id,),
                )
                row = cursor.fetchone()
                max_id = str(row[0]) if row and row[0] is not None else None
                if max_id != expected_checkpoint_id:
                    cp.conn.rollback()
                    return False
                cursor.execute(
                    "DELETE FROM checkpoints WHERE thread_id = ?",
                    (locked.thread_id,),
                )
                cursor.execute(
                    "DELETE FROM writes WHERE thread_id = ?",
                    (locked.thread_id,),
                )
                cp.conn.commit()
                return True
            except Exception:
                cp.conn.rollback()
                raise

    def _thread_state_locked(
        self, thread_id: str
    ) -> tuple[bool, str | None, bool]:
        """Check (exists, latest_run_id, is_paused) under proof-of-lock."""
        if self._backend == "memory":
            cp = cast(InMemorySaver, self.checkpointer)
            root_cps = cp.storage.get(thread_id, {}).get("", {})
            if not root_cps:
                return False, None, False
            latest_cid = max(root_cps.keys())
            raw_meta = root_cps[latest_cid][1]
            meta: dict[str, Any] = {}
            if raw_meta:
                meta = cast("dict[str, Any]", cp.serde.loads_typed(raw_meta))
            writes = cp.writes.get((thread_id, "", latest_cid), {})
            is_paused = any(w[1] == "__interrupt__" for w in writes.values())
            run_id = str(meta.get("run_id")) if "run_id" in meta else None
            return True, run_id, is_paused

        cp = self.checkpointer
        with cp.lock:
            if hasattr(cp, "setup") and hasattr(cp, "is_setup") and not cp.is_setup:
                cp.setup()
            cursor = cp.conn.cursor()
            cursor.execute(
                """
                SELECT
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
                return False, None, False
            raw_meta, is_paused = row[0], bool(row[1])
            meta = {}
            if raw_meta:
                meta = json.loads(
                    raw_meta.decode("utf-8")
                    if isinstance(raw_meta, (bytes, bytearray))
                    else str(raw_meta)
                )
            run_id = str(meta.get("run_id")) if "run_id" in meta else None
            return True, run_id, is_paused

    def run(
        self,
        *,
        now: datetime | None = None,
        dry_run: bool = False,
    ) -> ExpiryReport:
        """Scan checkpointer, evaluate policy, and delete expired threads."""
        ref_now = now or datetime.now(UTC)
        all_threads = list(self.threads(now=ref_now))
        to_expire = self._evaluate_policy(all_threads)

        expired_list: list[ExpiredThread] = []
        skipped_busy: list[str] = []
        skipped_changed: list[str] = []
        recovered_episodes: list[str] = []

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
                if self.store is not None and t.paused and t.run_id:
                    recovered_episodes.append(t.run_id)
        else:
            for t, reason in to_expire:
                try:
                    with thread_lock(self.checkpointer, t.thread_id) as locked:
                        deleted = self._delete_thread_locked(locked, t.checkpoint_id)
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
                            if self.store is not None and t.paused and t.run_id:
                                record_episode_status(
                                    self.store, t.run_id, "abandoned", locked=locked
                                )
                                recovered_episodes.append(t.run_id)
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
            for rid, st in unfinished:
                owner = episode_owner(self.store, rid)
                if owner is None:
                    logger.warning(
                        "Episode %r has status %r but no episodes/index entry; skipping",
                        rid,
                        st,
                    )
                    continue

                thread_id = owner.session_id or owner.run_id
                try:
                    with thread_lock(self.checkpointer, thread_id) as locked:
                        should_abandon = False
                        if st == "running":
                            should_abandon = True
                        elif st == "escalated":
                            exists, latest_run_id, is_paused = (
                                self._thread_state_locked(thread_id)
                            )
                            if not exists or latest_run_id != rid or not is_paused:
                                should_abandon = True

                        if should_abandon:
                            if rid not in recovered_episodes:
                                recovered_episodes.append(rid)
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
            recovered_episodes=recovered_episodes,
        )
