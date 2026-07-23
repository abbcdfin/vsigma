"""Persistent first-party stores behind the `MemoryStore` protocol.

`SQLiteStore` is the batteries-included session/long-term backend: one SQLite
file per agent, stdlib-only (no new dependency), transactional, and adequate
for kv facts, episodic records, and provenance-carrying record dicts. Swapping
to a production backend later is the backend-adapter axis — config, not a
rewrite, because everything goes through the same protocol.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path


class SQLiteStore:
    """`MemoryStore` on a single SQLite file. Values are stored as JSON."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS memory ("
            " namespace TEXT NOT NULL,"
            " key TEXT NOT NULL,"
            " value TEXT NOT NULL,"
            " PRIMARY KEY (namespace, key))"
        )
        self._conn.commit()

    def write(self, namespace: str, key: str, value: object) -> None:
        payload = json.dumps(value, default=str)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO memory (namespace, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT (namespace, key) DO UPDATE SET value = excluded.value",
                (namespace, key, payload),
            )

    def read(self, namespace: str, key: str) -> object | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM memory WHERE namespace = ? AND key = ?", (namespace, key)
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def query(self, namespace: str) -> list[tuple[str, object]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM memory WHERE namespace = ? ORDER BY rowid",
                (namespace,),
            ).fetchall()
        return [(key, json.loads(value)) for key, value in rows]

    def namespaces(self) -> list[str]:
        """All namespaces in first-write order — a backend extra beyond the
        `MemoryStore` protocol (the maintenance pass and `memory export` need
        enumeration; the runtime protocol deliberately stays minimal)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT namespace FROM memory GROUP BY namespace ORDER BY MIN(rowid)"
            ).fetchall()
        return [row[0] for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
