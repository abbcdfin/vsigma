"""Two-round consolidation (§E) — reflexive at run-end, deep at maintenance.

Both rounds are the *same* construction engine (`construction.distill`) under
different configuration; neither is a second subsystem.

* **Reflexive** (round 1): runs on the agent's clock at run-end — bounded
  (at most one LLM call over a capped trajectory window) and skippable (with
  no LLM it degrades to a mechanical episode summary; a malformed pass is
  dropped, never breaks the run). Output is provisional: low confidence,
  ``consolidator="reflexive"``.
* **Deep** (round 2): offline, operator-triggered, with the most capable
  constructor available. Reads raw trajectories across episodes (the seam's
  episodic capture is the retained source of truth), emits high-confidence
  records — and, because record ids are content-addressed, *supersedes*
  matching round-1 records on ingest instead of duplicating them.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from .construction import Source, distill
from .llm import LLM
from .memory import MemoryStore
from .records import MemoryRecord, Provenance
from .scope import AGENT, Scope, episode, persona, session
from .state import State

#: The run-end hook signature (see `Services.run_end`).
RunEndHook = Callable[[State, MemoryStore], None]

#: Reflexive input cap: at most this many trajectory steps feed the one call.
REFLEXIVE_WINDOW = 20


def _knowledge_scope(state: State) -> Scope:
    """Where provisional knowledge lands: the serving persona if known, else
    the session, else the agent's own scope."""
    if state.get("user_id"):
        return persona(str(state["user_id"]))
    if state.get("session_id"):
        return session(str(state["session_id"]))
    return AGENT


def episode_trajectory(memory: MemoryStore, run_id: str) -> Source:
    """The retained raw trajectory of one episode, as a distill source."""
    steps = memory.query(episode(run_id).namespace("context"))
    text = json.dumps(dict(steps[-REFLEXIVE_WINDOW:]), default=str)
    return (f"trajectory:{run_id}", text)


def reflexive_consolidator(
    llm: LLM | None = None, *, constructor: str = "reflexive"
) -> RunEndHook:
    """Build the round-1 run-end hook. `llm=None` degrades to mechanical
    capture only — quality degrades gracefully, nothing breaks."""

    def consolidate(state: State, memory: MemoryStore) -> None:
        run_id = str(state.get("run_id"))
        # Mechanical episode summary — always written, LLM or not.
        summary = (
            f"episode {run_id}: status={state.get('status')}, result={state.get('result')!r}"
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
        if llm is None:
            return
        # The one bounded LLM call: distill provisional knowledge from the
        # episode's captured trajectory.
        try:
            records = distill(
                [episode_trajectory(memory, run_id)],
                llm,
                scope=_knowledge_scope(state),
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
