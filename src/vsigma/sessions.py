"""Multi-round interaction — the `Session` handle.

The spine transports adapt into (interaction-model proposal §B): whatever way
input arrives — direct call today; HTTP adapters or a message bus later — it
terminates in `session.send(payload)`, which continues the dialog as a new
episode (or resumes a paused one). Sessions are transport-agnostic; `channel`
is recorded as event metadata, never sent to the model, never a scope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from uuid import uuid4

from .critique import CRITIQUE_MARKER
from .scope import session as session_scope

if TYPE_CHECKING:
    from .run import Agent, Run

_DIALOG_KEY = "dialog"


def _dialog_turns(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    """Extract the dialog from a transcript: user + assistant turns only.

    Tool traffic, system prompts (incl. recall injections) and critique
    feedback are per-episode working noise, not dialog.
    """
    turns: list[dict[str, object]] = []
    for message in messages:
        if message.get("role") not in ("user", "assistant"):
            continue
        if message.get("tool_calls"):
            continue
        content = str(message.get("content", ""))
        if content.startswith(CRITIQUE_MARKER):
            continue
        turns.append({"role": message["role"], "content": content})
    return turns


class Session:
    """A dialog with an agent: several episodes sharing continuity.

    Each `send` is one round. The dialog so far is persisted to the session's
    context namespace in the agent's memory store, so a handle constructed for
    an existing session id **rehydrates** — on a persistent store the dialog
    survives process restarts. Memory-side continuity (session-scope knowledge
    via `RecallContext`, consolidated episode summaries) composes on top.
    """

    def __init__(
        self, agent: Agent, *, session_id: str | None = None, user_id: str | None = None
    ) -> None:
        self._agent = agent
        self.session_id = session_id or uuid4().hex
        self.user_id = user_id
        self.runs: list[Run] = []
        stored = agent.memory.read(self._namespace, _DIALOG_KEY)
        self._dialog: list[dict[str, object]] = (
            cast("list[dict[str, object]]", stored) if isinstance(stored, list) else []
        )
        self._turn = len(
            [key for key, _ in agent.memory.query(self._namespace) if key.startswith("turn:")]
        )

    @property
    def _namespace(self) -> str:
        return session_scope(self.session_id).namespace("context")

    @property
    def dialog(self) -> list[dict[str, object]]:
        """The dialog carried across rounds (read-only copy)."""
        return list(self._dialog)

    def send(self, payload: str, *, channel: str | None = None) -> Run:
        """One round of interaction; returns the (completed or paused) `Run`.

        Grain unification: if the previous round escalated, the payload is
        routed to `resume()` on that run — one verb for "here is the next
        human input", mid-run or between runs.
        """
        if self.runs and self.runs[-1].status == "escalated":
            run = self.runs[-1].resume(payload)
        else:
            run = self._agent.run(
                payload,
                session_id=self.session_id,
                user_id=self.user_id,
                history=list(self._dialog),
            )
            self.runs.append(run)
        # The event envelope (payload, author persona, session, channel) is
        # persisted as metadata; the model never sees `channel`.
        self._agent.memory.write(
            self._namespace,
            f"turn:{self._turn}",
            {
                "payload": payload,
                "user_id": self.user_id,
                "channel": channel,
                "run_id": run.run_id,
            },
        )
        self._turn += 1
        self._dialog = _dialog_turns(
            cast("list[dict[str, object]]", run.state.get("messages", []))
        )
        self._agent.memory.write(self._namespace, _DIALOG_KEY, self._dialog)
        return run
