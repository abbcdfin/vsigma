"""Routing primitives shared by conditional-edge blocks.

A `Router` pairs a routing function (state -> branch key) with the mapping from
branch keys to target node names. Both resilience loop-breakers and effort
estimation produce `Router`s.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from langgraph.graph import END

from .state import State


@dataclass(frozen=True)
class Router:
    """A conditional edge: a routing function plus its branch mapping."""

    fn: Callable[[State], str]
    mapping: dict[str, str]


def loop_until_done(*, retry: str, done: str = END) -> Router:
    """The minimal loop terminator: finish when `status == "done"`, else `retry`.

    No attempt cap or escalation — the flat-loop counterpart to
    `break_on_attempts`. Runaway loops are bounded by LangGraph's `recursion_limit`.
    """

    def route(state: State) -> str:
        return "done" if state.get("status") == "done" else "retry"

    return Router(fn=route, mapping={"done": done, "retry": retry})
