"""Observability — append-only trajectory capture.

The wrapper seam emits one `TrajectoryEvent` per node execution, keyed by
run/task/session, kept separate from the lean graph state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TrajectoryEvent:
    run_id: str
    task_id: str
    session_id: str | None
    node: str
    attempt: int
    summary: str


class Observer(Protocol):
    def emit(self, event: TrajectoryEvent) -> None: ...


class TrajectoryLog:
    """Append-only in-memory trajectory sink."""

    def __init__(self) -> None:
        self.events: list[TrajectoryEvent] = []

    def emit(self, event: TrajectoryEvent) -> None:
        self.events.append(event)
