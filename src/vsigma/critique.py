"""Critique / Reflexion — validate a candidate answer, feed failures back.

`critic_node` closes the generate → validate → revise loop with a
*deterministic* validator (the Reflexion insight: the strongest critique is an
objective check, e.g. running tests — not a model grading itself). The
validator returns `None` to accept or a critique string to reject; on
rejection the critique is appended as feedback and the loop retries.
"""

from __future__ import annotations

from collections.abc import Callable

from .llm import LLM
from .state import NodeFn, State

#: A deterministic validator: candidate result -> None (accepted) or a
#: critique explaining the failure (rejected).
Validator = Callable[[str], str | None]

#: Prefix marking critique feedback messages (mirrors RECALL_MARKER's role:
#: makes the injected message identifiable in transcripts and tests).
CRITIQUE_MARKER = "[critique]"


def dialog_turns(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    """Extract clean dialog turns from a transcript: user + assistant turns only.

    Filters out tool calls, tool responses, system messages, and critique feedback.
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
        return {
            "messages": [
                {
                    "role": "user",
                    "content": f"{CRITIQUE_MARKER} Your answer was rejected:\n"
                    f"{critique}\nRevise your answer.",
                }
            ],
            "status": "running",
        }

    return node


def all_of(*validators: Validator) -> Validator:
    """Chain validators; the first critique wins.

    Order cheap deterministic clauses first and any non-deterministic clause
    (e.g. `llm_judge`) last — the judge then only sees candidates that already
    passed everything objectively checkable.
    """

    def validate(candidate: str) -> str | None:
        for validator in validators:
            critique = validator(candidate)
            if critique is not None:
                return critique
        return None

    return validate


_JUDGE_SYSTEM = (
    "You are a strict reviewer. Check the candidate ONLY against the rubric. "
    "Reject only on clear violations. If the candidate satisfies every point, "
    "reply exactly PASS. Otherwise reply with the specific violations, one per "
    "line — no other commentary."
)


def llm_judge(judge: LLM, rubric: str) -> Validator:
    """LLM-as-judge clause: one rubric'd completion, PASS or critique.

    The deliberate trade: unlike every other clause this is *not*
    deterministic — a model grading a model. Use a different (ideally
    stronger) model than the generator, keep the rubric specific criteria
    rather than "is this good?", and put it last in `all_of` behind the
    deterministic clauses. Fails closed: a judge error becomes a critique, so
    the loop's attempt budget escalates instead of the run crashing.
    """

    def validate(candidate: str) -> str | None:
        try:
            reply = judge.complete(
                [
                    {"role": "system", "content": _JUDGE_SYSTEM},
                    {
                        "role": "user",
                        "content": f"Rubric:\n{rubric}\n\nCandidate:\n{candidate}",
                    },
                ],
                [],
            ).text.strip()
        except Exception as exc:  # noqa: BLE001 — gate must not kill the run
            return f"validation unavailable (judge error: {exc})"
        return None if reply.upper().startswith("PASS") else reply

    return validate
