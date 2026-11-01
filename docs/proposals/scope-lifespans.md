# Proposal: Scope Lifespans — How Long Each Rung Keeps What It Holds

**Status:** **ACCEPTED** 2026-10-30. Built: §C.1 (Phase 4.12, `v0.2.3`), §C.3 (Phase 4.14, `v0.3.0`).
Waits: §C.2.
**Role:** the model and the contract between its components; it builds nothing itself.
**Implements:** architecture §8, "Axis 1 — Scope: namespace + visibility + lifespan". Namespace
and visibility are built; lifespan never was, and nothing in memory is ever deleted.
**Components:** [thread-concurrency](./thread-concurrency.md) (the per-thread lock everything
else relies on), [thread-expiry](./thread-expiry.md) (the session rung and one-shot runs),
[episode-retention](./episode-retention.md) (the episode rung's transcripts), and a memory store
that can delete (§D).

## A. The model

Memory spans threads. It is organized by the scope ladder (architecture §5), not by thread:

```
global ─ agent ─ persona ─ session ─ episode ─ step
```

A thread is where LangGraph keeps one rung's conversation: a session's (`thread_id =
session_id`) or a one-shot run's (`thread_id = run_id`). Each rung's lifespan:

| Rung | Holds | Ends when | Enforced by |
|---|---|---|---|
| episode | its transcript (capture rows); a one-shot run's thread | transcript: by the retention limits, once consolidated if consolidation is in use. Thread: `Run.end()`, or the app's `run_ttl` | episode retention; thread expiry |
| session | its thread (the conversation); session summaries; session knowledge, the default home of reflexive knowledge (`consolidation.py:51-72`) | `Session.end()`, or the app's session limits (idle TTL, keep the last N per user) | thread expiry |
| persona, agent, global | knowledge | not by age. The memory pass supersedes it; deleting conclusions is the tombstones design (maintenance-pass §D) | — |
| step | nothing of its own; steps are rows of the episode transcript | with the episode | — |

Outside the ladder, operational records keep the lifecycle, one writer per key so that no
record is read, modified and written by two parties (§C):

| Namespace | Holds | Written by | Lifespan |
|---|---|---|---|
| `episodes/index` | ownership, write-once (Phase 4.7) | the runtime, at run start | forever: a pruned `run_id` is never reused |
| `episodes/status` | the episode's status (§C.1) | the runtime and thread expiry's sweep, under the thread's lock | with the index entry |
| `episodes/tombstone` | a pruned episode's disposition and content hash, written before its rows are deleted (episode-retention §B.5) | episode retention | forever |
| `maintenance/state` | `deep:<run_id>` markers, with the namespace's size | the memory pass | until the episode is pruned |
| `sessions/forget` | intent records for unfinished session cleanup (§C.2) | thread expiry, `end(forget=True)` | until the cleanup completes |
| `sessions/index` (session-admission, not built) | registry entry | — | with its session |

## B. The contract

The app declares limits once, as data, and calls `agent.maintain` repeatedly. vsigma decides
what is due, enforces the safety rules, and deletes. The one limit vsigma chooses is the default
transcript budget (episode-retention §B.4); ending a conversation always takes the app's limit.
The jobs, how they are built and where they run are in [maintenance](./maintenance.md). Per turn,
unchanged: `checkpoints="latest"` and `retain_messages` (4.10).

**Concurrency.** Every operation that checks a thread and then changes it holds that thread's
lock, per [thread-concurrency](./thread-concurrency.md): turns, resumes, `end()`, expiry, and
completing a forget record. A busy thread raises `SessionBusy`, and expiry skips it. On the
memory side, one memory pass runs per store at a time. These are documented boundaries: one
process per checkpointer, whose agents are the only ones writing episodes to their store, and
one maintainer per store. Several processes need a lease in the database, later.

## C. The hand-off between the two

No ordering between the entry points is needed: each reads what the other left, whenever it
next runs.

1. **Every episode ends in a final status**, kept in `episodes/status` whenever memory is
   configured. Statuses come from every way a run can end, not only the ones `Run` reports:

   | Event | Status | Final |
   |---|---|---|
   | run starts | `running` | no |
   | run returns at an interrupt | `escalated` | no: it may still be resumed |
   | run returns finished | `done` | yes |
   | run or resume raises | `failed`, then the exception is raised as usual | yes |
   | a pause is discarded (`abandon=True`) | `abandoned` | yes |
   | its thread is deleted (`end()`, expiry) while the run is not final | `abandoned`: nothing can continue it | yes |
   | expiry's sweep finds it unfinished and dead (below) | `abandoned` | yes |

   - Each is written under the thread's lock, so writers never interleave, and a final status
     never changes. Deleting a thread comes before recording its run `abandoned`, so a crash
     between them leaves a non-final status for the sweep, never a final one on a pause that
     could still be resumed.
   - An episode without a status record ran outside `Agent` and has no lifecycle (episode-
     retention §B.3.1).
   - **Recovery.** A process that dies mid-run leaves its episode `running`, and a first turn
     under `durability="exit"` dies before writing any checkpoint, while `episodes/index`
     already holds its entry (`run.py:176`, before the run starts at `run.py:190`). No scan of
     threads can find it. Thread expiry with a store therefore sweeps `episodes/status` on every
     `run()`, after its deletes. For each unfinished episode it tries its thread's lock (the
     thread id from `episodes/index`) without waiting, skips a busy thread, and records
     `abandoned` when the episode is:
     - `running`: a live run holds its thread's lock from the moment it records `running` until
       it records its outcome, so a free lock means its process died, or it raised before
       `failed` was recorded;
     - `escalated`, and its thread no longer holds its pause: the thread is gone, its latest run
       is another, or, after a resume died under a durability that writes mid-run, it is no
       longer paused.

     The sweep relies on the boundary of §B: every agent writing this store's episodes runs in
     this process, over the checkpointer expiry was given. It reads `episodes/status` whole.
     `agent.maintain` always runs the sweep when the agent has a store; an app that skips it
     leaves crashed episodes `running`, and the memory pass never consolidates them.
2. **Session memory ends with its session, retried until done.** Cleanup spans two stores, so
   it starts by recording what it means to do. Under the thread's lock:
   1. write `sessions/forget/<session_id>` with the thread's latest checkpoint id;
   2. delete the thread;
   3. delete `session:<id>/context` and `session:<id>/knowledge`;
   4. delete the record.

   A crash leaves the record, and whoever next takes that thread's lock completes it first:
   thread expiry for every record, on each `run()`, and every turn, resume and `end()` for its
   own session. Completing branches on the thread's latest checkpoint:

   | Latest checkpoint | Meaning | Then |
   |---|---|---|
   | the recorded one | the crash came before step 2 | steps 2–4 |
   | none | the thread is gone: step 2 ran | steps 3–4 |
   | another | the thread moved on without completing the record | drop the record, delete nothing |

   - "None" is unambiguous under the lock: a session reusing the id completes the record before
     its first turn, so no new thread exists while the record does.
   - The last row cannot arise within the boundary, since every operation on the thread
     completes the record first. It keeps a stale record from deleting a live conversation.
   - A user who returns to a session whose cleanup was interrupted finds it ended, as if the
     cleanup had finished.

   `end(forget=True)` follows the same steps. Plain `end()` keeps memory, as 4.10 documents, and
   leaves no record: nothing will look for that memory again.
3. **Episodes outlive their session, and are consolidated only once they have ended.** The
   memory pass selects only episodes with a final status, so a running or paused episode is never
   read half-written. Its `deep:` marker records the namespace's size, and an episode counts
   as consolidated only while the two match. That one test decides selection, pruning, and the
   protection of prior context (episode-retention §B.3). Durable learning comes from the deep pass
   over those transcripts. Session knowledge is the reflexive round's provisional output
   (confidence 0.3, unreviewed), so deleting it with its session loses nothing the deep pass cannot
   rebuild, provided the transcripts are kept until consolidated, which episode retention guarantees
   whenever consolidation is in use (episode-retention §B.3.2).

## D. Storage

- **Checkpointer:** unchanged. Thread expiry supports `InMemorySaver` and `SqliteSaver`; others
  raise.
- **Memory store:** forgetting a session and pruning transcripts both delete, and `MemoryStore`
  cannot. The foundation is LangGraph's `SqliteStore` behind the `MemoryStore` protocol, in
  [store-adapter](./store-adapter.md), with no migration of existing data.
- Until the store can delete, thread expiry works on the checkpointer alone, and still records
  `abandoned`, which is a write.

## E. Build order

1. The per-thread lock, `SessionBusy` and the pause id
   ([thread-concurrency](./thread-concurrency.md), accepted 2026-10-29 as Phase 4.11), a bug
   fix for released behaviour: `v0.2.1`, with the `mcp<2` cap. Then thread expiry on the
   checkpointer, and the status records with expiry's recovery sweep (§C.1), which need only
   writes: `v0.2.3`, after `v0.2.2`, the first public release.
2. The [store adapter](./store-adapter.md): `v0.3.0`.
3. [Maintenance](./maintenance.md) with episode retention: `v0.3.0`.
4. Thread expiry's store side: forgetting session memory (§C.2).

## F. Alternatives considered

- **Policy functions the app writes.** Replaced by declarative limits. A custom function stays
  an escape hatch to add when a consumer needs a rule the limits cannot express.
- **Store TTL as the mechanism.** It deletes regardless of consolidation and cannot reach the
  checkpointer. The adapter may use it inside the session rung later.

## G. Non-goals

- Agent-driven memory: the LLM choosing what to remember or forget.
- Deleting knowledge in persona, agent or global scope (maintenance-pass §D).
- `TrajectoryLog` (opt-in, for debugging) and maintenance bundles (the operator's `--out`).

## H. Verdicts (decided 2026-10-30)

1. **The model:** per-rung lifespans, enforced by two entry points with the hand-off of §C —
   **decided: yes.** Moves into `docs/architecture_design.md` §8.
2. **Session memory, context and knowledge, ends with its session** when expiry has the store
   (§C.2, §C.3) — **decided: yes.**
3. **`Session.end()`** stays thread-only, as 4.10 documents. Forgetting on an explicit end would
   be an opt-in `end(forget=True)` — **decided: yes, add it with §E step 3.**
4. **The status lifecycle in `episodes/status`, one writer per operational key, and expiry's
   recovery sweep, built first** (§A, §C.1) — **decided: yes.**
5. **The build order and releases** (§E) — **decided: yes;** steps 3 and 4 and the releases revised
   2026-10-31.
6. **The forget record** (§C.2): cleanup across the two stores is recorded first, retried by
   whoever next takes the thread's lock, and completed before a session id is reused; a
   returning user finds the session ended — **decided: yes.**
7. **Consolidate only ended episodes**, with the namespace's size in the marker (§C.3) —
   **decided: yes;** size replaced the row count 2026-10-31, to avoid reading every transcript.
8. **Concurrency boundaries** (§B): one process per checkpointer, one maintainer per store —
   **decided: yes.**
