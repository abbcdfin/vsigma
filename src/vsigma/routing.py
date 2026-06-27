"""Routing primitives shared by conditional-edge blocks.

A `Router` pairs a routing function (state -> branch key) with the mapping from
branch keys to target node names. Both resilience loop-breakers and effort
estimation produce `Router`s.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .state import State


@dataclass(frozen=True)
class Router:
    """A conditional edge: a routing function plus its branch mapping."""

    fn: Callable[[State], str]
    mapping: dict[str, str]
