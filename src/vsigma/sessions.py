"""Multi-round interaction — the `Session` handle.

The spine transports adapt into: whatever way input arrives — direct call today;
HTTP adapters or a message bus later — it terminates in `session.send(payload)`.

In vsigma 4.8, `Session` is a stateless projection over the checkpointer's thread
(`thread_id = session_id`). Dialog, pause status, and interrupt payloads are read
directly from the thread's checkpoint snapshot, so handles recreated in another
process see identical state.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from uuid import uuid4

from langchain_core.messages import convert_to_openai_messages
from langgraph.types import StateSnapshot

from .critique import dialog_turns
from .episodes import record_episode_status
from .run import Agent, PauseChanged, Run, SessionPaused
from .threads import SessionBusy, thread_lock

__all__ = ["PauseChanged", "Session", "SessionBusy", "SessionPaused"]


class Session:
    """A dialog with an agent: several episodes sharing continuity on one thread.

    In vsigma 4.8, a session is a LangGraph thread (thread_id = session_id).
    The handle holds no process-local state: dialog, pause, and interrupts are
    read directly from the checkpointer snapshot, so handles recreated in
    another process or thread see identical state.
    """

    def __init__(
        self, agent: Agent, *, session_id: str | None = None, user_id: str | None = None
    ) -> None:
        self._agent = agent
        self.session_id = session_id or uuid4().hex
        self.user_id = user_id

    @property
    def config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self.session_id}}

    @property
    def snapshot(self) -> StateSnapshot:
        return self._agent.graph.get_state(cast("Any", self.config))

    @property
    def paused(self) -> bool:
        return bool(self.snapshot.interrupts)

    @property
    def interrupt(self) -> Any:
        return self.snapshot.interrupts[0].value if self.snapshot.interrupts else None

    @property
    def interrupt_id(self) -> str | None:
        return self.snapshot.interrupts[0].id if self.snapshot.interrupts else None

    @property
    def dialog(self) -> list[dict[str, object]]:
        """The dialog carried across rounds (derived from thread history)."""
        messages = cast("Sequence[Any]", self.snapshot.values.get("messages", []))
        converted = cast("list[dict[str, object]]", convert_to_openai_messages(messages))
        return dialog_turns(converted)

    def send(
        self, payload: str, *, channel: str | None = None, abandon: bool = False
    ) -> Run:
        """Send a new turn to this session; returns the Run.

        If the session is currently paused at an interrupt, this raises `SessionPaused`
        unless `abandon=True` is passed (preventing LangGraph from silently dropping
        the pending interrupt).
        """
        return self._agent.run(
            payload,
            session_id=self.session_id,
            user_id=self.user_id,
            channel=channel,
            abandon=abandon,
        )

    def resume(
        self, value: object, *, interrupt_id: str | None = None
    ) -> Run:
        """Resume a paused turn in this session."""
        with thread_lock(self._agent.graph.checkpointer, self.session_id) as locked:
            snapshot = self.snapshot
            if not snapshot.interrupts:
                if interrupt_id is not None:
                    raise PauseChanged(None, None)
                raise RuntimeError(f"Session '{self.session_id}' is not paused")

            metadata = snapshot.metadata or {}
            if "run_id" not in metadata:
                raise RuntimeError(
                    f"Paused checkpoint for session '{self.session_id}' has no run_id in metadata"
                )
            rid = str(metadata["run_id"])
            tid = str(metadata.get("task_id", rid))

            run = Run(self._agent, rid, tid, self.session_id)
            return run._resume_locked(value, locked, interrupt_id=interrupt_id)

    def end(self, *, abandon: bool = False) -> None:
        """End this session by deleting its thread from the checkpointer.

        Only the checkpointer thread is deleted; vsigma memory (transcripts,
        summaries, distilled knowledge) stays. Callers should read any final
        state or results before calling `end()`.

        Raises:
            SessionPaused: If called on a paused session without `abandon=True`.
        """
        thread_id = str(self.config["configurable"]["thread_id"])
        with thread_lock(self._agent.graph.checkpointer, thread_id) as locked:
            snapshot = self.snapshot
            if snapshot.interrupts and not abandon:
                intr = snapshot.interrupts[0]
                raise SessionPaused(self.session_id, intr.value, intr.id)
            was_paused = bool(snapshot.interrupts)
            metadata = snapshot.metadata or {}
            latest_run_id = str(metadata["run_id"]) if "run_id" in metadata else None
            cast("Any", self._agent.graph.checkpointer).delete_thread(thread_id)
            if was_paused and latest_run_id is not None and self._agent.memory is not None:
                record_episode_status(
                    self._agent.memory, latest_run_id, "abandoned", locked=locked
                )
