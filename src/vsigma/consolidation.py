"""Two-round consolidation (§E) — reflexive at run-end, deep at maintenance.

Both rounds are the *same* construction engine (`construction.distill`) under
different configuration; neither is a second subsystem.

* **Reflexive** (round 1): runs on the agent's clock at every terminal edge —
  bounded (at most one LLM call over a capped trajectory window) and skippable
  (with no LLM it degrades to a mechanical episode summary; a malformed pass is
  dropped, never breaks the run). The mechanical summary is written for *every*
  terminal status (done, escalated) so retry runs can find failed prior
  attempts; distillation runs only on ``done`` — a cheap pass over a failure
  trajectory risks storing the mistake as knowledge, so failure-learning is
  the deep round's job. Output is provisional: low confidence,
  ``consolidator="reflexive"``.
* **Deep** (round 2): offline, operator-triggered, with the most capable
  constructor available. Reads raw trajectories across episodes (the
  interception points' episodic capture is the retained source of truth),
  emits high-confidence records — and, because record ids are
  content-addressed, *supersedes* matching round-1 records on ingest instead
  of duplicating them **when it targets the same scope**. Round 1 writes to
  the session (see `_knowledge_scope`); a deep pass aimed wider adds a second
  copy that `recall` returns alongside the first.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

from .construction import Source, distill
from .critique import dialog_turns
from .llm import LLM
from .memory import MemoryStore
from .records import MemoryRecord, Provenance
from .scope import Scope, episode, session
from .state import State

#: The run-end hook signature (see `Interceptor.run_end`).
RunEndHook = Callable[[State, MemoryStore], None]

#: Reflexive input cap: at most this many trajectory steps feed the one call.
REFLEXIVE_WINDOW = 20


#: Chooses where a round-1 distillation lands, or `None` to write nothing
#: durable for this run. See `reflexive_consolidator(knowledge_scope=)`.
KnowledgeScope = Callable[[State], Scope | None]


def _knowledge_scope(state: State) -> Scope | None:
    """The narrowest durable scope available — and where none is, nothing.

    Round-1 output is *provisional*: one cheap call, confidence 0.3, unreviewed.
    Writing it any wider than the session it came from exposes unvetted material
    to turns that never asked for it, so this rule is deliberately unable to
    widen. It cannot reach persona (a `user_id` is an identity claim, not a
    review) and it cannot reach `AGENT`, which `visible_scopes` returns for
    *every* persona and would therefore be the widest exposure in the system.

    No fallback to `episode` either. Episode scope is visible only during the
    run that owns it, so an episode-scoped *knowledge* record can never be read
    again — a write-only sink that looks like a feature. (The mechanical summary
    below does fall back to episode; that fallback is already a dead letter
    there and is not copied here.)

    Returning `None` skips the distillation write entirely. The run is
    unaffected — the same contract the hook already has for a missing LLM or a
    malformed pass. Widening is `promotion`'s job, not construction's: see
    docs/proposals/memory-scope-promotion.md §B.1 and §C.
    """
    if state.get("session_id"):
        return session(str(state["session_id"]))
    return None


def episode_trajectory(
    memory: MemoryStore,
    run_id: str,
    *,
    window: int | None = None,
    max_prior_turns: int = 3,
) -> Source:
    """The retained raw trajectory of one episode, as a distill source.

    Each node update in episodic context carries only the messages added by that
    node firing (append-only semantics), and the turn's prompt is recorded in
    the input row. Reading the rows in insertion order yields the complete,
    untruncated conversation without cumulative duplicates.

    When `window` is given (the reflexive round's designed bound), the input row
    is always kept so the task is preserved, along with the trailing `window` rows.
    When `window` is None (the deep pass), the entire transcript is included.

    For sessions with prior turns, a bounded tail of earlier transcripts (up to
    `max_prior_turns`) is prepended as prior context, so single-turn phrases
    like "yes, do that" retain distillation grounding.
    """
    from .episodes import EpisodeOwner, episode_owner, episode_owners

    prior_entries: list[tuple[str, object]] = []
    this_owner = episode_owner(memory, run_id)
    if this_owner and this_owner.session_id and max_prior_turns > 0:
        all_owners = episode_owners(memory)
        session_eps: list[EpisodeOwner] = []
        for ep in all_owners.values():
            if ep.run_id == run_id:
                break
            if ep.session_id == this_owner.session_id:
                session_eps.append(ep)
        for ep in session_eps[-max_prior_turns:]:
            ep_steps = memory.query(episode(ep.run_id).namespace("context"))
            messages: list[dict[str, object]] = []
            for k, v in ep_steps:
                if not k.startswith("episode:") and isinstance(v, dict) and "messages" in v:
                    msgs = v["messages"]
                    if isinstance(msgs, list):
                        messages.extend(cast("list[dict[str, object]]", msgs))
            dialog = dialog_turns(messages)
            if dialog:
                prior_entries.append((f"prior_context:{ep.run_id}", dialog))

    steps = memory.query(episode(run_id).namespace("context"))
    if window is not None and len(steps) > window:
        input_steps = [s for s in steps[:-window] if s[0].startswith("input:")]
        selected = input_steps + steps[-window:]
    else:
        selected = steps

    cleaned: list[tuple[str, object]] = prior_entries + [
        (k, v) for k, v in selected if not k.startswith("episode:")
    ]
    text = json.dumps(dict(cleaned), default=str)
    return (f"trajectory:{run_id}", text)


def reflexive_consolidator(
    llm: LLM | None = None,
    *,
    constructor: str = "reflexive",
    knowledge_scope: KnowledgeScope = _knowledge_scope,
) -> RunEndHook:
    """Build the round-1 run-end hook. `llm=None` degrades to mechanical
    capture only — quality degrades gracefully, nothing breaks.

    Called at every terminal edge: the mechanical summary is written for any
    terminal status (its id is fixed per episode, so escalate-then-resume just
    overwrites it with the final status); the LLM distillation runs only when
    the run ended ``done``.

    `knowledge_scope` decides where that distillation lands, and defaults to the
    narrowest-durable-scope rule (`_knowledge_scope`): the session, or nothing.
    It is the escape hatch for a consumer who wants something wider — an
    explicit, recorded choice, never an inferred fallback. To restore the
    pre-Phase-4.7 behaviour of widening to the serving persona, pass it in::

        def persona_first(state: State) -> Scope | None:
            if state.get("user_id"):
                return persona(str(state["user_id"]))
            return session(str(state["session_id"])) if state.get("session_id") else AGENT

        reflexive_consolidator(llm, knowledge_scope=persona_first)

    Note what that opts into: unreviewed 0.3-confidence material visible to
    every later session of that persona, and — via the `AGENT` arm — to every
    persona the agent serves.
    """

    def consolidate(state: State, memory: MemoryStore) -> None:
        run_id = str(state.get("run_id"))
        task_id = str(state.get("task_id") or run_id)
        # Mechanical episode summary — always written, whatever the terminal
        # status. Carries the task_id so a retry run (same task, new episode)
        # can find prior attempts in session context; task is correlation
        # metadata, not a scope rung (see architecture_design §5).
        summary = (
            f"episode {run_id} (task {task_id}): "
            f"status={state.get('status')}, result={state.get('result')!r}"
        )
        target = (
            session(str(state["session_id"])) if state.get("session_id") else episode(run_id)
        )
        record = MemoryRecord(
            id=f"episode:{run_id}",
            scope=target.head,
            kind="context",
            content=summary,
            provenance=Provenance(
                origin="consolidated",
                source_ref=f"trajectory:{run_id}",
                confidence=0.3,
                consolidator="reflexive",
                constructor=constructor,
            ),
        )
        memory.write(record.namespace, record.id, record.to_dict())
        # Distill only from successful runs: on a failure trajectory a cheap
        # pass can't reliably invert "what happened" into "what to avoid" —
        # mining failures is the deep round's job (capable constructor,
        # cross-episode comparison).
        if llm is None or str(state.get("status")) != "done":
            return
        target_scope = knowledge_scope(state)
        if target_scope is None:
            return  # no durable destination for provisional knowledge
        # The one bounded LLM call: distill provisional knowledge from the
        # episode's captured trajectory.
        try:
            records = distill(
                [episode_trajectory(memory, run_id, window=REFLEXIVE_WINDOW)],
                llm,
                scope=target_scope,
                kind="knowledge",
                origin="consolidated",
                consolidator="reflexive",
                confidence=0.3,
                constructor=constructor,
            )
        except ValueError:
            return  # skippable: a malformed reflexive pass never breaks the run
        for rec in records:
            memory.write(rec.namespace, rec.id, rec.to_dict())

    return consolidate


def deep_consolidate(
    episodes: list[Source],
    llm: LLM,
    *,
    scope: Scope,
    constructor: str,
) -> list[MemoryRecord]:
    """Round 2: cross-episode distillation with a capable constructor. Returns
    records for a bundle (commit through `write_bundle` + the ingest gate)."""
    return distill(
        episodes,
        llm,
        scope=scope,
        kind="knowledge",
        origin="consolidated",
        consolidator="deep",
        confidence=0.9,
        constructor=constructor,
    )
