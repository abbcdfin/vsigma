"""Critique / Reflexion — validate a candidate answer, feed failures back.

`critic_node` closes the generate → validate → revise loop with a
*deterministic* validator (the Reflexion insight: the strongest critique is an
objective check, e.g. running tests — not a model grading itself). The
validator returns `None` to accept or a critique string to reject; on
rejection the critique is appended as feedback and the loop retries.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from .state import NodeFn, State

#: A deterministic validator: candidate result -> None (accepted) or a
#: critique explaining the failure (rejected).
Validator = Callable[[str], str | None]

#: Prefix marking critique feedback messages (mirrors RECALL_MARKER's role:
#: makes the injected message identifiable in transcripts and tests).
CRITIQUE_MARKER = "[critique]"


def critic_node(validator: Validator) -> NodeFn:
    """Validate `state["result"]`; accept (stay done) or reject with feedback.

    On rejection the critique is appended as a user-role message and `status`
    flips back to ``"running"`` so the loop's router sends the agent another
    revision turn. Wire it after the acting node's ``done`` branch; route its
    own output with `break_on_attempts` so a candidate that always finishes
    but never validates escalates instead of spinning.
    """

    def node(state: State) -> State:
        critique = validator(str(state.get("result", "")))
        if critique is None:
            return {"status": "done"}
        messages = list(cast("list[dict[str, object]]", state.get("messages", [])))
        messages.append(
            {
                "role": "user",
                "content": f"{CRITIQUE_MARKER} Your answer was rejected:\n"
                f"{critique}\nRevise your answer.",
            }
        )
        return {"messages": messages, "status": "running"}

    return node
