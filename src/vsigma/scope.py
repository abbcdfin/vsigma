"""The scope ladder — the running-context model in code.

Memory namespaces are structured scope paths, ``"level:id/kind"`` (e.g.
``persona:alice/knowledge``, ``session:s1/context``), per the running-context
model proposal: the ladder is global / agent / persona / session / episode /
step; *kind* separates durable knowledge from situational context. The
`MemoryStore` protocol is unchanged — scopes ride on the namespace string.

Visibility is a first-class property of scope: `visible_scopes` never includes
another persona's namespace, and `recall` (the retrieve-path helper) only reads
visible scopes — persona-private memory cannot leak across participants.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from .memory import MemoryStore
from .state import State

ScopeLevel = Literal["global", "agent", "persona", "session", "episode", "step"]
Kind = Literal["knowledge", "context"]

SCOPE_LEVELS: tuple[str, ...] = get_args(ScopeLevel)
KINDS: tuple[str, ...] = get_args(Kind)


@dataclass(frozen=True)
class Scope:
    """One rung of the ladder, optionally bound to an id (global/agent have none)."""

    level: ScopeLevel
    id: str = ""

    def namespace(self, kind: Kind) -> str:
        head = f"{self.level}:{self.id}" if self.id else self.level
        return f"{head}/{kind}"


#: The two id-less scopes, shared by every run.
GLOBAL = Scope("global")
AGENT = Scope("agent")


def persona(user_id: str) -> Scope:
    return Scope("persona", user_id)


def session(session_id: str) -> Scope:
    return Scope("session", session_id)


def episode(run_id: str) -> Scope:
    return Scope("episode", run_id)


def visible_scopes(state: State) -> list[Scope]:
    """The scopes the current turn may read: global, agent, the *serving*
    persona, this session, this episode — never another persona's scope."""
    scopes = [GLOBAL, AGENT]
    user_id = state.get("user_id")
    if user_id:
        scopes.append(persona(str(user_id)))
    session_id = state.get("session_id")
    if session_id:
        scopes.append(session(str(session_id)))
    run_id = state.get("run_id")
    if run_id:
        scopes.append(episode(str(run_id)))
    return scopes


def recall(
    memory: MemoryStore, state: State, kind: Kind | None = None
) -> list[tuple[str, str, object]]:
    """Retrieve-path helper: everything visible to this turn, as
    ``(namespace, key, value)`` triples. Enforces the visibility rule."""
    kinds: list[Kind] = [kind] if kind else ["knowledge", "context"]
    out: list[tuple[str, str, object]] = []
    for scope in visible_scopes(state):
        for k in kinds:
            ns = scope.namespace(k)
            out.extend((ns, key, value) for key, value in memory.query(ns))
    return out
