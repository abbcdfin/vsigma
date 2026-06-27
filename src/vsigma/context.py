"""Context management — the pre-call assembly step in the wrapper seam.

Phase 1 ships a pass-through default: the hook exists so real assembly and
compaction can slot in later as an extension, not a rewrite.
"""

from __future__ import annotations

from typing import Protocol

from .memory import MemoryStore
from .state import State


class ContextManager(Protocol):
    def assemble(self, state: State, memory: MemoryStore) -> State: ...


class PassthroughContext:
    """No-op assembly: returns the state unchanged."""

    def assemble(self, state: State, memory: MemoryStore) -> State:
        return state
