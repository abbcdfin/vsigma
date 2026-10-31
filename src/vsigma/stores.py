"""Persistent first-party stores behind the `MemoryStore` and `MaintainableStore` protocols.

`PersistentStore` adapts LangGraph's `SqliteStore` for session and long-term memory.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from langgraph.store.base import Op, PutOp
from langgraph.store.sqlite import SqliteStore


class _SqliteStore(SqliteStore):
    """Subclass of LangGraph's SqliteStore that delegates transactions to PersistentStore."""

    @contextmanager
    def _cursor(self, *, transaction: bool = True) -> Iterator[sqlite3.Cursor]:
        with super()._cursor(transaction=False) as cur:
            yield cur


def _encode(namespace: str) -> tuple[str, str]:
    return ("m", namespace.replace("%", "%25").replace(".", "%2E"))


def _dump_json(value: object) -> tuple[str, int]:
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(value, default=str, ensure_ascii=True)
        encoded = text.encode("utf-8")
    return text, len(encoded)


class PersistentStore:
    """`MemoryStore` and `MaintainableStore` adapter over LangGraph's `SqliteStore`."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        p = Path(self.path)
        if p.is_file() and p.stat().st_size > 0:
            # Check for legacy pre-v0.3.0 stores on a read-only connection
            # so SQLite does not checkpoint uncheckpointed WAL frames.
            uri = f"{p.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as ro_conn:
                table_exists = ro_conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory'"
                ).fetchone()
                if table_exists:
                    has_rows = ro_conn.execute(
                        "SELECT 1 FROM memory LIMIT 1"
                    ).fetchone()
                    if has_rows:
                        raise RuntimeError(
                            f"Store file '{self.path}' contains pre-v0.3.0 data in table 'memory'. "
                            "vsigma does not migrate existing stores; please specify a new path. "
                            "The file was left untouched."
                        )

        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._store = _SqliteStore(self._conn)
        self._store.setup()
        self._last = 0

    @contextmanager
    def _transaction(self, begin: str = "BEGIN IMMEDIATE") -> Iterator[None]:
        with self._lock:
            self._conn.execute(begin)
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def _sequence(self) -> int:
        self._last = max(self._last + 1, time.time_ns())
        return self._last

    def _search(self, labels: tuple[str, ...]) -> list[Any]:
        items: list[Any] = []
        offset = 0
        page_size = 1000
        while True:
            page = self._store.search(labels, limit=page_size, offset=offset)
            items.extend(item for item in page if tuple(item.namespace) == labels)
            if len(page) < page_size:
                break
            offset += page_size
        return items

    def write(self, namespace: str, key: str, value: object) -> None:
        text, byte_count = _dump_json(value)
        labels = _encode(namespace)
        with self._transaction("BEGIN IMMEDIATE"):
            old_item = self._store.get(labels, key)
            meta_item = self._store.get(("n",), namespace)

            if old_item is not None:
                sequence = old_item.value["s"]
                old_bytes = len(old_item.value["j"].encode("utf-8"))
                byte_delta = byte_count - old_bytes
                row_delta = 0
            else:
                sequence = self._sequence()
                byte_delta = byte_count
                row_delta = 1

            if meta_item is not None:
                meta = meta_item.value
                new_first = meta["first"]
                new_rows = meta["rows"] + row_delta
                new_bytes = meta["bytes"] + byte_delta
            else:
                new_first = sequence
                new_rows = row_delta
                new_bytes = byte_delta

            ops: list[Op] = [
                PutOp(labels, key, {"s": sequence, "j": text}, index=False),
                PutOp(
                    ("n",),
                    namespace,
                    {"first": new_first, "rows": new_rows, "bytes": new_bytes},
                    index=False,
                ),
            ]
            self._store.batch(ops)

    def read(self, namespace: str, key: str) -> object | None:
        labels = _encode(namespace)
        with self._transaction("BEGIN"):
            item = self._store.get(labels, key)
        if item is None:
            return None
        val: object = json.loads(item.value["j"])
        return val

    def query(self, namespace: str) -> list[tuple[str, object]]:
        labels = _encode(namespace)
        with self._transaction("BEGIN"):
            items = self._search(labels)
        items.sort(key=lambda item: item.value["s"])
        return [(item.key, json.loads(item.value["j"])) for item in items]

    def delete(self, namespace: str, keys: list[str]) -> None:
        unique_keys = set(keys)
        if not unique_keys:
            return
        labels = _encode(namespace)
        with self._transaction("BEGIN IMMEDIATE"):
            meta_item = self._store.get(("n",), namespace)
            if meta_item is None:
                return
            existing_items = [self._store.get(labels, k) for k in unique_keys]
            valid_items = [item for item in existing_items if item is not None]
            if not valid_items:
                return

            deleted_bytes = sum(len(item.value["j"].encode("utf-8")) for item in valid_items)
            deleted_rows = len(valid_items)

            meta = meta_item.value
            remaining_rows = meta["rows"] - deleted_rows
            remaining_bytes = meta["bytes"] - deleted_bytes

            ops: list[Op] = [PutOp(labels, item.key, None) for item in valid_items]
            if remaining_rows == 0:
                ops.append(PutOp(("n",), namespace, None))
            else:
                ops.append(
                    PutOp(
                        ("n",),
                        namespace,
                        {
                            "first": meta["first"],
                            "rows": remaining_rows,
                            "bytes": remaining_bytes,
                        },
                        index=False,
                    )
                )
            self._store.batch(ops)

    def namespaces(self) -> list[str]:
        """All namespaces in first-write order."""
        with self._transaction("BEGIN"):
            meta_records = self._search(("n",))
        meta_records.sort(key=lambda item: item.value["first"])
        return [item.key for item in meta_records]

    def namespace_sizes(self) -> dict[str, int]:
        """Stored value bytes per namespace (counting UTF-8 JSON text)."""
        with self._transaction("BEGIN"):
            meta_records = self._search(("n",))
        return {item.key: item.value["bytes"] for item in meta_records}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
