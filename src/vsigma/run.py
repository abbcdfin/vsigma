"""The runtime handles: `Agent` (a compiled loop) and `Run` (one execution).

`Agent` wraps a *compiled LangGraph `StateGraph`* plus the injected `Services` —
it is built directly from a graph (by a template or by hand), not via a builder.
`agent.run(task)` executes one task and returns a `Run`, which can pause at a
human-interrupt and `resume()`.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from .memory import MemoryStore
from .observability import Observer, TrajectoryEvent
from .seam import Services
from .state import State


class Agent:
    """A compiled loop: a reusable definition you execute via `run()`."""

    def __init__(
        self,
        graph: CompiledStateGraph[Any, Any, Any, Any],
        services: Services,
        *,
        system: str | None = None,
        recursion_limit: int = 25,
    ) -> None:
        self.graph = graph
        self.services = services
        self.system = system
        # Safety backstop for flat/unguarded loops; LangGraph raises if exceeded.
        self.recursion_limit = recursion_limit

    @property
    def memory(self) -> MemoryStore:
        return self.services.memory

    @property
    def observer(self) -> Observer:
        return self.services.observer

    def run(
        self,
        task: str,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        system: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> Run:
        rid = run_id or uuid4().hex
        tid = task_id or rid
        messages: list[dict[str, object]] = []
        system_prompt = system if system is not None else self.system
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": task})
        initial: State = {
            "task_id": tid,
            "run_id": rid,
            "session_id": session_id,
            "user_id": user_id,
            "parent_run_id": None,
            "attempt_count": 0,
            "status": "running",
            "messages": messages,
        }
        run = Run(self, rid, tid, session_id)
        run.config["recursion_limit"] = self.recursion_limit
        output = self.graph.invoke(initial, cast("Any", run.config))
        run._record(cast("State", output))
        return run


class Run:
    """One bounded execution of an `Agent`, backed by a LangGraph thread."""

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
