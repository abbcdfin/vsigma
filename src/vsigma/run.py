"""The runtime handles: `Agent` (a compiled loop) and `Run` (one execution).

`Agent` wraps a *compiled LangGraph `StateGraph`* plus the injected `Interceptor` —
it is built directly from a graph (by a template or by hand), not via a builder.
`agent.run(task)` executes one task and returns a `Run`, which can pause at a
human-interrupt and `resume()`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Durability, StateSnapshot

from .episodes import record_episode, record_episode_status
from .interceptor import Interceptor
from .memory import MemoryStore
from .observability import Observer, TrajectoryEvent, TrajectoryLog
from .retention import prune_checkpoints, validate_checkpointer_support
from .scope import episode
from .state import State
from .threads import LockedThread, thread_lock

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .sessions import Session


class SessionPaused(RuntimeError):
    """Raised when a run or send is called on a session thread paused at an interrupt."""

    def __init__(
        self,
        session_id: str,
        interrupt_payload: Any,
        interrupt_id: str | None = None,
    ) -> None:
        super().__init__(
            f"Session '{session_id}' is paused at interrupt {interrupt_payload}. "
            "Call session.resume(value) to continue it, or pass abandon=True "
            "to abandon the paused turn."
        )
        self.session_id = session_id
        self.interrupt = interrupt_payload
        self.interrupt_id = interrupt_id


class PauseChanged(RuntimeError):
    """Raised when resume(interrupt_id=...) does not match the currently pending pause."""

    def __init__(
        self,
        interrupt: Any = None,
        interrupt_id: str | None = None,
    ) -> None:
        if interrupt_id is not None:
            msg = (
                f"Pending pause has changed to interrupt {interrupt_id!r} ({interrupt}); "
                "the answered pause is no longer pending."
            )
        else:
            msg = "No pause is currently pending; the answered pause is no longer pending."
        super().__init__(msg)
        self.interrupt = interrupt
        self.interrupt_id = interrupt_id


def _stream_run(
    graph: CompiledStateGraph[Any, Any, Any, Any],
    input_data: Any,
    config: dict[str, Any],
    observer: Observer | None,
    durability: Durability | None,
) -> None:
    invoke_kwargs: dict[str, Any] = {}
    if durability is not None:
        invoke_kwargs["durability"] = durability

    last_attempt: dict[tuple[str, ...], int] = {}
    metadata = config.get("metadata", {})
    run_id = str(metadata.get("run_id", ""))
    task_id = str(metadata.get("task_id", run_id))
    session_id = metadata.get("session_id")
    sess_id_str = str(session_id) if session_id is not None else None

    stream = graph.stream(
        input_data,
        config=cast("Any", config),
        stream_mode=["updates", "values"],
        subgraphs=True,
        **invoke_kwargs,
    )
    for ns, mode, payload in stream:
        if mode == "values" and isinstance(payload, dict):
            if "attempt_count" in payload:
                last_attempt[ns] = int(str(payload.get("attempt_count", 0)))
        elif mode == "updates" and isinstance(payload, dict):
            attempt = last_attempt.get(ns, last_attempt.get((), 0))
            for node_name, update in payload.items():
                if node_name == "__interrupt__":
                    continue
                clean_ns = [part.split(":")[0] for part in ns]
                if clean_ns:
                    label = f"{'/'.join(clean_ns)}/{node_name}"
                else:
                    label = node_name

                if observer is not None:
                    summary = (
                        f"{label}: status={update.get('status')}"
                        if isinstance(update, dict)
                        else f"{label}: status=None"
                    )
                    observer.emit(
                        TrajectoryEvent(
                            run_id=run_id,
                            task_id=task_id,
                            session_id=sess_id_str,
                            node=label,
                            attempt=attempt,
                            summary=summary,
                        )
                    )


class Agent:
    """A compiled loop: a reusable definition you execute via `run()`."""

    def __init__(
        self,
        graph: CompiledStateGraph[Any, Any, Any, Any],
        interceptor: Interceptor,
        *,
        system: str | None = None,
        recursion_limit: int = 25,
        durability: Durability | None = "exit",
        checkpoints: Literal["all", "latest"] = "all",
    ) -> None:
        if checkpoints not in ("all", "latest"):
            raise ValueError(f"checkpoints must be 'all' or 'latest', got {checkpoints!r}")
        if checkpoints == "latest":
            validate_checkpointer_support(graph)
        self.graph = graph
        self.interceptor = interceptor
        self.system = system
        # Safety backstop for flat/unguarded loops; LangGraph raises if exceeded.
        self.recursion_limit = recursion_limit
        self.durability = durability
        self.checkpoints = checkpoints

    @property
    def memory(self) -> MemoryStore | None:
        return self.interceptor.memory

    @property
    def observer(self) -> Observer | None:
        return self.interceptor.observer

    def run(
        self,
        task: str,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        system: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        channel: str | None = None,
        abandon: bool = False,
    ) -> Run:
        """Execute one task on a session thread.

        In vsigma 4.8, `thread_id = session_id`. A one-off `agent.run(task)`
        runs on its own thread. `run_id` identifies this episode on the
        thread and is tagged in checkpoint metadata, `episodes/index`, and
        episodic context.
        """
        rid = run_id or uuid4().hex
        tid = task_id or rid
        thread_id = session_id or rid
        with thread_lock(self.graph.checkpointer, thread_id) as locked:
            old_paused_run_id: str | None = None
            if session_id is not None:
                cfg = {"configurable": {"thread_id": session_id}}
                snapshot = self.graph.get_state(cast("Any", cfg))
                if snapshot.interrupts:
                    if not abandon:
                        intr = snapshot.interrupts[0]
                        raise SessionPaused(session_id, intr.value, intr.id)
                    metadata = snapshot.metadata or {}
                    if "run_id" in metadata:
                        old_paused_run_id = str(metadata["run_id"])

            system_prompt = system if system is not None else self.system

            initial: State = {
                "task_id": tid,
                "run_id": rid,
                "session_id": session_id,
                "user_id": user_id,
                "system": system_prompt,
                "parent_run_id": None,
                "attempt_count": 0,
                "status": "running",
                "messages": [{"role": "user", "content": task}],
                "result": None,
            }
            # Registers the episode only when memory is configured.
            if self.memory is not None:
                record_episode(
                    self.memory, rid, task_id=tid, session_id=session_id, user_id=user_id
                )
                record_episode_status(self.memory, rid, "running", locked=locked)
                self.memory.write(
                    episode(rid).namespace("context"),
                    f"input:{uuid4().hex}",
                    {"messages": [{"role": "user", "content": task}]},
                )
            run = Run(self, rid, tid, session_id)
            run.config["metadata"] = {
                "run_id": rid,
                "task_id": tid,
                "session_id": session_id,
                "user_id": user_id,
                "channel": channel,
            }
            try:
                _stream_run(self.graph, initial, run.config, self.observer, self.durability)
            except BaseException:
                if self.memory is not None:
                    try:
                        record_episode_status(self.memory, rid, "failed", locked=locked)
                    except Exception:
                        logger.warning(
                            "Failed to record episode %r status as 'failed'",
                            rid,
                            exc_info=True,
                        )
                raise
            run._record()
            if self.memory is not None:
                final_status = "escalated" if run.interrupted else "done"
                record_episode_status(self.memory, rid, final_status, locked=locked)
                if old_paused_run_id is not None:
                    record_episode_status(
                        self.memory, old_paused_run_id, "abandoned", locked=locked
                    )
            if self.checkpoints == "latest":
                prune_checkpoints(self.graph, locked)
            return run

    def session(self, session_id: str | None = None, *, user_id: str | None = None) -> Session:
        """Open (or rehydrate) a multi-round dialog handle — see `Session`."""
        from .sessions import Session

        return Session(self, session_id=session_id, user_id=user_id)


class Run:
    """One bounded execution of an `Agent`, backed by a LangGraph thread."""

    def __init__(
        self, agent: Agent, run_id: str, task_id: str, session_id: str | None = None
    ) -> None:
        self._agent = agent
        self.run_id = run_id
        self.task_id = task_id
        self.session_id = session_id
        thread_id = session_id or run_id
        self.config: dict[str, object] = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": agent.recursion_limit,
            "metadata": {
                "run_id": run_id,
                "task_id": task_id,
                "session_id": session_id,
            },
        }
        self._consolidated = False

    def _record(self) -> None:
        # Run-end hook: fires at every terminal edge — escalation included, so
        # a failed/parked attempt leaves a record too. The hook sees the
        # *derived* status ("escalated" while parked at an interrupt; the raw
        # state still says "running" there). Per-status policy belongs to the
        # hook (e.g. `reflexive_consolidator` distills only on "done").
        hook = self._agent.interceptor.run_end
        if hook is None or self._agent.memory is None:
            return
        if self.interrupted:
            hook({**self.state, "status": "escalated"}, self._agent.memory)
        elif self.status == "done" and not self._consolidated:
            self._consolidated = True
            hook(self.state, self._agent.memory)

    @property
    def snapshot(self) -> StateSnapshot:
        """This run's newest checkpoint snapshot."""
        history = list(
            self._agent.graph.get_state_history(
                cast("Any", self.config), filter={"run_id": self.run_id}, limit=1
            )
        )
        if history:
            return history[0]
        raise RuntimeError(f"No checkpoint found for run '{self.run_id}'")

    @property
    def _is_active(self) -> bool:
        """Whether this run is the thread's newest/active execution."""
        thread_state = self._agent.graph.get_state(cast("Any", self.config))
        metadata = thread_state.metadata or {}
        return metadata.get("run_id") == self.run_id

    @property
    def state(self) -> State:
        return dict(self.snapshot.values)

    @property
    def interrupted(self) -> bool:
        if not self._is_active:
            return False
        return bool(self.snapshot.interrupts)

    @property
    def interrupt(self) -> Any:
        if not self._is_active:
            return None
        return self.snapshot.interrupts[0].value if self.snapshot.interrupts else None

    @property
    def interrupt_id(self) -> str | None:
        if not self._is_active:
            return None
        return self.snapshot.interrupts[0].id if self.snapshot.interrupts else None

    @property
    def status(self) -> str:
        if self.snapshot.interrupts:
            return "escalated"
        return str(self.state.get("status", "running"))

    @property
    def result(self) -> str | None:
        value = self.state.get("result")
        return None if value is None else str(value)

    @property
    def trajectory(self) -> list[TrajectoryEvent]:
        if not isinstance(self._agent.observer, TrajectoryLog):
            raise RuntimeError(
                "Run.trajectory requires a TrajectoryLog observer, "
                "but none was configured on Interceptor"
            )
        return [event for event in self._agent.observer.events if event.run_id == self.run_id]

    def _resume_locked(
        self, feedback: object, locked: LockedThread, *, interrupt_id: str | None = None
    ) -> Run:
        thread_state = self._agent.graph.get_state(cast("Any", self.config))
        metadata = thread_state.metadata or {}
        active = metadata.get("run_id") == self.run_id
        if interrupt_id is not None:
            current_intr = thread_state.interrupts[0] if thread_state.interrupts else None
            current_id = current_intr.id if current_intr is not None else None
            if not active or current_id != interrupt_id:
                current_val = current_intr.value if current_intr is not None else None
                raise PauseChanged(current_val, current_id)
        if not active or not thread_state.interrupts:
            raise RuntimeError(
                f"Run '{self.run_id}' is not currently paused (active={active})"
            )
        tags = {
            "run_id": metadata.get("run_id", self.run_id),
            "task_id": metadata.get("task_id", self.task_id),
            "session_id": metadata.get("session_id", self.session_id),
            "user_id": metadata.get("user_id"),
            "channel": metadata.get("channel"),
        }
        config = {
            **self.config,
            "metadata": {
                **cast("dict[str, Any]", self.config.get("metadata", {})),
                **tags,
            },
        }
        try:
            _stream_run(
                self._agent.graph,
                Command(resume=feedback),
                config,
                self._agent.observer,
                self._agent.durability,
            )
        except BaseException:
            if self._agent.memory is not None:
                try:
                    thread_state = self._agent.graph.get_state(cast("Any", self.config))
                    if not thread_state.interrupts:
                        record_episode_status(
                            self._agent.memory, self.run_id, "failed", locked=locked
                        )
                except Exception:
                    logger.warning(
                        "Failed to record episode %r status after exception in resume",
                        self.run_id,
                        exc_info=True,
                    )
            raise
        self._record()
        if self._agent.memory is not None:
            final_status = "escalated" if self.interrupted else "done"
            record_episode_status(
                self._agent.memory, self.run_id, final_status, locked=locked
            )
        if self._agent.checkpoints == "latest":
            prune_checkpoints(self._agent.graph, locked)
        return self

    def resume(
        self, feedback: object, *, interrupt_id: str | None = None
    ) -> Run:
        cfg = cast("dict[str, Any]", self.config)
        thread_id = str(cfg["configurable"]["thread_id"])
        with thread_lock(self._agent.graph.checkpointer, thread_id) as locked:
            return self._resume_locked(feedback, locked, interrupt_id=interrupt_id)

    def end(self, *, abandon: bool = False) -> None:
        """End this one-shot run by deleting its thread from the checkpointer.

        Only the checkpointer thread is deleted; vsigma memory stays. Callers must read
        `result` or other properties before calling `end()`.

        Raises:
            ValueError: If called on a run that belongs to a session (call `Session.end()` instead).
            RuntimeError: If called on an interrupted run without `abandon=True`.
        """
        if self.session_id is not None:
            raise ValueError(
                "Run.end() cannot end a session thread; call Session.end() instead."
            )
        cfg = cast("dict[str, Any]", self.config)
        thread_id = str(cfg["configurable"]["thread_id"])
        with thread_lock(self._agent.graph.checkpointer, thread_id) as locked:
            if self.interrupted and not abandon:
                raise RuntimeError(
                    f"Run '{self.run_id}' is paused; pass abandon=True to delete its thread."
                )
            was_interrupted = self.interrupted
            cast("Any", self._agent.graph.checkpointer).delete_thread(thread_id)
            if was_interrupted and self._agent.memory is not None:
                record_episode_status(
                    self._agent.memory, self.run_id, "abandoned", locked=locked
                )
