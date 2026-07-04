# Proposal: Interaction Model — Sessions, Transports, and the Bus Boundary

> **Status: PROPOSED (2026-07-04).** The session slice (grain 2) is Phase 4.2 and
> implements §B; the bus (grain 3) stays deferred until after Phase 4.5 — this document
> fixes the boundary between them so the session surface built now is the surface the
> bus later delivers into.

## A. The three interaction grains

vsigma agents take input at three grains, covered unevenly today:

1. **Mid-run input** ⚙ — a paused run continues with feedback: `interrupt()` /
   `run.resume()`. Exists since Phase 1.
2. **Between-run continuity (multi-round dialog)** — several episodes in sequence within
   one session, each new run aware of what came before. The scope ladder (session scope,
   `session_id`, consolidation writing into session scope) exists; the *ergonomic
   surface* does not: today each `agent.run()` starts a blank conversation. **This is
   the gap consumers hit first** (bumblebee: "one playbook input, want multiple
   rounds"). → §B, Phase 4.2.
3. **Async external signals** ✎ — input the agent didn't ask for, arriving on its own
   schedule: webhooks, queues, other agents. The messaging bus. → §C, post-4.5.

## B. Grain 2: the `Session` handle

### The layering argument (why sessions come before the bus)

Every transport, whatever it is, must ultimately turn a delivered message into
*"continue session S, as persona P, with this payload."* That operation is the session
handle. The bus is a pipe; the session is the context boundary the pipe empties into:

```
CLI / playbook loop ──► direct call ─┐
HTTP endpoint ────────► adapter ─────┼──► Session.send ──► episode run
message bus (post-4.5) ► subscriber ─┘
```

Same pattern as tools (`Tool` is the spine, sources adapt in; the engine never learns
about sources): **`Session` is the spine for interaction; transports adapt in.** Built
in this order the bus arrives as a thin delivery layer. Built bus-first, it would arrive
with nothing to deliver into and would grow interaction semantics it shouldn't own.
This is the concrete form of the running-context decision that sessions are
transport-agnostic and channel is event metadata, never a scope.

### API (v0)

```python
session = agent.session(session_id="s1", user_id="alice")   # or fresh: agent.session()
run = session.send("first instruction")                     # episode 1
run = session.send("refine it like this", channel="cli")    # episode 2, carries context
```

- `send(payload, *, channel=None) -> Run` — one round: starts a new episode whose
  prompt includes the session dialog so far. **Envelope-compatible by construction**:
  `(payload, author persona = session.user_id, session, channel)` is the §5 event
  envelope minus delivery concerns — a future bus subscriber maps an event onto `send`
  one-to-one. Channel is recorded as metadata on the persisted dialog, but *never* sent
  to the model and never a scope.
- **Grain unification:** if the previous round escalated (paused at human-interrupt),
  `send` routes the payload to `run.resume()` instead of starting a new episode — the
  caller has one verb for "here is the next human input" regardless of whether the agent
  is mid-run or between runs.

### Continuity mechanism (v0: deterministic, no model calls)

- After each round, the **dialog turns** are extracted from the final state — user and
  assistant messages only; tool traffic, system prompts, recall injections
  (`[vsigma:recall]`) and critique feedback (`[critique]`) are working noise, not dialog.
- The dialog is persisted to `session:<id>/context` (key `dialog`) in the agent's
  memory store — so a `Session` handle **rehydrates**: constructing a handle for an
  existing session id on a persistent store (SQLite) resumes the dialog across process
  restarts. This retires the "multi-episode session rehydration" pending demo.
- The next `send` seeds the new episode with that dialog via `Agent.run(history=...)`
  (additive parameter). Memory-side continuity (session-scope knowledge via
  `RecallContext`, episode summaries via the reflexive consolidator) composes on top
  unchanged — transcript continuity and knowledge continuity are separate channels.

### Non-goals for v0 (recorded, not lost)

- **No summarization/compaction of the carried dialog** — long sessions eventually need
  it (§10's progressive summarization ✎); v0 carries the raw dialog and
  `SlidingWindowContext` already bounds the model window. The measured version belongs
  with Phase 4.5's eval slice.
- **No concurrency control** — two `send`s from different processes on one session are
  last-writer-wins on the dialog record. Real arbitration is a bus concern (§C).
- **No multi-persona turn attribution beyond `user_id`** — the dialog records who the
  serving persona was per round; richer author attribution rides the envelope (§C).

## C. Grain 3: the bus (deferred, boundary fixed now)

What the bus adds when it comes (post-4.5, when something genuinely *unsolicited* needs
to reach an agent): delivery — subscriptions, queues/transports, arbitration of
concurrent input, richer envelopes `(author_persona, session, channel, payload)` with
author ≠ serving persona (multi-persona sessions, agent-to-agent). What the bus does
**not** own: interaction semantics. A bus subscriber terminates in `session.send(...)`
(or `run.resume(...)`); it never invokes graphs directly or grows its own context rules.

Trigger to build: a consumer needs reactive input (schedule, webhook, another agent) —
not before. (Phase 5's `delegate` may become that consumer.)

## D. Graduation criteria

- A consumer (bumblebee) drives a real multi-round dialog through `Session` with no
  engine changes. — *the field test*
- Rehydration proven: process restart, same session id, dialog continues (SQLite). — *v0 test*
- The bus, when built, lands as adapters onto this surface with no change to `Session`'s
  API. — *the §B layering claim, testable only then*
