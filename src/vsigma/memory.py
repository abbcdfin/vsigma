"""Memory store — a symmetric (read + write), shape-agnostic interface.

This is the Phase-1 adapter interface for the memory layer (see
docs/proposals/memory-construction.md): construction writes through the same
interface the runtime reads. `InMemoryStore` is the trivial default backend.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .stores import _dump_json


@runtime_checkable
class MemoryStore(Protocol):
    def write(self, namespace: str, key: str, value: object) -> None: ...
    def read(self, namespace: str, key: str) -> object | None: ...

    def query(self, namespace: str) -> list[tuple[str, object]]:
        """Every entry in the namespace, **in first-write order**.

        The ordering is part of the contract, not an implementation accident:
        trajectory rows carry no ordinal in their keys (see `interceptor.wrap`), so
        this is what puts an episode's steps in sequence — and
        `episode_trajectory`'s trailing window has always relied on it.
        """
        ...


@runtime_checkable
class MaintainableStore(MemoryStore, Protocol):
    """Protocol for stores managed by maintenance passes and retention policies."""

    def namespaces(self) -> list[str]:
        """All namespaces in first-write order."""
        ...

    def namespace_sizes(self) -> dict[str, int]:
        """Stored value bytes per namespace (counting UTF-8 JSON text)."""
        ...

    def delete(self, namespace: str, keys: list[str]) -> None:
        """Delete keys from a namespace in a single transaction."""
        ...


class InMemoryStore:
    """Trivial in-process backend. Namespaces isolate sessions/tasks."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, object]] = {}

    def write(self, namespace: str, key: str, value: object) -> None:
        self._data.setdefault(namespace, {})[key] = value

    def read(self, namespace: str, key: str) -> object | None:
        return self._data.get(namespace, {}).get(key)

    def query(self, namespace: str) -> list[tuple[str, object]]:
        return list(self._data.get(namespace, {}).items())

    def namespaces(self) -> list[str]:
        return [ns for ns, items in list(self._data.items()) if items]

    def namespace_sizes(self) -> dict[str, int]:
        sizes: dict[str, int] = {}
        # Snapshot dictionary items to safely iterate beside concurrent live turns
        for ns, items in list(self._data.items()):
            if not items:
                continue
            ns_bytes = 0
            for val in list(items.values()):
                _, byte_count = _dump_json(val)
                ns_bytes += byte_count
            sizes[ns] = ns_bytes
        return sizes

    def delete(self, namespace: str, keys: list[str]) -> None:
        ns_dict = self._data.get(namespace)
        if ns_dict is None:
            return
        for key in set(keys):
            ns_dict.pop(key, None)
        if not ns_dict:
            self._data.pop(namespace, None)
