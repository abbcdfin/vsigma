"""Context management — the pre-call assembly step in the wrapper seam.

`PassthroughContext` is the no-op default; `SlidingWindowContext` compacts;
`RecallContext` injects recalled knowledge into the window (v0: confidence-
ranked and bounded — semantic scoring and token budgets are the Phase 4.5
upgrade, behind the same protocol). All slot into the same seam hook — context
management stays an extension point, never a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast

from .memory import MemoryStore
from .scope import recall
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


#: Sentinel prefix marking the message RecallContext injects. Needed because
#: the seam persists node output back into state: without it, each step would
#: re-inject on top of the last step's injection. Tagging lets every assemble
#: *replace* the recalled block (also keeping it fresh as memory grows).
RECALL_MARKER = "[vsigma:recall]"


@dataclass
class RecallContext:
    """Inject recalled knowledge into the window (v0 of the read-side loop).

    Gather: `recall()` over the scopes visible to this turn (the §5 visibility
    rule — another persona's private memory can never enter the prompt).
    Filter/rank: knowledge-kind records with `confidence >= min_confidence`,
    highest confidence first. Bound: at most `max_facts`. Format: one tagged
    system message after the pinned system prompt.

    Opt-in by design (`Services(context=RecallContext())`); the default stays
    `PassthroughContext`. Phase 4.5 upgrades the ranking (semantic relevance,
    token budget) behind this same protocol. Compose with other policies via
    `inner` (applied first), e.g. ``RecallContext(inner=SlidingWindowContext())``.
    """

    max_facts: int = 10
    min_confidence: float = 0.0
    inner: ContextManager | None = None

    def assemble(self, state: State, memory: MemoryStore) -> State:
        window = dict(self.inner.assemble(state, memory)) if self.inner else dict(state)
        facts = self._gather(window, memory)
        messages = [
            m
            for m in cast("list[dict[str, object]]", window.get("messages", []))
            if not str(m.get("content", "")).startswith(RECALL_MARKER)
        ]
        if facts:
            recalled: dict[str, object] = {
                "role": "system",
                "content": RECALL_MARKER + " Relevant knowledge:\n"
                + "\n".join(f"- {fact}" for fact in facts),
            }
            at = 1 if messages and messages[0].get("role") == "system" else 0
            messages = [*messages[:at], recalled, *messages[at:]]
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
