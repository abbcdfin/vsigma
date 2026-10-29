"""Context management — strategies for assembling what the LLM sees.

The context manager slot in `Interceptor` is the **only** place where context policy
lives: it decides what portion of the state (and the store) the model sees before
a node runs. Nodes receive the assembled window and return only their newly
produced messages.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from langchain_core.messages import AnyMessage, SystemMessage

from .memory import MemoryStore
from .scope import recall
from .state import State


class ContextManager(Protocol):
    """Protocol for context-assembly strategies.

    Strategies decide what the model sees before a node runs. They are pure
    functions of state and the memory store: they may read memory, but they
    never mutate it.
    """

    def assemble(self, state: State, memory: MemoryStore | None) -> State:
        """Return the window the node should see."""
        ...


class PassthroughContext:
    """No-op assembly: returns the state unchanged."""

    def assemble(self, state: State, memory: MemoryStore | None) -> State:
        return state


def window(messages: Sequence[AnyMessage], max_messages: int) -> list[AnyMessage]:
    """Sliding window over messages: keep the tail, dropping leading tool messages."""
    if len(messages) <= max_messages:
        return list(messages)
    tail = list(messages[-max_messages:])
    while tail and tail[0].type == "tool":
        tail = tail[1:]
    return tail


@dataclass
class SlidingWindowContext:
    """Compaction: keep the most recent turns.

    The simplest real context policy — bounds the model window on long
    episodes. `max_messages` counts the kept tail.
    """

    max_messages: int = 20

    def assemble(self, state: State, memory: MemoryStore | None) -> State:
        messages = cast("list[AnyMessage]", state.get("messages", []))
        if len(messages) <= self.max_messages:
            return state
        tail = window(messages, self.max_messages)
        res = dict(state)
        res["messages"] = tail
        return res


#: Sentinel prefix marking the message RecallContext injects. Tagging lets every
#: assemble *replace* the recalled block (keeping it fresh as memory grows, and
#: preventing stacked injections across repeated assembly).
RECALL_MARKER = "[vsigma:recall]"


@dataclass
class RecallContext:
    """Inject recalled knowledge into the window (v0 of the read-side loop).

    Gather: `recall()` over the scopes visible to this turn (the §5 visibility
    rule — another persona's private memory can never enter the prompt).
    Filter/rank: knowledge-kind records with `confidence >= min_confidence`,
    highest confidence first. Bound: at most `max_facts`. Format: one tagged
    system message injected at the front of the window.

    Opt-in by design (`Interceptor(context=RecallContext())`); the default stays
    `PassthroughContext`. Phase 4.5 upgrades the ranking (semantic relevance,
    token budget) behind this same protocol. Compose with other policies via
    `inner` (applied first), e.g. ``RecallContext(inner=SlidingWindowContext())``.
    """

    max_facts: int = 10
    min_confidence: float = 0.0
    inner: ContextManager | None = None

    def assemble(self, state: State, memory: MemoryStore | None) -> State:
        if memory is None:
            raise ValueError(
                "RecallContext requires a MemoryStore to retrieve knowledge, but memory is None."
            )
        window = dict(self.inner.assemble(state, memory)) if self.inner else dict(state)
        facts = self._gather(window, memory)
        messages = [
            m
            for m in cast("list[AnyMessage]", window.get("messages", []))
            if not m.text.startswith(RECALL_MARKER)
        ]
        if facts:
            recalled = SystemMessage(
                content=RECALL_MARKER
                + " Relevant knowledge:\n"
                + "\n".join(f"- {fact}" for fact in facts),
            )
            messages = [recalled, *messages]
        window["messages"] = messages
        return window

    def _gather(self, state: State, memory: MemoryStore) -> list[str]:
        # (confidence, arrival order) — deterministic; semantic scoring is 4.5.
        ranked: list[tuple[float, int, str]] = []
        for order, (_, _, value) in enumerate(recall(memory, state, kind="knowledge")):
            if isinstance(value, dict) and "content" in value:
                prov = value.get("provenance")
                confidence = (
                    float(cast("float", prov.get("confidence", 1.0)))
                    if isinstance(prov, dict)
                    else 1.0
                )
                content = str(value["content"])
            elif isinstance(value, str):
                confidence, content = 1.0, value
            else:
                continue  # not a fact-shaped entry
            if confidence >= self.min_confidence:
                ranked.append((-confidence, order, content))
        return [content for *_, content in sorted(ranked)[: self.max_facts]]
