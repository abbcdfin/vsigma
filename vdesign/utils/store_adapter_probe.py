"""Throwaway prototype for Phase 4.13: LangGraph's SqliteStore behind `MemoryStore`.

Not production code, and not the design: see docs/proposals/store-adapter.md. It lists namespaces
by scanning rows, which §B.3's namespace record replaces, and has no sizes or old-file check.

Loaded as a pytest plugin, it swaps `SQLiteStore` for this adapter across the suite:

    PYTHONPATH=vdesign/utils uv run pytest -p store_adapter_probe -p no:cacheprovider
"""

import json
import sqlite3
import threading
import time
from pathlib import Path

from langgraph.store.sqlite import SqliteStore

import vsigma
import vsigma.cli
import vsigma.maintenance
import vsigma.stores

PAGE = 1000


def _encode(namespace: str) -> tuple[str, ...]:
    return ("m", namespace.replace("%", "%25").replace(".", "%2E"))


def _decode(labels: tuple[str, ...]) -> str:
    return labels[1].replace("%2E", ".").replace("%25", "%")


class AdapterStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._store = SqliteStore(self._conn)
        self._store.setup()
        self._last = 0

    def _sequence(self) -> int:
        self._last = max(self._last + 1, time.time_ns())
        return self._last

    def write(self, namespace: str, key: str, value: object) -> None:
        payload = json.loads(json.dumps(value, default=str))
        with self._lock:
            old = self._store.get(_encode(namespace), key)
            sequence = old.value["s"] if old else self._sequence()
            self._store.put(_encode(namespace), key, {"s": sequence, "v": payload}, index=False)

    def read(self, namespace: str, key: str) -> object | None:
        with self._lock:
            item = self._store.get(_encode(namespace), key)
        return None if item is None else item.value["v"]

    def _items(self, namespace: str) -> list:
        out: list = []
        offset = 0
        labels = _encode(namespace)
        while True:
            page = self._store.search(labels, limit=PAGE, offset=offset)
            out.extend(item for item in page if tuple(item.namespace) == labels)
            if len(page) < PAGE:
                return out
            offset += PAGE

    def query(self, namespace: str) -> list[tuple[str, object]]:
        with self._lock:
            items = self._items(namespace)
        items.sort(key=lambda item: item.value["s"])
        return [(item.key, item.value["v"]) for item in items]

    def namespaces(self) -> list[str]:
        with self._lock:
            names: list[str] = []
            offset = 0
            while True:
                page = self._store.list_namespaces(prefix=("m",), limit=PAGE, offset=offset)
                names.extend(_decode(labels) for labels in page)
                if len(page) < PAGE:
                    break
                offset += PAGE
            first = {name: min(i.value["s"] for i in self._items(name)) for name in names}
        return sorted(names, key=first.__getitem__)

    def delete(self, namespace: str, key: str) -> None:
        with self._lock:
            self._store.delete(_encode(namespace), key)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


for module in (vsigma, vsigma.stores, vsigma.cli, vsigma.maintenance):
    module.SQLiteStore = AdapterStore  # type: ignore[attr-defined, misc, assignment]
