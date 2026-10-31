# Proposal: Thread Expiry — Ending the Sessions and Runs Nobody Ends

**Status:** **GRADUATED (Phase 4.12)** 2026-10-30, released in `v0.2.3` (2026-10-31; §B.3.2 forgetting session memory waits for the `BaseStore` adapter). Revision 2
replaced policy functions with a declarative policy and added the store side. Revision 3 answers
an audit (2026-10-27) and its follow-up (2026-10-29): deletion now holds the thread's lock, so it
cannot delete during a running turn, and the store side sweeps unfinished episodes, including
first turns that died before writing a checkpoint, and completes forget records.
**Component of:** [scope-lifespans](./scope-lifespans.md), which holds the model: this proposal
enforces the session rung's lifespan and a one-shot run's thread.
**Prompted by:** a web app's migration decisions (2026-10-27): one app-wide persistent
`SqliteSaver`, and no thread deletion until vsigma can expire idle threads.
**Builds on:** [thread-retention](./thread-retention.md) §B.3 (`Session.end()`/`Run.end()`),
and reverses its §C call that idle expiry is the app's.

## A. Problem

1. **A persistent checkpointer keeps every thread forever.** Under `checkpoints="latest"` each
   thread is one checkpoint, so a thread is bounded but their number is not: every agent session
   a web app ever opens, and every one-shot run whose `Run.end()` was never called
   (`thread_id = run_id`).
2. **`end()` needs the app to know a thread is finished.** In a "release, not delete" app nobody
   does: a crashed browser tab never sends a close, which is why such an app wants a sliding
   TTL.
3. **An app cannot expire threads well on its own.** LangGraph's public `checkpointer.list(None)`
   finds them (§H.1), but:
   - it deserializes every thread's full state to read one timestamp;
   - a user may come back between the scan and the delete, and deleting then loses the turn they
     just took. Preventing that takes a transaction specific to each checkpointer, as
     `keep_latest` does (thread-retention §B.1).
4. **A session's memory outlives its thread.** `Session.end()` deletes only the thread (4.10).
   The session's summaries stay in `session:<id>/context`, and its distilled knowledge in
   `session:<id>/knowledge`: by default that is where all reflexive knowledge goes
   (`_knowledge_scope`, `consolidation.py:51-72`). `RecallContext` recalls that knowledge if the
   same session id comes back, and ids derived from the user and a client-chosen name
   (`user:session`) do.

## B. Design

The app declares limits; vsigma reads, decides and deletes; the app calls it repeatedly.

### B.1 The policy

```python
@dataclass(frozen=True)
class ExpiryPolicy:
    idle_ttl: timedelta | None = None         # a session idle longer than this expires
    keep_last: int | None = None              # each user keeps their N most recently active sessions
    paused_idle_ttl: timedelta | None = None  # paused threads; default: idle_ttl
    run_ttl: timedelta | None = None          # one-shot runs nobody ended; default: idle_ttl
```

- A thread is a **session** when its latest checkpoint's metadata has a `session_id`, and a
  **run** otherwise (§H.5). Runs follow `run_ttl`, so a one-shot task can go sooner than a
  conversation.
- A thread expires when it breaks any limit that is set: idle too long, or outside its user's
  `keep_last` newest sessions.
- `keep_last` counts sessions per `user_id`. Sessions without one form a single group.
- **Paused threads follow `paused_idle_ttl` alone** and do not count toward `keep_last`, so a
  newer session never pushes out a question someone may still answer.
- An empty policy raises at construction, as do non-positive durations and `keep_last < 1`.

### B.2 The entry point

```python
expiry = ThreadExpiry(checkpointer, policy, store=None)
expiry.run(*, now=None, dry_run=False) -> ExpiryReport   # what expired, and why
expiry.threads(*, now=None) -> Iterator[ThreadInfo]      # read only: what vsigma sees

@dataclass(frozen=True)
class ThreadInfo:
    thread_id: str
    kind: Literal["session", "run"]
    user_id: str | None
    run_id: str             # the thread's latest run
    last_active: datetime   # UTC
    idle: timedelta
    paused: bool
```

- **Reading** uses each thread's latest checkpoint in the root namespace (`""`), without
  deserializing state:
  - `last_active` is decoded from the checkpoint id, a time-ordered UUID (uuid6). It matches the
    checkpoint's `ts` within milliseconds (§H.3); a test pins that.
  - `paused` is an `__interrupt__` pending write on that checkpoint; a pause inside a subgraph
    writes one there too (§H.2).
  - `kind`, `user_id` and `run_id` come from the checkpoint's metadata, which vsigma writes on
    every run (§H.5).
- **Deleting holds the thread's lock** ([thread-concurrency](./thread-concurrency.md)). Expiry
  tries the lock without waiting and skips a busy thread. A running turn is invisible in storage
  under `durability="exit"` (§H.6), so the lock, not the checkpoint, is what keeps expiry from
  deleting a thread mid-turn. Holding it, expiry re-reads the thread's latest checkpoint and
  deletes only if it is still the one scanned: a turn that finished between the scan and the
  lock has moved it. On `SqliteSaver` that read and the delete share one transaction under
  `cp.lock` (`BEGIN IMMEDIATE`); on `InMemorySaver` they run under the thread's lock alone. The
  whole thread goes, subgraph namespaces included: the same as `Session.end(abandon=True)`.
- **Boundary:** one process per checkpointer, as thread-concurrency documents, and with a store,
  that process's agents are the only ones writing its episodes (scope-lifespans §B). Expiry
  must be given the checkpointer object the agents use: the lock registry is keyed by it.
- **Supported checkpointers** are those `keep_latest` supports, `InMemorySaver` and `SqliteSaver`.
  Others raise `NotImplementedError` at construction.
- **It takes the checkpointer, not an `Agent`.** An app may build an `Agent` per session over
  one shared checkpointer.
- **It never runs by itself.** The app calls `run()` on its own timer; it is cheap when nothing
  has expired.

### B.3 The store side

With `store=`, expiry also does the session rung's part of the hand-off (scope-lifespans §C):

1. **It sweeps unfinished episodes** after its deletes, by scope-lifespans §C.1's recovery
   rule: under each episode's thread lock, a `running` episode whose lock is free, and an
   `escalated` one whose thread no longer holds its pause, are recorded `abandoned`. That
   covers the paused runs of threads it just deleted, runs whose process died, first turns that
   died before writing any checkpoint (§H.7), and runs that raised before `failed` could be
   recorded. It reads `episodes/status` and writes, so it works with today's stores.
2. **It forgets an expired session's memory**, `session:<id>/context` and
   `session:<id>/knowledge`, through the forget record of scope-lifespans §C.2: the record is
   written under the thread's lock before the thread is deleted, and each `run()` first
   completes the records an earlier run left. Deleting memory needs a store that can delete,
   which arrives with the `BaseStore` adapter (scope-lifespans §D). Until then the store side
   only sweeps.

The episodes themselves outlive their session: their transcripts stay until the memory pass
consolidates and prunes them (scope-lifespans §C.3).

### B.4 Use

```python
expiry = ThreadExpiry(
    checkpointer,
    ExpiryPolicy(idle_ttl=timedelta(days=7), keep_last=50),
    store=store,
)
# at startup, then every few minutes from the app's own background task:
report = await run_in_threadpool(expiry.run)
```

## C. Consumers

- **A web app:** `ExpiryPolicy(idle_ttl=timedelta(days=7), keep_last=50)` and a timer in the
  app, over the one checkpointer it builds. Its sessions carry `user_id`, so `keep_last` counts
  per user. Its memory store is per connection and nothing reads it, so it passes no `store`. A
  session reopened after expiry starts empty, which its TTL intends.
- **bumblebee:** a daemon on `MemorySaver`, whose threads otherwise live as long as the process.
  It passes its `SQLiteStore`, so episodes of expired threads, and episodes a crash left
  `running`, are recorded `abandoned` and become consolidatable; session memory goes too once
  the store can delete.

## D. Build order

0. Prerequisite: the per-thread lock ([thread-concurrency](./thread-concurrency.md)).
1. `ThreadExpiry.threads()` on `InMemorySaver`, with the uuid6-vs-`ts` test and the metadata
   fields.
2. The same on `SqliteSaver`, both savers parametrized.
3. `ExpiryPolicy`, its validation, and `run()`. Tests:
   - each limit alone, and two at once;
   - `keep_last` per user, and the shared group without a user;
   - a paused thread follows `paused_idle_ttl` and is exempt from `keep_last`;
   - a run follows `run_ttl`;
   - a thread with a turn in progress is skipped, and its turn completes intact;
   - a thread resumed after the scan is skipped;
   - another thread is untouched;
   - `dry_run` deletes nothing;
   - an unsupported checkpointer raises.
4. The store side's sweep (§B.3.1), with the runtime's status lifecycle (scope-lifespans §C.1).
   Tests:
   - a first turn that died before its first checkpoint, left as its `running` status and index
     entry with no lock held, is recorded `abandoned` by the next `run()`;
   - the episode of a turn in progress is left `running`;
   - the paused run of a thread expiry deletes is recorded `abandoned` in the same `run()`;
   - an `escalated` run whose session has since run another turn is recorded `abandoned`; one
     still holding its pause is left alone.
5. Docs: `upgrading.md` (an additive section), `getting_started.md` sessions section.
6. Release `v0.2.3`.
7. Later, after the `BaseStore` adapter: forgetting session memory through the forget record
   (§B.3.2), with a test for each of scope-lifespans §C.2's three cases:
   - a failure after the thread delete is completed by the next `run()`, which deletes the
     memory and the record;
   - a failure before the thread delete: a turn on that session completes the record first, and
     starts with no thread and no old memory;
   - a record whose thread has moved on is dropped and deletes nothing;
   - and a session id reused before completion starts with no old memory.

## E. Alternatives considered

- **Leave it to the app** (thread-retention's call). Each app re-derives the per-checkpointer,
  race-free delete, and `list()` deserializes every thread on each sweep (§A.3).
- **Policy functions the app writes** (revision 1). Every app writes the common rules again.
  Declarative limits cover them; a custom function stays an escape hatch for later (§F).
- **vsigma schedules the sweep** (`Agent(thread_ttl=...)`). A library has no lifecycle to hang a
  timer on; several processes on one SQLite file would race; sweeping inside a turn slows an
  unrelated user.
- **One `maintain()` with the memory pass.** Rejected in maintenance §E: different cost,
  cadence and dependencies.
- **A last-activity index in vsigma memory.** A second source of truth, and memory is optional
  since 4.8.
- **Read `ts` through `list()`.** Public API, but loads every thread's state (§A.3).
- **Checkpointer-native TTL.** Redis and MongoDB have it, and suit apps on them. SQLite and
  in-memory have none.

## F. Non-goals

- The memory pass: consolidating and pruning transcripts ([episode-retention](./episode-retention.md)).
- `max_age`, a lifetime counted from creation. Under `checkpoints="latest"` the first checkpoint
  is gone, so the creation time is not known; vsigma would have to carry it in metadata.
- A custom policy function, until a consumer needs a rule the limits cannot express.
- Scheduling.
- Redis, MongoDB and Postgres: they raise; Redis and MongoDB have native TTL.
- Archiving a thread before deleting it.

## G. Verdicts (decided 2026-10-30)

1. **Ship expiry in vsigma**, reversing thread-retention §C's "leave it to the app" —
   **decided: yes** (§A.3, §E).
2. **The declarative policy** (§B.1): `idle_ttl`, `keep_last` per user, `paused_idle_ttl`,
   `run_ttl`, and no policy functions for now — **decided: yes.**
3. **Paused threads** follow their own TTL and are exempt from `keep_last` — **decided: yes.**
4. **`ThreadExpiry(checkpointer, policy, store=None)`** with `run()` and `threads()` —
   **decided: yes.**
5. **Last activity from the checkpoint id**, pinned by a test against `ts`, rather than
   deserializing `ts` — **decided: yes**; it is the third private detail vsigma pins, after
   `keep_latest`'s two layouts.
6. **Supported checkpointers:** `InMemorySaver` and `SqliteSaver`; others raise —
   **decided: yes.**
7. **The store side** (§B.3): the sweep of unfinished episodes from the first release; forget
   session memory through the forget record once the store can delete — **decided: yes.**
8. **Release:** `v0.2.3`, as Phase 4.12, after `v0.2.1` ships thread-concurrency alone (decided
   2026-10-29, thread-concurrency §F.4) and `v0.2.2` is the first public release —
   **decided: yes.**
9. **Deletion holds the thread's lock** and skips busy threads (§B.2), with one process per
   checkpointer as the boundary — **decided: yes.**

## H. Evidence (LangGraph 1.2.6, langgraph-checkpoint 4.1.1, langgraph-checkpoint-sqlite 3.1.1; 2026-10-27)

1. `checkpointer.list(None)` lists every thread, every namespace, on `InMemorySaver` and
   `SqliteSaver`: a finished session, a one-shot run, and a paused session. The latest root
   checkpoint's `ts` is ISO 8601 UTC with microseconds (`2026-10-27T07:39:28.752414+00:00`).
2. A session paused inside a subgraph: its root checkpoint has an `__interrupt__` pending write,
   as does the subgraph's, on both savers; `Session.paused` is `True`. A finished session has
   no pending writes.
3. The time decoded from each checkpoint id (uuid6: 100 ns ticks since 1582-10-15) differs from
   its `ts` by 0.02 to 2.4 ms, root and subgraph namespaces, both savers.
4. `keep_latest` (`retention.py`) already tracks both savers' private layouts, pinned by
   `tests/test_retention.py`.
5. The latest checkpoint's metadata carries `session_id`, `user_id` and `run_id` from
   `Agent.run` (`run.py:183-189`): `{"session_id": "s1", "user_id": "alice", …}` for a session;
   `session_id: None` for a one-shot run. `SqliteSaver` keeps metadata in its own column, so
   reading it does not load the checkpoint's state.
6. Under `durability="exit"`, a turn in progress writes no checkpoint: revision 2's guard (compare
   the latest checkpoint with the one scanned) saw no change and deleted a thread mid-turn,
   which the turn then rewrote when it ended (thread-concurrency §G.1; the audit reproduced the
   same).
7. 2026-10-29, `SqliteSaver` and `SQLiteStore`: during a session's first turn, held in its LLM
   call, `episodes/index` held one entry and `checkpointer.list(None)` no thread; after the turn,
   one thread. A crash in that window leaves an episode no thread scan finds (the audit's
   follow-up verified the same).
