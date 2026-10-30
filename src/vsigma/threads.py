"""Per-thread concurrency lock for checkpointer operations."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from weakref import WeakKeyDictionary

__all__ = ["LockedThread", "SessionBusy", "thread_lock"]


class SessionBusy(RuntimeError):
    """Raised when an operation is attempted on a thread that already has an operation in flight."""

    def __init__(self, thread_id: str) -> None:
        super().__init__(f"Thread '{thread_id}' is busy with another operation.")
        self.thread_id = thread_id


class LockedThread:
    """Proof of an active thread lock held on (checkpointer, thread_id)."""

    def __init__(self, checkpointer: Any, thread_id: str) -> None:
        self.checkpointer = checkpointer
        self.thread_id = thread_id
        self._active = True

    def check_active(self) -> None:
        """Verify that the lock on this thread is still active."""
        if not self._active:
            raise RuntimeError(
                f"LockedThread for thread '{self.thread_id}' is no longer active (lock released)"
            )

    def __repr__(self) -> str:
        return f"LockedThread(thread_id={self.thread_id!r}, active={self._active})"


_REGISTRY_LOCK = threading.Lock()
_BUSY_THREADS: WeakKeyDictionary[Any, set[str]] = WeakKeyDictionary()


@contextmanager
def thread_lock(checkpointer: Any, thread_id: str) -> Iterator[LockedThread]:
    """Mark `(checkpointer, thread_id)` busy for the block, or raise SessionBusy.

    Yields a LockedThread proof of lock that is invalidated when the block exits.
    """
    if checkpointer is None:
        locked = LockedThread(None, thread_id)
        try:
            yield locked
        finally:
            locked._active = False
        return

    with _REGISTRY_LOCK:
        busy = _BUSY_THREADS.setdefault(checkpointer, set())
        if thread_id in busy:
            raise SessionBusy(thread_id)
        busy.add(thread_id)
    locked = LockedThread(checkpointer, thread_id)
    try:
        yield locked
    finally:
        locked._active = False
        with _REGISTRY_LOCK:
            busy_set = _BUSY_THREADS.get(checkpointer)
            if busy_set is not None:
                busy_set.discard(thread_id)
                if not busy_set:
                    del _BUSY_THREADS[checkpointer]
