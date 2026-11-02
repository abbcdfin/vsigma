# Upgrading vsigma

Migration notes for consumers adopting breaking or behavioral changes across vsigma releases.

**Releases.** What each version contains, and its known issues, is in
[`CHANGELOG.md`](../CHANGELOG.md); this guide says how to move your code. Sections are titled by
version and phase, newest first. `v0.1.0` (Phase 4.7) is the last release before the breaking
changes of `v0.2.0`: pin it until you migrate, then go straight to the latest `v0.2.x`. Releases
are tagged on `master`; a fix for an older line goes on its `release/X.Y` branch (`release/0.1`).

---

## `v0.3.1` (Phase 4.15) — Run Context: One Agent, Many Sessions

Phase 4.15 ([run-context](./proposals/run-context.md)) enables production services to maintain one long-lived `Agent` with one persistent checkpointer and store, while passing per-session objects (credentials, trace spans, request-scoped services) directly to turns via `run_context`. Compatible with `v0.3.0`.

### Added

- **`run_context=` parameter on `Agent.run` and `Agent.session`:**
  - Passes a Python object through `Session` and `Run` to LangGraph's runtime context (`graph.stream(context=...)`).
  - *Falsy object caveat:* LangGraph merges context using `other.context or self.context`, so falsy objects (such as `{}`, `[]`, `""`, `0`, or `False`) evaluate to `None`. The context object must be truthy; passing a falsy object causes calls to `run_context()` to raise `NoRunContext`.
  - Read inside tools, hand-built nodes, routers, and `ContextManager.assemble()` via `run_context()`.
- **`run_context() -> Any` and `NoRunContext(RuntimeError)`:**
  - `run_context()` returns the active run's context object.
  - Raises `NoRunContext` when called outside an active run, on an unmanaged thread (e.g. `threading.Thread` spawned inside a tool), or when the run was invoked without `run_context`.
  - In `react_node`, `NoRunContext` is re-raised immediately rather than caught and formatted as tool error text for the model, ensuring programming/configuration errors fail closed.
- **Fail-closed `has_context` metadata guard:**
  - Checkpoint metadata records boolean `has_context: True` (never the object itself).
  - Calling `Session.send()` or `Session.resume()` / `Run.resume()` on a thread that ran with context without supplying an object raises `NoRunContext` immediately before executing or altering paused interrupts.
  - Calling `Session.send(..., abandon=True)` on a guarded session without an object also raises `NoRunContext`, preserving the paused interrupt and leaving the prior episode's status as `"escalated"` (not `"abandoned"`).
  - Renewing a handle with `agent.session(session_id, run_context=renewed)` resumes the paused thread safely.

### Migrating an app that built one `Agent` per session

Earlier versions required building a new `Agent` per session if tools closed over per-session credentials or objects. That prevented using `agent.maintain()`, which requires that only one live `Agent` instance holds the persistent store in the process.

To migrate to the one-agent pattern:
1. **Instantiate one long-lived `Agent`:**
   Configure tools to read per-session objects from `run_context()` instead of closing over them at tool factory time:
   ```python
   # Before (one Agent per session):
   def make_query_tool(credential: str):
       def query(args):
           return db.query(args["id"], credential=credential)
       return tool(query)

   agent = make_react_agent(llm, [make_query_tool(user_cred)], checkpointer=saver, interceptor=Interceptor(memory=store))
   session = agent.session(session_id)

   # Now (one Agent across sessions):
   def query(args):
       services = run_context()
       return db.query(args["id"], credential=services.credential)

   agent = make_react_agent(llm, [tool(query)], checkpointer=saver, interceptor=Interceptor(memory=store))

   # Open session with run_context:
   session = agent.session(session_id, run_context=SessionServices(credential=user_cred))
   session.send("show my order")
   ```
2. **Retrying `SessionBusy` from `maintain`:**
   `agent.maintain()` briefly locks threads during checkpoint pruning. If concurrent turn requests arrive while a thread lock is held, `send()` raises `SessionBusy`. Catch `SessionBusy` and retry with a brief backoff (e.g. 50-100ms).
3. **Secret hygiene:**
   The `run_context` object is process-local and lost on restart. It is never serialized or written to checkpoint snapshots or store memory unless the application explicitly writes it to graph state or `memory.write()`.

---

## `v0.3.0` (Phases 4.13 & 4.14) — Store Adapter, Background Maintenance & Episode Retention

Adapts LangGraph's `SqliteStore` for session and long-term memory behind the `MemoryStore` and `MaintainableStore` protocols (Phase 4.13, [store-adapter](./proposals/store-adapter.md)), and adds unified background maintenance (`agent.maintain()`, CLI `vsigma memory maintain`), declarative episode retention (`EpisodeRetention`, `RetentionPolicy`), and memory consolidation pass with owner routing (`MemoryPass`, `MemoryPassReport`) (Phase 4.14, [maintenance](./proposals/maintenance.md), [episode-retention](./proposals/episode-retention.md)).

### Breaking changes

1. **`SQLiteStore` is renamed to `PersistentStore` (no compatibility alias):**
   - Update imports:
     ```python
     # Before (<= v0.2.3)
     from vsigma import SQLiteStore
     store = SQLiteStore("agent.db")

     # Now (v0.3.0)
     from vsigma import PersistentStore
     store = PersistentStore("agent.db")
     ```
2. **`run_maintenance` renamed to `MemoryPass.run()` (no compatibility alias):**
   - Update imports and usage:
     ```python
     # Before (<= v0.2.3)
     from vsigma import run_maintenance
     report = run_maintenance(store, llm, sources=sources, scope=scope, out_dir=out_dir)

     # Now (v0.3.0)
     from vsigma import MemoryPass
     pass_runner = MemoryPass(store, llm, sources=sources, scope=scope, constructor="my-model/deep", out_dir=out_dir)
     report = pass_runner.run()
     ```
3. **`MaintenanceReport` renamed to `MemoryPassReport`, `MaintenanceReport` now represents `Agent.maintain()`:**
   - The memory pass report is now `MemoryPassReport` (`report.records`, `report.episodes`, `report.bundles`, `report.unowned`, `report.busy`).
   - `MaintenanceReport` is now the report returned by `Agent.maintain()`, holding `expiry: ExpiryReport | None`, `memory: MemoryPassReport | None`, and `retention: RetentionReport | None`.
4. **`langgraph-checkpoint-sqlite` is now a required dependency:**
   - Installs `langgraph-checkpoint-sqlite>=3.1,<4`, bringing `sqlite-vec` and `aiosqlite`. Every install receives the same store.
5. **No automatic store migration; old store files fail loudly:**
   - Existing store databases are not migrated and their rows do not carry over.
   - Opening an existing SQLite file containing pre-v0.3.0 data in table `memory` raises `RuntimeError` immediately, leaving the file untouched.
   - Specify a new path for your store database when upgrading to `v0.3.0`. `v0.2.3`'s `vsigma memory export` can still read old files.

### Added

- **Unified background maintenance (`Agent.maintain()`):**
  - Runs all four maintenance jobs safely in one call: thread expiry sweep, memory pass with owner routing, old checkpoint pruning, and transcript retention.
  - Enforces the one-agent rule: `agent.maintain()` requires that the calling agent is the only live `Agent` instance holding its store in the process (counts `Agent` objects in the current process, not deployed services across machines).
  - Zero spend by default (`expiry=None`, `distiller=None` spend $0 on LLM). Prunes checkpoints and enforces 1 GiB transcript budget by default.
  - Knowledge lands with its owner: `user_id` episodes route to `persona(user_id)`, `session_id`-only episodes route to `session(session_id)`. Unowned episodes are skipped and counted in `report.memory.unowned`.
- **Declarative transcript retention (`EpisodeRetention` and `RetentionPolicy`):**
  - `RetentionPolicy(max_bytes=1_073_741_824, keep_last=None, max_age=None, prune_unconsolidated=False)`.
  - Prunes capture rows while preserving `episodes/index`, summary, and writing a record at key `<run_id>` in `episodes/tombstone` with `disposition="pruned"`.
  - Crash recovery completes interrupted prunes on the next pass.
- **`MaintainableStore` protocol:**
  - Adds `namespaces() -> list[str]`, `namespace_sizes() -> dict[str, int]`, and `delete(namespace: str, keys: list[str]) -> None`.
  - Implemented by both `PersistentStore` and `InMemoryStore`.
- **CLI `vsigma memory maintain`:**
  - Combines memory pass and episode retention from the CLI with flags `--source`, `--all-sources`, `--scope`, `--keep-last`, `--max-bytes`, `--max-age`, `--prune-unconsolidated`.

---

## `v0.2.3` (Phase 4.12) — Thread Expiry

Adds declarative checkpointer thread expiry (`ThreadExpiry`), race-free deletion under the thread's lock, and recovery of unfinished episodes (`episodes/status`). See [thread-expiry](./proposals/thread-expiry.md).

No code changes are required for existing consumers: existing applications continue to run unchanged. Agents configured with a memory store now write an `episodes/status` record per episode; episodes from earlier releases have no status record and are treated as legacy (ignored by the recovery sweep).

### Declarative thread expiry (`ThreadExpiry`)
- `ThreadExpiry(checkpointer, policy, store=None)` scans idle threads without deserializing checkpoint state blobs and race-freely deletes expired threads under each thread's lock.
- **Supported checkpointers:** `InMemorySaver` and `SqliteSaver` (others fail fast with `NotImplementedError`).
- **Policy (`ExpiryPolicy`):**
  - `idle_ttl`: Maximum idle time since the latest root checkpoint.
  - `keep_last`: Maximum active sessions kept per user (`user_id=None` forms one group). Limits only unpaused sessions; one-shot runs and paused threads are exempt.
  - `paused_idle_ttl`: Maximum idle time for paused sessions/runs (defaults to `idle_ttl`).
  - `run_ttl`: Maximum idle time for one-shot runs (defaults to `idle_ttl`).
- `expiry.threads(now=None)` yields `ThreadInfo` metadata without state deserialization.
- `expiry.run(*, now=None, dry_run=False) -> ExpiryReport`:
  - Evaluates policy, acquires the thread's lock, verifies checkpoint ID has not changed, and atomically deletes expired threads.
  - Returns `ExpiryReport(expired, skipped_busy, skipped_changed, abandoned_runs)`.
  - When `dry_run=True`, reports candidate expirations and recoverable episodes without deleting threads or writing status.

### Setup requirements
Thread expiry relies on the boundary defined in `docs/proposals/thread-expiry.md` §B.2:
- **The same checkpointer object:** Expiry must get the exact object the agents use, not a second `SqliteSaver` on the same file, because the lock registry recognises a checkpointer by object.
- **One process per checkpointer boundary:** The thread lock is in-process.
- **With `store=`, the same store:** Pass the same `MemoryStore` instance the agents write to.
- **It never runs on its own:** `ThreadExpiry` has no internal background timer; the host application calls `expiry.run()` from its own timer (e.g. periodically in a background task).

### Episode status lifecycle and recovery sweep
- Episode execution status is tracked under the thread's lock in `episodes/status`: `"running"`, `"escalated"`, `"done"`, `"failed"`, `"abandoned"`.
- Once an episode reaches a final status (`"done"`, `"failed"`, `"abandoned"`), it cannot be modified; attempts to overwrite log a warning and are ignored.
- Passing `store=` into `ThreadExpiry` opts in to the recovery sweep:
  - Deleting an expired paused thread marks its run `abandoned`.
  - Sweeps all unfinished episodes (`running` or `escalated`) in `episodes/status`:
    - A `running` episode whose thread lock is free (process died or crashed mid-run) is marked `abandoned`.
    - An `escalated` episode whose thread is gone, whose latest run is another, or which is no longer paused, is marked `abandoned`.

---

## `v0.2.2` — First Public Release

No code changes from `v0.2.1`, so nothing to migrate. vsigma is now on PyPI: depend on
`vsigma>=0.2.2,<0.3` instead of a git tag.

---

## `v0.2.1` (Phase 4.11) — Thread Concurrency

Fixes the check-then-act races across concurrent operations on the same thread and prevents a late
resume from answering a newer question (see [thread-concurrency](./proposals/thread-concurrency.md)).
Also caps `mcp>=1.28.1,<2` so fresh installs do not pull incompatible `mcp` 2.x releases.

### One operation per thread at a time (`SessionBusy`)
- Every operation that checks and changes a thread (`Agent.run`, `Session.send`, `Session.resume`, `Run.resume`, `Session.end`, `Run.end`, and `keep_latest`) marks `(checkpointer, thread_id)` busy for the entire operation (from the initial `SessionPaused` / active check through episode recording, execution, the run-end hook, and `"latest"` checkpoint pruning) and clears it on exit.
- The busy-thread registry is keyed by the checkpointer instance, so multiple `Agent` instances built over one shared checkpointer share the same per-thread lock state.
- **Fail fast when busy:** A second operation on a thread with an operation already in flight immediately raises `SessionBusy(thread_id)` (a `RuntimeError` subclass exposing `.thread_id`) rather than blocking. The caller decides whether and when to retry: an interactive app answers it the way it answers its own "busy" case, by asking the client to retry later.
- **Boundary:** One process per checkpointer. The lock is in-process; sharing one checkpointer database across multiple OS processes requires a database lease and is outside this boundary. Within the process, share one checkpointer object: two saver objects over the same database file do not see each other's busy threads.

### Naming the pause a resume answers (`interrupt_id` and `PauseChanged`)
- LangGraph's `Interrupt.id` (`str | None`) is exposed on `Session.interrupt_id`, `Run.interrupt_id`, and `SessionPaused.interrupt_id`, beside the existing `.interrupt` value.
- `Session.resume(value, *, interrupt_id: str | None = None)` and `Run.resume(feedback, *, interrupt_id: str | None = None)` accept an optional `interrupt_id`.
- Holding the thread's lock, `resume` compares `interrupt_id` against the currently pending pause's id:
  - If another pause is now pending, or no pause is pending (the run has already finished or moved on), it raises `PauseChanged` (a `RuntimeError` subclass exposing `.interrupt` and `.interrupt_id` of the pause now pending, or `None` when none is pending).
  - Without `interrupt_id`, `resume` behaves as before.
- **Rule for interactive apps:** Every app that shows a pause to a person should send `interrupt_id` out with the question and pass that id back into `resume(value, interrupt_id=...)` when the answer arrives. Do not look up `session.interrupt_id` at answer arrival time, as that reads whichever pause is pending then.
- **On `PauseChanged`:** do not retry the answer. Tell the person their answer was for a question that is no longer open, and show the one now pending (`.interrupt`, whose `.interrupt_id` goes back with the next answer), or nothing when `.interrupt_id` is `None`.

### Consumer migration notes
- **A web app** (agents behind a websocket, one app-wide checkpointer, questions shown to people): target `v0.2.1`, not `v0.2.0`. On top of the `v0.2.0` notes below:
  - **`busy` from another connection.** A reconnect while the old connection's turn still runs makes the new turn raise `SessionBusy`, instead of starting a second turn on the same session. If the app turns every exception from a turn into a generic error, catch `SessionBusy` first and send the same "busy, retry" reply as its own per-connection guard.
  - **The id out.** Send the pause's `interrupt_id` with the question, read from the paused run (`run.interrupt_id`) when the question is relayed.
  - **The id back.** Take the id from the message carrying the answer and pass it to `resume(answer, interrupt_id=...)`.
  - **A stale answer.** Answer `PauseChanged` with an error the front end shows, not retriable. vsigma raises it for a double submit after the run paused again; the newer question has already been relayed. An app that discards questions on reopen should raise it too when an answer names a pause it no longer holds (the run finished, or the app dropped the question), so that the answer never runs as a new turn. A message without an id still starts a new turn.
  - **The front end** must send the id back with the answer before the check takes effect. Until it does, answers behave as before, so the server side can ship first.
  - A trial migration of a production web app that follows these notes passes its offline suite on Python 3.13 and 3.11.
- **bumblebee:** if two messages for one session can arrive while a turn runs, the second now raises `SessionBusy`; queue it or retry it. If people answer its escalations, send `interrupt_id` out with the question and pass it back to `resume`.

---

## `v0.2.0` (Phase 4.10) — Thread Retention

Bounds what the checkpointer keeps across turns and provides mechanisms to end finished threads (see [thread-retention](./proposals/thread-retention.md)). All policies are opt-in; default behaviour is unchanged. vsigma now requires `langgraph>=1.2.4`.

### Keep the latest checkpoint (`checkpoints="latest"`)
- `Agent(checkpoints="latest")` and the four prebuilt templates (`make_react_agent`, `make_guarded_react_agent`, `make_plan_execute_agent`, `make_reflexion_agent`) accept `checkpoints: Literal["all", "latest"] = "all"`.
- Under `"latest"`, `keep_latest(graph, thread_id)` runs automatically after each turn (after the run-end consolidation hook).
  - When not paused: keeps only the root namespace's (`""`) latest checkpoint and pending writes; deletes all older checkpoints, writes, and finished subgraph namespaces.
  - When paused: keeps the latest checkpoint and pending writes in every namespace, preserving paused subgraphs.
  - For `InMemorySaver`: deletes blobs no longer referenced by kept checkpoints.
  - For checkpointers that implement `prune` (e.g. Redis), delegates to `cp.prune([thread_id], strategy="keep_latest")`.
  - Unsupported checkpointers or graphs using `DeltaChannel` fail fast at construction.
- Note: Earlier runs' `Run.snapshot`, `Run.state`, and `Run.result` raise `RuntimeError` after older checkpoints are pruned. Active execution state, `Session.dialog`, and new turns are unaffected.

### Stored message window (`retain_messages=N`)
- The prebuilt templates take `retain_messages: int | None = None`.
- When set, a plain `"retain"` node runs on turn completion (`done`), emitting `RemoveMessage(id=...)` for every message outside the sliding window.
- The window matches `SlidingWindowContext` (keeps the most recent N messages, never starting with an orphaned tool result).
- Passing `retain_messages` smaller than the view window configured on `SlidingWindowContext` raises `ValueError` at construction.
- On paused/escalated turns, `"retain"` does not run, ensuring human guidance and questions survive.

### Ending threads (`Session.end()` and `Run.end()`)
- Call `session.end()` or `run.end()` when a conversation or one-shot task is complete to delete its thread from the checkpointer (`delete_thread`).
- `Session.end()` deletes the session's thread. Afterwards `session.dialog` returns `[]`, and a subsequent `send()` starts a clean thread.
- `Run.end()` deletes a one-shot run's thread. Calling `run.end()` on a run belonging to a session raises `ValueError` (call `session.end()` instead).
- Both refuse a paused execution unless `abandon=True` is passed.
- **Important:** Callers must read `result`, `status`, or `state` *before* calling `end()`. Only checkpointer storage is deleted; vsigma memory (transcripts in `MemoryStore`, episode summaries, distilled knowledge) is preserved.

### Consumer migration notes
- **A web app** (pin `v0.1.0` first, then migrate to `v0.2.1`, not `v0.2.0`: one app-wide checkpointer plus a per-connection "busy" guard lets a reconnect during a turn start a second turn on the same session; see `v0.2.1` above):
  - Checkpointer: one for the whole app, passed to every connection the way the memory store is: a `SqliteSaver` on a file in a persistent volume, so dialogs survive reopens, reconnects and restarts (see 4.8, Legacy persisted dialogs). A graph builder that hardcodes `MemorySaver()` should take the checkpointer as a parameter instead, and pass `checkpoints=` to its `Agent`.
  - A question pending across a reopen: the app's choice. The shared checkpointer keeps the pause, so a session reopened in a new handle, or after a restart, still reports it (`session.paused`, `session.interrupt`, `session.interrupt_id`), and `resume` continues it. Either show the question again and resume with its id, or discard it with `send(text, abandon=True)` so the next message starts a new turn: for example, when the front end cannot show the question again.
  - `checkpoints="latest"`: fits when the app does not inspect earlier runs.
  - `retain_messages`: does *not* fit while the app reads every user turn from `Session.dialog`.
  - Ending threads: nothing deletes a closed session's thread until vsigma can expire idle threads; under `"latest"` each keeps one checkpoint.
- **bumblebee:**
  - Runs as a daemon on `MemorySaver` (`core/agent.py:361`), so every session's checkpoints remain in memory. `checkpoints="latest"` bounds memory growth to one checkpoint per session.
  - `retain_messages`: fits cleanly. Bumblebee windows its view (`core/agent.py:351-358`), keeps full transcripts in `SQLiteStore` capture rows, and reads no raw messages from state.

---

## `v0.2.0` (Phase 4.9) — LangGraph Messages

- **`ReActState.messages` holds LangChain messages:**
  `messages` in `ReActState` is reduced by LangGraph's `add_messages` (`langchain_core.messages.AnyMessage`). Messages carry unique ids and merge by id. Nodes may still return plain OpenAI dicts (`{"role": "assistant", "content": ...}`); LangGraph converts them to LangChain messages upon reduction.
- **Node contract & subgraph composition:**
  The 4.8 custom reducer and its fail-loud guard have been removed. Subgraph nodes that share the `messages` channel now compose cleanly without manual wrappers, whether they run straight through or pause at an `interrupt()`. Returning cumulative history updates existing messages by id rather than creating duplicates.
- **In-graph readers:**
  Code reading messages from `state["messages"]` directly (such as custom context managers or in-graph edge routers), as well as `run.state["messages"]`, sees LangChain message objects. Inspect `m.type` (`"human"`, `"ai"`, `"tool"`, `"system"`) and `m.text` / `m.content` instead of `m["role"]` or `m["content"]`.
- **Dict boundaries:**
  Where messages leave graph state, they remain plain OpenAI dicts:
  - `LLM.complete(messages, tools)` receives plain OpenAI dicts (the `LLM` protocol is unchanged).
  - Memory capture rows (`input:<uuid>` and `<node>:<uuid>`) store plain OpenAI dicts.
  - `Session.dialog` returns plain OpenAI dicts.
- **Consumer migration notes:**
  - **A web app** with its own graph:
    - A context manager that reads roles reads `m.type` instead of `m["role"]` and injects a `SystemMessage`. It also takes `memory: MemoryStore | None` per 4.8.
    - A clarify node: returning its two new messages is recommended, but returning cumulative history also works without duplicates.
    - Scripts reading `run.state["messages"]` access `.type` and `.content`. Code behind `LLM.complete` keeps reading dicts.
    - Readers of `session.dialog`, and code calling `llm.complete` directly, are unchanged.
    - A second loop on a LangChain chat model now shares one message type with vsigma's.
  - **bumblebee:**
    - Uses `make_guarded_react_agent` with `SlidingWindowContext` (`core/agent.py:349-361`) and reads no messages. No migration changes beyond 4.8.

---

## `v0.2.0` (Phase 4.8) — Thread-Native Runtime

### Slice 1 — Append-Only Messages

- **Custom node return contract:**
  In vsigma 4.8, nodes return **only the messages they add**, never cumulative history.
  ```python
  # PRE-4.8:
  def my_node(state: State) -> State:
      messages = list(state.get("messages", []))
      messages.append({"role": "user", "content": "..."})
      return {"messages": messages}

  # 4.8+:
  def my_node(state: State) -> State:
      return {"messages": [{"role": "user", "content": "..."}]}
  ```
  *(In 4.9, `add_messages` merges messages by id, allowing subgraphs and repeats to merge cleanly).*

- **View-only context & capture:**
  Context assembly (`wrap`) is view-only. Nodes see the trimmed window, but new messages are appended to full thread history. Episodic context capture rows hold only each node firing's newly added messages ($O(N)$ growth).

### Slice 2 — Session = Thread & Stateless Session Handle

- **Session is a LangGraph thread:**
  `thread_id = session_id`. The session handle holds no in-memory state. Dialog, pause status, and interrupt payloads are read directly from checkpointer snapshots (`graph.get_state()`).
- **`Session.runs` removed:**
  `Session.runs` was process-local and is removed. Use `session.paused` and `session.interrupt` to inspect execution state.
- **`send` and `run` while paused raise `SessionPaused`:**
  To prevent LangGraph from silently dropping pending interrupts when new input arrives, both `session.send(text)` and `agent.run(task, session_id=...)` raise `SessionPaused` if the session thread is interrupted. To resume, call `session.resume(value)` or `run.resume(value)`. To explicitly discard the pause, pass `abandon=True` (`session.send(text, abandon=True)` or `agent.run(task, session_id=..., abandon=True)`).
  *Example:* an app with two kinds of pause dispatches on the interrupt's `kind`: resume a clarification, or `send(text, abandon=True)` past a spent escalation.
- **`Agent.run(history=)` removed:**
  Continuity lives on the LangGraph thread. Passing `history=` to `Agent.run()` is removed.
- **System prompt moved to `state["system"]`:**
  The system prompt is no longer pinned into `messages[0]`. It is stored in `state["system"]` on every turn. Nodes that invoke an LLM (`react_node`, `plan_node`, or custom nodes) prepend `state["system"]` to the completion window.
- **Legacy persisted dialogs:**
  Pre-4.8 dialogs stored under `session:<id>:context/dialog` in `SQLiteStore` are no longer read; a session's first turn in 4.8 starts a fresh thread. A conversation outlives the process only with a persistent checkpointer, shared by every session: `SqliteSaver(sqlite3.connect(path, check_same_thread=False))`. `SqliteSaver` comes from `langgraph-checkpoint-sqlite`, which vsigma does not install: add it to the consumer's dependencies.

### Slice 3 — Optional Memory & `Interceptor` Rename

- **`Services` renamed to `Interceptor` (no compatibility aliases):**
  The cross-cutting wrapper container is renamed to `Interceptor` (`src/vsigma/interceptor.py`). `Services`, `src/vsigma/seam.py`, `Agent.services`, and the templates' `services=` parameter are completely removed. Import and use `Interceptor`.
  ```python
  from vsigma import Interceptor, TrajectoryLog

  interceptor = Interceptor(observer=TrajectoryLog())
  ```
- **Keyword-only construction:**
  `Interceptor` uses `kw_only=True`: arguments (`observer`, `memory`, `context`, `run_end`) must be passed by keyword.
- **Optional memory store (`memory=None`):**
  Agents and templates now default to `memory=None`. With no `memory` store provided:
  - Zero external store writes occur during the run (no `input:<uuid4>` or `node:<uuid4>` trajectory rows).
  - Episode trajectories and state live entirely in the thread checkpointer.
  - Attempting to use memory-dependent features without configuring a store fails loudly: `RecallContext.assemble` raises `ValueError`, and `record_episode` / `episode_trajectory` require an explicit `MemoryStore`.
- **Template argument updated:**
  Prebuilt agent templates (`make_react_agent`, `make_guarded_react_agent`, `make_plan_execute_agent`, `make_reflexion_agent`) now take `interceptor: Interceptor | None = None`. The old `services=` parameter is removed.
- **Stream-driven trajectory observability:**
  Trajectory events are now emitted from LangGraph's execution stream (`updates` and `values` modes) inside `Agent.run` and `Run.resume`. They are no longer emitted inside node `wrap()`. This guarantees that unwrapped nodes and nested subgraphs emit trajectory events accurately. Accepted cost: raw `graph.invoke` bypasses the stream and emits no events.
- **Custom ContextManager signature:**
  `ContextManager.assemble` requires `memory: MemoryStore | None` as its second argument (`assemble(self, state: State, memory: MemoryStore | None) -> State`). Custom context managers must accept this argument without default. Dead system prompt handling in `SlidingWindowContext` and `RecallContext` has been removed since system prompts reside in `state["system"]`.
- **Opt-in observer (`observer=None` default):**
  `Interceptor.observer` now defaults to `None` instead of `TrajectoryLog()`. Production pipelines avoid unbounded in-memory trajectory event growth. For debugging or tests, pass `interceptor=Interceptor(observer=TrajectoryLog())`. If no TrajectoryLog observer is configured, accessing `run.trajectory` raises `RuntimeError`.
  *Example:* a server that created a `TrajectoryLog` per connection and never read it should drop it, so connections do not retain events for their whole lifetimes.
