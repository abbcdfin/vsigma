"""Memory store — a symmetric (read + write), shape-agnostic interface.

This is the Phase-1 seam for the memory adapter layer (see
docs/proposals/memory-construction.md): construction writes through the same
interface the runtime reads. `InMemoryStore` is the trivial default backend.
"""

from __future__ import annotations

from typing import Protocol


class MemoryStore(Protocol):
    def write(self, namespace: str, key: str, value: object) -> None: ...
    def read(self, namespace: str, key: str) -> object | None: ...

    def query(self, namespace: str) -> list[tuple[str, object]]:
        """Every entry in the namespace, **in first-write order**.

        The ordering is part of the contract, not an implementation accident:
        trajectory rows carry no ordinal in their keys (see `seam.wrap`), so
        this is what puts an episode's steps in sequence — and
        `episode_trajectory`'s trailing window has always relied on it.
        """
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
