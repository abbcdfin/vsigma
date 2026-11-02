# Proposal: Session Admission — A Session Belongs to the User Who Started It

**Status:** **ACCEPTED** 2026-11-02 as Phase 4.17. Builds on Phase 4.16, forgetting session memory
([scope-lifespans](./scope-lifespans.md) §C.2).
**Closes:** the ✎ note in `architecture_design.md` §5: session visibility is not enforced.

## A. Problem

1. **A `session_id` is a bearer capability.** `agent.run` and `Session` take the id and nothing
   checks who is asking. Reproduced on `v0.3.1` with `MemorySaver`, Alice starting session `s`:
   - `agent.session("s", user_id="bob").dialog` returns Alice's messages;
   - Bob's `send` runs a turn on her thread;
   - Bob's `end()` deletes it.
2. **Memory leaks the same way.** `visible_scopes` admits `session:<id>` whenever the state names
   the id, so Bob's turn recalls Alice's session knowledge.
3. **A reused id reads old transcripts.** After Alice's `end()`, Bob's first turn on `s` has a
   clean thread, but `episode_trajectory` prepends Alice's turns as prior context, and they reach
   the distiller. Reproduced: her message is in the source distilled for Bob's turn.

## B. Design

**The rule: a session's `user_id` never changes.** The first turn sets it, and from then on only
that `user_id` may use the session. vsigma compares the id the host passes; authenticating the
user stays the host's job.

1. **The thread holds the owner.** Every checkpoint already records `user_id` in its metadata, and
   resume copies it, so there is no new field, no registry and no migration. Existing threads are
   protected from the first turn after upgrading.
2. **Equality is the whole check.** `metadata["user_id"] == user_id`, with `None` as a value:
   - a session started with no `user_id` stays open, and only turns with no `user_id` may use it.
     That is the way to share a session, and shared turns have no persona memory;
   - a handle with no `user_id` can't use an owned session.
3. **Refusal is `SessionDenied`** (`RuntimeError`), raised before anything is read or changed. It
   carries the session id and the id that was refused, never the owner.
4. **Two places check, and everything passes through them:**
   - `Agent.run`, first under the thread's lock, before `_require_context` and `SessionPaused`, so
     a refused caller learns nothing about a pause, and `abandon=True` doesn't get past it;
   - `Session.snapshot`, which `dialog`, `paused`, `interrupt`, `interrupt_id`, `resume` and `end`
     all read. Constructing a `Session` still can't fail.

   `recall` needs no check: its state comes from a turn that passed. A `Run` handle is issued to
   the caller that passed, and `Run.resume` keeps working from it.
5. **An ended thread can leave memory behind.** Plain `end()` deletes the thread and keeps session
   memory and transcripts, so the owner has to outlive the thread. On an agent with a store, `end()`
   of an owned session writes `sessions/owners/<session_id>` = `user_id`, in the same lock. When a
   thread has no checkpoint, `Agent.run` and `end(forget=True)` compare against this record instead.
   Forgetting deletes it, since nothing is left to guard. It is an operational namespace, so
   `visible_scopes` never yields it.
6. **Prior context comes from the same owner only.** `episode_trajectory` always applies the filter
   that `only_owner` makes optional today, and the parameter goes. This also covers a first turn
   that crashed before its first checkpoint, which leaves a transcript and no owner.

## C. Limits

- **Not authentication.** A host that passes a wrong `user_id` gets a wrong answer. `session_id`s
  should still come from the authenticated user, and `getting_started.md` keeps saying so.
- **Raw LangGraph is outside.** `agent.graph.get_state` reads any thread. `graph.update_state` writes
  a checkpoint without the tags, so the thread has no owner on record: with no `user_id` key in the
  metadata the check passes, and the next turn records its own `user_id`. `has_context` has the
  same limit.
- **`ThreadExpiry` without `store=`** deletes threads and can't write the record, so memory an app
  keeps in a store loses its guard. `agent.maintain` always passes the store.
- **Threads with several authors** take the owner from their latest checkpoint.

## D. Non-goals

- **Sharing with named users:** participants, `admit` and a private/open setting. Decided
  2026-11-02: neither known consumer shares a session, and this can be added later without breaking
  anything, by an `owner` key beside `user_id`.
- Roles or groups, which belong to the host.
- Keeping `session_id`s secret.

## E. Upgrading

This is a breaking change, released in `v0.4.0`. These now raise `SessionDenied`:

- a different `user_id` on an existing session;
- a named `user_id` on a session started without one, as when a session starts before login;
- a handle with no `user_id` reading an owned session.

## F. Graduation

Tests, each on `MemorySaver` and `SqliteSaver`:

- Bob's `dialog`, `paused`, `interrupt`, `interrupt_id`, `send`, `send(abandon=True)`, `resume` and
  `end` all raise on Alice's session, leaving her thread, pause and episode status unchanged.
- `agent.run(session_id=...)` refuses Bob the same way, with no episode recorded for him.
- A handle with no `user_id` is refused on an owned session, and a named one on an open session.
- Alice's session works across a restart, a resume and `checkpoints="latest"`.
- A thread written by `v0.3.1` is protected with no migration.
- After Alice's plain `end()`, Bob is refused and Alice may continue, with her memory intact.
- After `end(forget=True)` or expiry with a store, the id is free and the new owner sees nothing of
  the old one.
- Bob's distill source on a reused or crashed id holds none of Alice's turns.
- `sessions/owners` never reaches `visible_scopes` or `recall`.

A trial migration of a real multi-user consumer in a scratch copy passes unchanged, or with the
changes listed in `upgrading.md`.

On graduation, `architecture_design.md` §5 states the rule and loses its ✎ note.
