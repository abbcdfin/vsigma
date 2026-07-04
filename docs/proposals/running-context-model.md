# Proposal: Running-Context Model — Persona / Session / Episode

> **Status: LARGELY VALIDATED (Phase 4, 2026-07-04).** The scope ladder is in code
> (`scope.py`: structured namespaces, `user_id` on `BaseState`) and the persona-visibility
> rule is enforced by `recall` on the retrieve path (tested: private memory never crosses
> personas). Still pending: a multi-episode session rehydration demo, and the event envelope
> (built only when a messaging seam is pulled by a real reactive consumer). Extends the
> execution hierarchy in the [composition-model proposal](./composition-model.md); defines the
> **scope axis** of the [memory subsystem](./memory-construction.md). Graduates into
> [`architecture_design.md`](../architecture_design.md) in the next consolidation pass.

## Motivation

Memory scoping is only as clean as the model of *running context* beneath it. Before the
memory subsystem lands (Phase 4), we fix the vocabulary: what a user is, what a session is,
what one execution is, and where transport/channel fits — so memory tiers map onto stable
concepts instead of ad-hoc ids.

## The ladder (scope hierarchy)

```
global ─ agent ─ persona ─ session ─ episode ─ step
                └────────── the running context ─────────┘
```

| Layer | What it is | Maps to (code) | Lifespan |
|---|---|---|---|
| **Global** | world/org scope shared by all agents & users | a fixed namespace | forever |
| **Agent** | this agent's own identity: skills, learned strategies, semantic base | a fixed namespace | agent lifetime |
| **Persona** | a user/principal — stable identity across sessions *and* channels | `user_id` (new; joins `BaseState`/`Services` config) | user lifetime |
| **Session** | one continuous dialog — a continuity envelope | `session_id` ↔ a memory namespace (*not* a thread) | many episodes |
| **Episode** | **one state-graph run** for one task-arc, incl. its resumes | `run_id` ↔ a LangGraph thread | one task |
| **Step** | one node firing inside an episode | a trajectory event | one node exec |

Relationship to the composition model's `loop / run / session / task / step`: unchanged —
**episode is the memory-facing name for a run** (episodic memory is literally
memory-of-episodes). The ladder adds the layers *above* session (persona, agent, global) that
the execution hierarchy never needed but memory scoping does.

### Grain decisions (settled)

* **Episode = one thread = one task-arc.** A `resume()` after human-interrupt continues the
  *same* episode (same thread). A recurring goal produces *multiple* episodes.
* **Episodes nest.** A `delegate`d subagent is a child episode (`parent_run_id` already
  exists). The ladder tolerates nesting: a child episode may inherit/share its parent's
  session scope.
* **Session is optional** (a one-shot pipeline is just an episode), as before.

## Channels: transport is *event metadata*, never a scope

A session is a **continuity unit, not a transport unit** — what defines it is "the same
ongoing dialog," regardless of how the bits arrive. Therefore:

* **One session may span multiple channels** (start on chat, continue over email; a HITL
  approval arriving by email belongs to the *same* session).
* **Channel lives on each inbound/outbound event (turn), not on the session:**

  ```
  event = (author_persona, session, channel, payload)
  ```

* "Channel" decomposes into two things kept separate: the **medium** (Slack / email / voice —
  a rendering & policy signal) and the **reply-to endpoint** (the specific thread / address to
  answer on). A session may *accumulate* a set of endpoints it has seen; it is never defined
  by one.
* **Correlation is the integration's job.** Deciding that an inbound email belongs to session
  S is identity-stitching done by the channel adapter (thread token, persona + recency, …).
  The toolkit accepts a `session_id` and trusts it; it does not solve stitching itself.
* Channel is **never a memory scope** — it is metadata on events plus a policy input
  (voice ⇒ short answers), and stays out of the ladder entirely.

## Multiple personas per session

A session has a **set of participants**, not one owner. The event envelope above already
carries `author_persona` per turn (symmetric with `channel`). Consequences:

1. **Attribution** — every turn records which persona authored it.
2. **Visibility is a first-class property of scope.** Persona differs from channel: it is
   both turn-attribution *and* the scope for **private** memory. In a group session two kinds
   of memory are live at once:
   * **session-shared** — visible to all participants of the session;
   * **persona-private** — a participant's durable facts, visible only when serving *that*
     persona.
   The agent must never surface persona A's private memory into a turn serving persona B.
   Every memory scope therefore carries a visibility rule (global/agent: all; session: its
   participants; persona: that persona only).

## What this model is *for*

This ladder **is the scope axis** of the memory architecture (one of its three axes — see the
[memory subsystem proposal](./memory-construction.md) §C). Each layer is a memory namespace
with a lifespan and a visibility rule; consolidation flows *up* the ladder
(step → episode → session → persona/agent).

> **Terminology guard** — "context" is used three ways; keep them apart:
> 1. **scope** (this ladder) — persona/session/episode; do *not* call the ladder "context";
> 2. **context memory** (a *kind*, vs knowledge memory) — situational, volatile stored memory;
> 3. **context management** — the runtime *assembly* policy that reads memory to build each
>    model window.

## Impact on code (deferred to Phase 4)

* `BaseState` gains `user_id` (persona); memory namespaces become structured scope paths.
* The event envelope `(author_persona, session, channel, payload)` becomes the natural
  inbound contract if/when an external-signal seam (messaging) is added — defined here, built
  later, pulled by a real reactive consumer.

## Graduation criteria

Promote into [`architecture_design.md`](../architecture_design.md) only after Phase 4
validates it:

* Memory namespaces actually keyed by the scope ladder, with at least persona + session +
  episode scopes exercised by tests.
* The persona-visibility rule enforced on the retrieve path (a persona-private record is not
  returned when serving another persona) — proven by a test.
* One multi-episode session demonstrated (two runs sharing session scope, rehydration works).
