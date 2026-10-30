# Proposal: Scope Lifespans — How Long Each Rung Keeps What It Holds

**Status:** **OPEN** (DRAFT revision 2, 2026-10-27; Decision Check Point 2026-10-30).
An umbrella: it holds the model and the contract between its components, and builds nothing
itself. Every §H verdict is accepted (2026-10-30); §C.1 is built in Phase 4.12 (`v0.2.3`),
while §C.2–§C.3 wait for the `BaseStore` adapter.
Revision 2 answers an audit that found check-then-act races (2026-10-27) and its
follow-up (2026-10-29): it adds the concurrency rule (§B), the full status lifecycle, one writer
per record, and recovery of episodes whose process died (§A, §C.1), the retried forget record
and its three cases (§C.2), and consolidating only ended episodes (§C.3).
**Implements:** architecture §8, "Axis 1 — Scope: namespace + visibility + lifespan". Namespace
and visibility are built; lifespan never was, and nothing in memory is ever deleted.
**Components:** [thread-concurrency](./thread-concurrency.md) (the per-thread lock everything
else relies on), [thread-expiry](./thread-expiry.md) (the session rung and one-shot runs),
[episode-retention](./episode-retention.md) (the episode rung's transcripts, in the memory
pass), and a memory store that can delete (§D).

## A. The model

Memory spans threads. It is organized by the scope ladder (architecture §5), not by thread:

```
global ─ agent ─ persona ─ session ─ episode ─ step
```

A thread is where LangGraph keeps one rung's conversation: a session's (`thread_id =
session_id`) or a one-shot run's (`thread_id = run_id`). Each rung's lifespan:

| Rung | Holds | Ends when | Enforced by |
|---|---|---|---|
| episode | its transcript (capture rows); a one-shot run's thread | transcript: once consolidated, by the app's retention limits. Thread: `Run.end()`, or the app's `run_ttl` | memory pass; thread expiry |
| session | its thread (the conversation); session summaries; session knowledge, the default home of reflexive knowledge (`consolidation.py:51-72`) | `Session.end()`, or the app's session limits (idle TTL, keep the last N per user) | thread expiry |
| persona, agent, global | knowledge | not by age. Maintenance supersedes it; deleting conclusions is the tombstones design (maintenance-pass §D) | — |
| step | nothing of its own; steps are rows of the episode transcript | with the episode | — |

Outside the ladder, operational records keep the lifecycle, one writer per key so that no
record is read, modified and written by two parties (§C):

| Namespace | Holds | Written by | Lifespan |
|---|---|---|---|
| `episodes/index` | ownership, write-once (Phase 4.7) | the runtime, at run start | forever: a pruned `run_id` is never reused |
| `episodes/status` | the episode's status (§C.1) | the runtime and thread expiry's sweep, under the thread's lock | with the index entry |
| `episodes/tombstone` | a pruned episode's disposition and content hash, written before its rows are deleted (episode-retention §B.5) | the memory pass | forever |
| `maintenance/state` | `deep:<run_id>` markers, with the transcript's row count | the memory pass | until the episode is pruned |
| `sessions/forget` | intent records for unfinished session cleanup (§C.2) | thread expiry, `end(forget=True)` | until the cleanup completes |
| `sessions/index` (session-admission, not built) | registry entry | — | with its session |

## B. The contract

The app declares limits once, as data, and calls each entry point repeatedly. vsigma decides
what is due, enforces the safety rules, and deletes. There are two entry points, because the
work differs:

| | Thread expiry | Memory pass |
|---|---|---|
| Work | end idle sessions and forgotten runs | consolidate transcripts into knowledge, then prune them |
| Cost | a scan and some deletes | LLM calls, batched |
| Cadence | minutes | daily |
| Runs | in the app's process | outside it, by cron (maintenance-pass §B) or a daemon's timer |
| Needed by | any app with persistent threads | apps with memory |

```python
expiry = ThreadExpiry(checkpointer, ExpiryPolicy(idle_ttl=timedelta(days=7), keep_last=50),
                      store=store)                       # store optional
expiry.run()                                             # every few minutes, in the app
```

```
0 3 * * *  vsigma memory maintain --db /srv/agent.db --source persona:alice --scope persona:alice --keep-last 3 --max-age 30
```

Per turn, unchanged: `checkpoints="latest"` and `retain_messages` (4.10).

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
   - An episode without a status record predates this and is treated as legacy (episode-
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
     this process, over the checkpointer expiry was given. On today's stores it reads
     `episodes/status` whole; the `BaseStore` adapter's filtered search narrows that to
     unfinished episodes (§D). An app with memory and no expiry leaves crashed episodes
     `running`, and the memory pass never consolidates them: that is its choice.
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
   memory pass selects only episodes with a final status (legacy ones as today), so a running or
   paused episode is never read half-written. Its `deep:` marker records the transcript's row
   count, and an episode counts as consolidated only while the two match. That one test decides
   selection, pruning, and the protection of prior context (episode-retention §B.3). Durable
   learning comes from the deep pass over those transcripts. Session knowledge is the reflexive
   round's provisional output (confidence 0.3, unreviewed), so deleting it with its session loses
   nothing the deep pass cannot rebuild, provided the transcripts are kept until consolidated,
   which the memory pass guarantees.

## D. Storage

- **Checkpointer:** unchanged. Thread expiry supports `InMemorySaver` and `SqliteSaver`; others
  raise.
- **Memory store:** forgetting a session and pruning transcripts both delete, and `MemoryStore`
  cannot. The foundation is a LangGraph `BaseStore` behind the `MemoryStore` protocol
  (thread-native-runtime §G.7), still to be proposed. It brings `delete`, TTL,
  `list_namespaces` and more backends. A probe of `SqliteStore` (2026-10-27) found what the
  adapter must handle:
  - namespace labels cannot contain `.`, and user ids can (`u.a@x.com`);
  - `created_at` has one-second resolution, and rewriting an item moves it to the end, so
    `query`'s first-write order needs the adapter's own sequence. It must come from a
    time-ordered id assigned at write, not from reading the current maximum, which two writers
    could read at once;
  - `search` returns 10 items unless asked for more.
- The adapter's contract (its own proposal) also covers migrating existing `SQLiteStore` data,
  listing namespaces, and deleting.
- Until the store can delete, thread expiry works on the checkpointer alone, and still records
  `abandoned`, which is a write.

## E. Build order

1. The per-thread lock, `SessionBusy` and the pause id
   ([thread-concurrency](./thread-concurrency.md), accepted 2026-10-29 as Phase 4.11), a bug
   fix for released behaviour: `v0.2.1`, with the `mcp<2` cap. Then thread expiry on the
   checkpointer, and the status records with expiry's recovery sweep (§C.1), which need only
   writes: `v0.2.3`, after `v0.2.2`, the first public release.
2. The `BaseStore` adapter.
3. Thread expiry's store side: forgetting session memory (§C.2).
4. The memory pass with retention (episode-retention).

Each step is additive, so each fits a `v0.2.x` release.

## F. Alternatives considered

- **One `maintain()` for everything.** It would bring an LLM dependency to apps that need only
  expiry, let a failed consolidation hold up expiry, and pull LLM work into the app's process
  against maintenance-pass §B. Two jobs do not need a scheduler.
- **Policy functions the app writes.** Replaced by declarative limits. A custom function stays
  an escape hatch to add when a consumer needs a rule the limits cannot express.
- **Store TTL as the mechanism.** It deletes regardless of consolidation and cannot reach the
  checkpointer. The adapter may use it inside the session rung later.

## G. Non-goals

- Agent-driven memory: the LLM choosing what to remember or forget.
- Deleting knowledge in persona, agent or global scope (maintenance-pass §D).
- Scheduling: the app or cron owns time.
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
5. **The build order and releases** (§E) — **decided: yes.**
6. **The forget record** (§C.2): cleanup across the two stores is recorded first, retried by
   whoever next takes the thread's lock, and completed before a session id is reused; a
   returning user finds the session ended — **decided: yes.**
7. **Consolidate only ended episodes**, with the transcript's row count in the marker (§C.3) —
   **decided: yes.**
8. **Concurrency boundaries** (§B): one process per checkpointer, one maintainer per store —
   **decided: yes.**
