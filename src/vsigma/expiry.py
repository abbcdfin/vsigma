"""Thread expiry: bounding what the checkpointer keeps across sessions and runs."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from langgraph.checkpoint.memory import InMemorySaver

from .memory import MemoryStore

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
            )
