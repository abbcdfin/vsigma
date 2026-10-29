# Proposal: Thread Retention — Bounding What the Checkpointer Keeps

**Status:** **GRADUATED** (Phase 4.10 completed 2026-10-26). §B.1 was amended when the board was written.
**Prompted by:** the SQLite checkpointer spike (2026-10-26), run while preparing the 4.8/4.9
consumer notice. Since Phase 4.8 a session is a LangGraph thread, and its storage grows
quadratically.
**Answers:** [episode-retention](./episode-retention.md) §F.4: checkpoint retention gets its own
proposal. Episode-retention covers `MemoryStore`; this proposal covers the checkpointer.
**Builds on:** [langgraph-messages](./langgraph-messages.md) §F, which made removing messages
possible (`RemoveMessage`) and left using it to a later proposal.

## A. Problem

1. **Every checkpoint holds the whole conversation.** A checkpoint is a snapshot of all state,
   not a diff: nodes return only the messages they add, but the checkpointer stores the merged
   list. Under `durability="exit"` that is one full copy per turn. Over ten turns on
   `SqliteSaver` the latest checkpoint grew from 2.4 KB to 12.7 KB, 75.8 KB in total (§H.5).
   At that message size a session reaches 5.9 MB at 100 turns and about 574 MB at 1,000.
2. **Only the latest checkpoint is needed to continue.** With every older one deleted, a
   session reloads all its messages and a paused one still resumes (§H.6). Older checkpoints
   serve only time travel and earlier runs' `Run.snapshot` (`run.py:231-241`).
3. **No checkpointer bounds this itself** (§H.4). Only `RedisSaver` implements `prune`.
   `"exit"`, LangGraph's replacement for shallow savers and already the templates' default,
   still keeps one checkpoint per turn. `delete_thread` deletes the whole thread.
4. **The latest checkpoint keeps growing too.** The sliding window is view-only (Phase 4.8):
   the model never sees older messages again, but the thread keeps them.
5. **Nothing ever ends a thread.** Nothing calls `delete_thread`, neither for a session nor for
   a one-shot run, which gets a thread of its own (`thread_id = run_id`, `run.py:204`).

Both consumers are affected (§C).

## B. Design

Three parts, all opt-in. Default behaviour is unchanged.

### B.1 Keep the latest checkpoint

- `keep_latest(graph, thread_id)` deletes old checkpoints and their pending writes, and keeps
  the latest checkpoint's pending writes, which hold a paused run's interrupt.
  - **Not paused:** only the main graph's latest checkpoint stays. Every subgraph namespace is
    deleted.
  - **Paused:** the latest checkpoint stays in every namespace, so a paused subgraph survives.
  - It takes the compiled graph, not the checkpointer, because the graph supplies all three
    things it needs: the checkpointer, its channels (for the `DeltaChannel` check) and
    whether the thread is paused.
  - *Amended 2026-10-26, when the board was written.* The first version kept the latest
    checkpoint per namespace in every case. But every subgraph run gets a namespace of its own,
    and a finished one keeps its checkpoints (§H.10), so that version would have kept one per
    finished subgraph forever.
- Where the checkpointer implements `prune` (Redis), it delegates to that. Redis keeps the
  latest checkpoint per namespace, so finished subgraphs that paused keep one checkpoint each
  there. vsigma implements it for `InMemorySaver` and `SqliteSaver`, whose private storage
  layouts tests pin. `InMemorySaver` keeps channel values in separate blobs, so its version must
  delete the blobs the kept checkpoints no longer reference. Any other checkpointer is
  unsupported.
- `Agent(checkpoints="latest")`, and the same parameter on the templates, run it after every
  turn, paused or not. It runs after the run-end hook (`run.py:183-184`, `314`), which reads
  only the latest checkpoint and capture rows, both kept. The default, `"all"`, keeps today's
  behaviour.
- With `"latest"`, `Agent` checks at construction that the checkpointer is supported and that
  the graph has no `DeltaChannel` (beta, which replays the older writes keep-latest deletes).
  Anything else, including a graph with no checkpointer, raises at startup, not after the
  first turn.

### B.2 Keep a window of messages

- When a turn ends `done`, a node returns `RemoveMessage(id=…)` for every message outside the
  window (4.9's `add_messages`). It never runs on a paused turn: the question waiting for an
  answer must survive.
- The window is `SlidingWindowContext`'s rule: the last N messages, never starting with a tool
  result (`context.py:51-62`). It is extracted into one function used by both, so the stored
  thread and the model's view agree.
- It is a node, not a `ContextManager`, because assembly stays view-only (Phase 4.8). Inside
  the turn it adds no checkpoint under `"exit"`; `update_state` after the turn would add one per
  turn (§H.7).
- Templates take `retain_messages: int | None = None` and route `done` through the node. The
  routers already take `done=` (`agents/__init__.py:157`). Raw graphs add the node themselves.
- A `retain_messages` smaller than the view window raises at construction, because the model
  would silently see less than its window.

### B.3 End a thread: `Session.end()` and `Run.end()`

- `Session.end()` calls `checkpointer.delete_thread` on the session's thread.
  - Which thread holds a session is vsigma's mapping (`sessions.py:44-45`, `run.py:204`).
    Callers never repeat it, so a later change, such as session-admission's, cannot make them
    delete the wrong thread.
  - A later `send` on the same id starts an empty thread.
- `Run.end()` does the same for a run outside a session. On a session's run it raises and
  points to `Session.end()`, because that thread is the whole session.
  - A run's `result`, `status` and `state` read the checkpoint, so the app reads them first:
    `answer = run.result; run.end()`.
  - That is also why vsigma cannot end one-shot runs itself.
- Both refuse a paused thread unless `abandon=True`, as `send` does.
- Both delete only the thread. vsigma memory stays: capture rows, `episodes/index`, the
  episode summary and the extracted knowledge. Extraction's output is meant to outlive the run;
  the rest is episode-retention's concern.
- When to end is the app's decision, because vsigma cannot know when a conversation is over.

### B.4 What each policy costs

| | B.1 latest checkpoint | B.2 message window |
|---|---|---|
| Continue; resume a pause | yes | yes |
| `Session.dialog`, `Run.state["messages"]` | complete | window only |
| Earlier runs' `Run.snapshot`, `state`, `result` | raise `RuntimeError` (`run.py:241`) | unaffected |
| Time travel to earlier turns | lost | kept |
| Earlier turns' tags, with `memory=None` | lost (with memory on, `episodes/index` keeps them) | kept |
| Removed messages, with `memory=None` | — | only in older checkpoints, so gone with B.1 |
| Consolidation, with memory on | unaffected: it reads capture rows | unaffected |
| Storage per session | one checkpoint, linear in session length | each checkpoint bounded; total linear in turns |

With both, a session's storage stays constant.

## C. Consumers

- **A web app** (one agent per session, behind a websocket):
  - It first needs one checkpointer shared by its sessions, where each session built its own
    `MemorySaver`. The checkpointer must be persistent to survive reconnects and restarts:
    `SqliteSaver(sqlite3.connect(path, check_same_thread=False))` (§H.2). That belongs in the
    4.8/4.9 notice, not here.
  - B.1 fits: nothing reads earlier runs.
  - B.2 does not fit as it stands: the app reads every user turn from `Session.dialog`.
  - Closing a session is "release, not delete", so an idle-expiry policy would call
    `Session.end()`.
- **bumblebee:**
  - A daemon on the default `MemorySaver` (`core/agent.py:361`), so every session's
    checkpoints stay in RAM. B.1 bounds that.
  - B.2 fits. It already windows the view (`core/agent.py:351-358`), keeps transcripts in
    `SQLiteStore` capture rows, and reads no messages.

## D. Build order

B.1 first: it is the larger saving and needs no graph change.

1. `keep_latest` for `InMemorySaver`, and delegation to `prune`. Tests:
   - a paused session survives and resumes;
   - a paused subgraph's namespace survives, and is deleted once the thread is no longer
     paused;
   - an earlier run's `Run.snapshot` raises.
2. `keep_latest` for `SqliteSaver`, with `langgraph-checkpoint-sqlite` as a dev dependency
   (§G.6).
3. `checkpoints="latest"` on `Agent` and the templates. Tests: construction raises for an
   unsupported or missing checkpointer, and for a `DeltaChannel`.
4. `Session.end()` and `Run.end()`. Tests:
   - the thread is gone, and a later `send` starts empty;
   - a paused thread refuses without `abandon=True`;
   - memory rows survive;
   - `Run.end()` on a session's run raises.
5. B.2: the shared window function, the node, `retain_messages`, and its construction check.
6. Docs: `upgrading.md`, `getting_started.md`, `architecture_design.md` §4.

## E. Alternatives considered

- **`durability="exit"` alone.** It is the templates' default and still keeps one checkpoint
  per turn (§H.5).
- **A checkpointer that bounds itself.** Options are a shallow saver (Postgres's is
  deprecated, Redis has one, SQLite and in-memory have none), Redis `prune`, or Redis or
  MongoDB TTL. Fine for an app on Redis. TTL expires whole threads by age, which is B.3's job.
- **Leave it to the app**, with SQL like §H.6's. Each consumer would re-derive the
  per-namespace and pending-write handling against a private schema.
- **`DeltaChannel`** (beta) stores updates instead of the full list, and would replace B.1 for
  `messages`. But its format may change, and under `"exit"` the writes it replays were empty
  (§H.8). Worth a spike, not adoption.
- **One-shot work without a checkpointer**, instead of `Run.end()`. Every `Run` property
  raises (§H.9), the templates cannot opt out (`checkpointer=None` means `MemorySaver()`,
  `agents/__init__.py:84, 120, 163, 199`), a pause could not resume, and the run-end hook reads
  `run.state` (`run.py:216-229`).
- **Summarise, then remove** (compaction): a separate proposal, reusing B.2's removal.

## F. Non-goals

- Compaction.
- Retention of vsigma memory, which is episode-retention's.
- Token-based windows (token-budget-context).
- Choosing the app's checkpointer. vsigma constructs none beyond the templates' default
  (thread-native-runtime §B.7).

## G. Verdicts (decided 2026-10-26)

1. **Ship `keep_latest` in vsigma** — **decided: yes**, rather than leave it to the app (E).
   The cost is tracking two private layouts, pinned by tests.
2. **Run it automatically** — **decided: opt-in**, `checkpoints="latest"`, default `"all"`.
   Rejected: on by default, which breaks earlier `Run` objects and time travel without the app
   asking.
3. **Earlier `Run` objects after B.1** — **decided: they raise**, and this is documented.
   Rejected: caching each run's final state, a second copy of the thread.
4. **B.2 now** — **decided: yes, opt-in.** B.1 alone leaves the latest checkpoint growing.
5. **`Session.end()` and `Run.end()`** — **decided: yes** (B.3). Rejected: documenting
   `delete_thread(session_id)`, which makes callers repeat the mapping. For one-shot runs,
   also rejected: running without a checkpointer (E).
6. **`langgraph-checkpoint-sqlite` as a dev dependency** — **decided: yes, locked through the
   Tsinghua mirror declared as the project's default index.** PyPI is unreachable from the
   development machine. `uv.lock` then records the mirror's URL for every package, with the
   versions unchanged. Rejected: skipping the SQLite tests when the package is absent, which
   leaves the SQLite path unchecked by the gate; and waiting for PyPI access.
7. **Consumer notice** — **decided: hold the 4.8/4.9 notice until 4.10 lands**, so one notice
   covers all three phases.

## H. Evidence (LangGraph 1.2.6, langgraph-checkpoint 4.1.1, langchain-core 1.4.8; scratch scripts, one process per phase; 2026-10-26)

1. **Install.** `langgraph-checkpoint-sqlite` 3.1.1 resolves against vsigma's locked versions,
   adding `aiosqlite` 0.22.1 and `sqlite-vec` 0.1.9. So do `-postgres` 3.1.2, `-redis` 0.5.2 and
   `-mongodb` 0.5.1.
2. **Connection and threads.**
   - `SqliteSaver(sqlite3.connect(path))` fails on the first run with `ProgrammingError: SQLite
     objects created in a thread can only be used in that same thread`. LangGraph writes
     checkpoints from a worker thread.
   - `check_same_thread=False` fixes it; `SqliteSaver.from_conn_string` sets it. The saver adds
     its own lock and turns on WAL.
   - One shared saver, four threads, five turns each: no errors. With `check_same_thread=True`,
     every thread fails.
3. **Persistence across processes.** Process 1 ran two turns, 10 messages, and paused a guarded
   session at escalation. Process 2, on the same file:
   - found the same messages with the same ids, `Session.dialog`, `state["system"]`, and each
     turn's `run_id`, `task_id` and `user_id`;
   - ran a third turn: 15 messages, 15 unique ids. The model's prompt carried the earlier tool
     calls, an empty call id included;
   - found the guarded session paused with its interrupt payload. `send` raised
     `SessionPaused`, and `resume` ended `done`.

   There was one checkpoint per turn.
4. **Retention methods.** Each class was checked for overriding `BaseCheckpointSaver`; on
   `SqliteSaver` the methods were also called. No class implements `delete_for_runs` or
   `copy_thread`.

   | Checkpointer | `prune` | `delete_thread` | TTL |
   |---|---|---|---|
   | `InMemorySaver` | no | yes | no |
   | `SqliteSaver` | no (`NotImplementedError` when called) | yes | no |
   | `PostgresSaver` | no | yes | no |
   | `ShallowPostgresSaver` | keeps only the latest; deprecated for "PostgresSaver … with `durability='exit'`" | no | no |
   | `RedisSaver` | yes: `keep_latest`, or `keep_last=N`, per namespace | yes | yes |
   | `ShallowRedisSaver` | keeps only the latest | yes | yes |
   | `MongoDBSaver` | no | yes | yes |
5. **Growth.** Ten `"exit"` turns of five short messages on `SqliteSaver`. The latest
   checkpoint was 2,433, 3,582, 4,726 … 12,731 B, 75,848 B in all. The writes table was empty.
6. **Keep-latest by SQL**, deleting the other checkpoints and their writes:
   - 10 checkpoints became 1. A new process reloaded all 50 messages, and the next turn ended
     `done`.
   - On a paused session, working per namespace and keeping the latest checkpoint's writes:
     2 checkpoints and 2 writes became 1 and 1. A new process found it paused, and `resume`
     ended `done`.
   - `delete_thread` removed the thread's rows, and `Session.dialog` returned `[]`.
7. **Message window.** Ten turns, each followed by `update_state` with `RemoveMessage` for
   everything outside the last 10 messages. The latest checkpoint was 2,440 B, then 3,589 B,
   then 3,538–3,544 B for every later turn. `Session.dialog` held turns 8 and 9. `update_state`
   added a checkpoint per turn, for 18 in all.
8. **`DeltaChannel`** is beta in LangGraph 1.2.6. It rebuilds state by replaying older writes,
   and `SqliteSaver` supports it. Under `"exit"` the writes table was empty (5).
9. **No checkpointer.** A graph compiled without one, run through `Agent`, executes. Then
   `run.status`, `result`, `state` and `interrupted` each raise
   `ValueError: No checkpointer set`.
10. **Subgraph namespaces** (`InMemorySaver`). Three turns through a compiled subgraph node
    left three subgraph namespaces, `sub_node:<task id>`, one per run:
    - empty under `"exit"`;
    - with 3 checkpoints each under the default durability.

    A subgraph that paused held 1 checkpoint and a pending write in its namespace, and still
    held the checkpoint after resuming. `InMemorySaver` stores channel values in `blobs`,
    keyed by thread, namespace, channel and version: three turns left three `messages` blobs,
    one full list per turn.
