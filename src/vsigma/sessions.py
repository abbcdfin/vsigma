"""Multi-round interaction — the `Session` handle.

The spine transports adapt into: whatever way input arrives — direct call today;
HTTP adapters or a message bus later — it terminates in `session.send(payload)`.

In vsigma 4.8, `Session` is a projection over the checkpointer's thread
(`thread_id = session_id`). Dialog, pause status, and interrupt payloads are read
directly from the thread's checkpoint snapshot, but the handle holds the
process-local `run_context` object.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from uuid import uuid4

from langchain_core.messages import convert_to_openai_messages
from langgraph.types import StateSnapshot

from .critique import dialog_turns
from .episodes import record_episode_status
from .expiry import complete_forget_locked, forget_session_locked
from .memory import MaintainableStore
from .run import Agent, PauseChanged, Run, SessionPaused
from .threads import SessionBusy, thread_lock

__all__ = ["PauseChanged", "Session", "SessionBusy", "SessionPaused"]


class Session:
    """A dialog with an agent: several episodes sharing continuity on one thread.

    In vsigma 4.8, a session is a LangGraph thread (thread_id = session_id).
    Thread state (dialog, pause, interrupts) is read directly from the
    checkpointer snapshot, but the handle holds the process-local `run_context`
    object, passed to turns executed via `send` or `resume`.
    """

    def __init__(
        self,
        agent: Agent,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        run_context: Any = None,
    ) -> None:
        self._agent = agent
        self.session_id = session_id or uuid4().hex
        self.user_id = user_id
        self._run_context = run_context

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
            run_context=self._run_context,
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

            run = Run(self._agent, rid, tid, self.session_id, run_context=self._run_context)
            return run._resume_locked(value, locked, interrupt_id=interrupt_id)

    def end(self, *, abandon: bool = False, forget: bool = False) -> None:
        """End this session by deleting its thread from the checkpointer.

        By default (`forget=False`), only the checkpointer thread is deleted;
        vsigma memory (transcripts, summaries, distilled knowledge) stays.
        With `forget=True`, `session:<id>/context` and `session:<id>/knowledge`
        are also deleted through the forget record, and the session's episodes
        are detached (`session_id=None` in `episodes/index`). Works after a
        plain `end()` has already deleted the thread. With `memory=None`,
        `forget=True` behaves as a plain `end()`. Callers should read any final
        state or results before calling `end()`.

        Raises:
            TypeError: If `forget=True` and the agent's store is not a `MaintainableStore`.
            SessionPaused: If called on a paused session without `abandon=True`.
        """
        store = self._agent.memory
        if forget and store is not None and not isinstance(store, MaintainableStore):
            raise TypeError(
                f"Session.end(forget=True) requires a MaintainableStore, got {type(store).__name__}"
            )
        thread_id = str(self.config["configurable"]["thread_id"])
        checkpointer = self._agent.graph.checkpointer
        with thread_lock(checkpointer, thread_id) as locked:
            if isinstance(store, MaintainableStore):
                complete_forget_locked(checkpointer, store, locked)
            snapshot = self.snapshot
            if snapshot.interrupts and not abandon:
                intr = snapshot.interrupts[0]
                raise SessionPaused(self.session_id, intr.value, intr.id)
            if forget and isinstance(store, MaintainableStore):
                forget_session_locked(checkpointer, store, locked)
            else:
                was_paused = bool(snapshot.interrupts)
                metadata = snapshot.metadata or {}
                latest_run_id = (
                    str(metadata["run_id"]) if "run_id" in metadata else None
                )
                cast("Any", checkpointer).delete_thread(thread_id)
                if was_paused and latest_run_id is not None and store is not None:
                    record_episode_status(
                        store, latest_run_id, "abandoned", locked=locked
                    )
