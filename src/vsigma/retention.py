"""Thread retention: bounding what the checkpointer keeps."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, cast
from weakref import WeakKeyDictionary

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from .state import NodeFn, State


class SessionBusy(RuntimeError):
    """Raised when an operation is attempted on a thread that already has an operation in flight."""

    def __init__(self, thread_id: str) -> None:
        super().__init__(f"Thread '{thread_id}' is busy with another operation.")
        self.thread_id = thread_id


_REGISTRY_LOCK = threading.Lock()
_BUSY_THREADS: WeakKeyDictionary[Any, set[str]] = WeakKeyDictionary()


@contextmanager
def thread_lock(checkpointer: Any, thread_id: str) -> Iterator[None]:
    """Mark `(checkpointer, thread_id)` busy for the block, or raise SessionBusy."""
    if checkpointer is None:
        yield
        return
    with _REGISTRY_LOCK:
        busy = _BUSY_THREADS.setdefault(checkpointer, set())
        if thread_id in busy:
            raise SessionBusy(thread_id)
        busy.add(thread_id)
    try:
        yield
    finally:
        with _REGISTRY_LOCK:
            busy_set = _BUSY_THREADS.get(checkpointer)
            if busy_set is not None:
                busy_set.discard(thread_id)
                if not busy_set:
                    del _BUSY_THREADS[checkpointer]

# Both are optional: `import vsigma` must work without the SQLite saver and on a
# LangGraph that predates DeltaChannel (where no graph can use one).
try:
    from langgraph.checkpoint.sqlite import SqliteSaver as _SqliteSaver
except ImportError:
    _SqliteSaver = None  # type: ignore[misc,assignment]

try:
    from langgraph.channels import DeltaChannel as _DeltaChannel
except ImportError:
    _DeltaChannel = None  # type: ignore[misc,assignment]


def _pruner(graph: Any) -> Callable[[str], None]:
    """Validate checkpointer support and return the pruning callable for `graph`.

    Raises:
        ValueError: If graph has no checkpointer, or uses a DeltaChannel.
        NotImplementedError: If checkpointer is not supported.
    """
    cp = getattr(graph, "checkpointer", None)
    if cp is None:
        raise ValueError("keep_latest requires a graph compiled with a checkpointer")

    channels = getattr(graph, "channels", {})
    if isinstance(channels, dict):
        for name, ch in channels.items():
            if _DeltaChannel is not None and isinstance(ch, _DeltaChannel):
                raise ValueError(f"keep_latest does not support DeltaChannel: {name}")

    def _is_paused(thread_id: str) -> bool:
        state = graph.get_state({"configurable": {"thread_id": thread_id}})
        return bool(state.interrupts)

    if isinstance(cp, InMemorySaver):
        return lambda thread_id: _keep_latest_in_memory(cp, thread_id, _is_paused(thread_id))

    if _SqliteSaver is not None and isinstance(cp, _SqliteSaver):
        return lambda thread_id: _keep_latest_sqlite(cp, thread_id, _is_paused(thread_id))

    if type(cp).prune is not BaseCheckpointSaver.prune:
        return lambda thread_id: cp.prune([thread_id], strategy="keep_latest")

    raise NotImplementedError(
        f"keep_latest is not supported for checkpointer {type(cp).__name__}"
    )


def validate_checkpointer_support(graph: Any) -> None:
    """Validate that the compiled graph uses a supported checkpointer and no DeltaChannel.

    Raises:
        ValueError: If graph has no checkpointer, or uses a DeltaChannel.
        NotImplementedError: If checkpointer is not supported.
    """
    _pruner(graph)


def keep_latest(graph: Any, thread_id: str) -> None:
    """Prune older checkpoints and pending writes for the given thread.

    - When the thread is not paused: keep only the root namespace's ('') latest
      checkpoint and its pending writes; delete all subgraph namespaces.
    - When the thread is paused: keep the latest checkpoint and its pending
      writes in every namespace so that paused subgraphs survive.

    For InMemorySaver, blobs not referenced by kept checkpoints are also deleted.
    For checkpointers overriding `prune` (e.g. Redis), delegates to `prune`.
    """
    prune = _pruner(graph)
    with thread_lock(graph.checkpointer, thread_id):
        prune(thread_id)


def _keep_latest_in_memory(cp: InMemorySaver, thread_id: str, is_paused: bool) -> None:
    if thread_id not in cp.storage:
        return

    thread_storage = cp.storage[thread_id]
    kept_checkpoints: set[tuple[str, str]] = set()

    if not is_paused:
        root_cps = thread_storage.get("", {})
        if root_cps:
            latest_id = max(root_cps.keys())
            kept_checkpoints.add(("", latest_id))
            thread_storage[""] = {latest_id: root_cps[latest_id]}
        else:
            thread_storage.pop("", None)
        for ns in list(thread_storage.keys()):
            if ns != "":
                del thread_storage[ns]
    else:
        for ns in list(thread_storage.keys()):
            ns_cps = thread_storage[ns]
            if ns_cps:
                latest_id = max(ns_cps.keys())
                kept_checkpoints.add((ns, latest_id))
                thread_storage[ns] = {latest_id: ns_cps[latest_id]}
            else:
                del thread_storage[ns]

    if not thread_storage:
        del cp.storage[thread_id]

    # Delete writes for checkpoints that are not kept
    for key in list(cp.writes.keys()):
        if key[0] == thread_id:
            ns, cid = key[1], key[2]
            if (ns, cid) not in kept_checkpoints:
                del cp.writes[key]

    # Collect referenced blobs from kept checkpoints
    referenced_blobs: set[tuple[str, str, Any]] = set()
    for ns, cid in kept_checkpoints:
        cp_tuple = thread_storage.get(ns, {}).get(cid)
        if cp_tuple is not None:
            checkpoint_raw = cp_tuple[0]
            checkpoint = cast("dict[str, Any]", cp.serde.loads_typed(checkpoint_raw))
            channel_versions = checkpoint.get("channel_versions", {})
            for ch, ver in channel_versions.items():
                referenced_blobs.add((ns, ch, ver))

    # Delete unreferenced blobs
    for b_key in list(cp.blobs.keys()):
        if b_key[0] == thread_id:
            b_ns, b_ch, b_ver = b_key[1], b_key[2], b_key[3]
            if (b_ns, b_ch, b_ver) not in referenced_blobs:
                del cp.blobs[b_key]


def _keep_latest_sqlite(cp: Any, thread_id: str, is_paused: bool) -> None:
    with cp.lock:
        if hasattr(cp, "setup") and hasattr(cp, "is_setup") and not cp.is_setup:
            cp.setup()
        if cp.conn.in_transaction:
            cp.conn.commit()
        cursor = cp.conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            if not is_paused:
                cursor.execute(
                    "SELECT MAX(checkpoint_id) FROM checkpoints "
                    "WHERE thread_id = ? AND checkpoint_ns = ''",
                    (thread_id,),
                )
                row = cursor.fetchone()
                max_id = row[0] if row else None
                if max_id is None:
                    cursor.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
                    cursor.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
                else:
                    cursor.execute(
                        "DELETE FROM checkpoints "
                        "WHERE thread_id = ? AND (checkpoint_ns != '' OR checkpoint_id != ?)",
                        (thread_id, max_id),
                    )
                    cursor.execute(
                        "DELETE FROM writes "
                        "WHERE thread_id = ? AND (checkpoint_ns != '' OR checkpoint_id != ?)",
                        (thread_id, max_id),
                    )
            else:
                cursor.execute(
                    """
                    DELETE FROM checkpoints
                    WHERE thread_id = ?
                      AND (checkpoint_ns, checkpoint_id) NOT IN (
                          SELECT checkpoint_ns, MAX(checkpoint_id)
                          FROM checkpoints
                          WHERE thread_id = ?
                          GROUP BY checkpoint_ns
                      )
                    """,
                    (thread_id, thread_id),
                )
                cursor.execute(
                    """
                    DELETE FROM writes
                    WHERE thread_id = ?
                      AND (checkpoint_ns, checkpoint_id) NOT IN (
                          SELECT checkpoint_ns, MAX(checkpoint_id)
                          FROM checkpoints
                          WHERE thread_id = ?
                          GROUP BY checkpoint_ns
                      )
                    """,
                    (thread_id, thread_id),
                )
            cp.conn.commit()
        except Exception:
            cp.conn.rollback()
            raise


def retain_window(max_messages: int) -> NodeFn:
    """Graph node that removes messages outside the sliding window from thread state.

    Emits `RemoveMessage(id=...)` for each message that falls outside the window.
    """
    if max_messages < 1:
        raise ValueError(f"max_messages must be >= 1, got {max_messages}")

    from langchain_core.messages import AnyMessage, RemoveMessage

    from .context import window

    def _retain(state: State) -> dict[str, object]:
        messages = cast("Sequence[AnyMessage]", state.get("messages", []))
        drop = len(messages) - len(window(messages, max_messages))
        if drop <= 0:
            return {}
        return {"messages": [RemoveMessage(id=cast("str", m.id)) for m in messages[:drop]]}

    return _retain


def validate_retain_messages(context: Any, retain_messages: int) -> None:
    """Validate that retain_messages is not smaller than any SlidingWindowContext view window."""
    from .context import SlidingWindowContext

    curr = context
    while curr is not None:
        if isinstance(curr, SlidingWindowContext):
            if curr.max_messages > retain_messages:
                raise ValueError(
                    f"retain_messages ({retain_messages}) cannot be smaller than "
                    f"SlidingWindowContext.max_messages ({curr.max_messages})"
                )
        curr = getattr(curr, "inner", None)

