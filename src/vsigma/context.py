"""Context management — the pre-call assembly step in the wrapper seam.

`PassthroughContext` is the no-op default; `SlidingWindowContext` is the first
real assembly policy (compaction). Both slot into the same seam hook — context
management stays an extension point, never a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast

from .memory import MemoryStore
from .state import State


class ContextManager(Protocol):
    def assemble(self, state: State, memory: MemoryStore) -> State: ...


class PassthroughContext:
    """No-op assembly: returns the state unchanged."""

    def assemble(self, state: State, memory: MemoryStore) -> State:
        return state


@dataclass
class SlidingWindowContext:
    """Compaction: keep the system prompt (if first) + the most recent turns.

    The simplest real context policy — bounds the model window on long
    episodes. `max_messages` counts the kept tail, excluding the pinned
    system message.
    """

    max_messages: int = 20

    def assemble(self, state: State, memory: MemoryStore) -> State:
        messages = cast("list[dict[str, object]]", state.get("messages", []))
        if len(messages) <= self.max_messages:
            return state
        pinned: list[dict[str, object]] = []
        rest = messages
        if messages and messages[0].get("role") == "system":
            pinned, rest = [messages[0]], messages[1:]
        tail = rest[-self.max_messages :]
        # Never let the window start with orphaned tool results (they need
        # their assistant tool-call turn); drop leading tool messages.
        while tail and tail[0].get("role") == "tool":
            tail = tail[1:]
        window = dict(state)
        window["messages"] = pinned + tail
        return window
