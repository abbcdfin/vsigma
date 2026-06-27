"""The `Run` handle — one bounded execution of a compiled agent (loop).

A run executes a single task within an optional session. It is backed by a
LangGraph thread, so it can pause at a human-interrupt and `resume()`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from langgraph.types import Command

from .observability import TrajectoryEvent
from .state import State

if TYPE_CHECKING:
    from .builder import Agent


class Run:
    def __init__(
        self, agent: Agent, run_id: str, task_id: str, session_id: str | None
    ) -> None:
        self._agent = agent
        self.run_id = run_id
        self.task_id = task_id
        self.session_id = session_id
        self.config: dict[str, object] = {"configurable": {"thread_id": run_id}}
        self._last: State = {}

    def _record(self, output: State) -> None:
        self._last = output

    @property
    def state(self) -> State:
        snapshot = self._agent.graph.get_state(cast("Any", self.config))
        return dict(snapshot.values)

    @property
    def interrupted(self) -> bool:
        return "__interrupt__" in self._last

    @property
    def status(self) -> str:
        if self.interrupted:
            return "escalated"
        return str(self.state.get("status", "running"))

    @property
    def result(self) -> str | None:
        value = self.state.get("result")
        return None if value is None else str(value)

    @property
    def trajectory(self) -> list[TrajectoryEvent]:
        events = cast("list[TrajectoryEvent]", getattr(self._agent.observer, "events", []))
        return [event for event in events if event.run_id == self.run_id]

    def resume(self, feedback: object) -> Run:
        output = self._agent.graph.invoke(Command(resume=feedback), cast("Any", self.config))
        self._record(cast("State", output))
        return self
