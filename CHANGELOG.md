# Changelog

What each vsigma release contains. How to move your code from one to the next is in
[`docs/upgrading.md`](docs/upgrading.md), whose sections are titled by version. Below 1.0, the
second number changes for breaking releases and the third for compatible fixes and additions.
Releases are tagged on `master`.

## v0.4.1 — 2026-11-12

Phase 4.20. Compatible with `v0.4.0`: distilled memory stops keeping the state of things at
the time of a run. Nothing to change in app code.

### Changed

- Distilled memory (the run-end round, `maintain`, bootstrap) no longer keeps a fact the
  model marks as the state of something at the time, such as an order's status. The prompt
  asks whether each fact is a standing fact or run-time state (`"lasting": true | false`),
  and `distill` drops facts marked `false`. Nothing to change in app code; records already
  stored stay ([durability-bar](docs/proposals/durability-bar.md)).

## v0.4.0 — 2026-11-03

Phases 4.16–4.19, and the first release on PyPI. Breaking: a session belongs to the user who started it, an agent refuses two
tools with one name, and MCP calls time out after 120 s by default. Session memory can now be
forgotten, and `mcp` 2 is supported. How to move: `docs/upgrading.md`, the `v0.4.0` sections.

### Changed

- **Breaking:** `react_node` and all agent builders now refuse two tools with the same name
  (`ValueError`, naming the tool and advising `dataclasses.replace(t, name=...)`)
  ([tool-sources](docs/proposals/tool-sources.md)).
- **Breaking:** `make_mcp_tools(connect, *, timeout=120.0)` now bounds each MCP tool call to 120s
  by default (pass `timeout=None` for no limit), cancelling timed-out calls and returning
  `"error: MCP tool '<name>' timed out after <timeout>s"` ([tool-sources](docs/proposals/tool-sources.md)).
- **Breaking:** A session's `user_id` is now fixed on its first turn ([session-admission](docs/proposals/session-admission.md)).
  Passing a different `user_id` on an existing session, a named `user_id` on an open session
  (`user_id=None`), or `user_id=None` on an owned session now raises `SessionDenied` across
  `Agent.run` and `Session` (`dialog`, `paused`, `interrupt`, `interrupt_id`, `send`, `resume`, `end`).
- **Breaking:** `episode_trajectory` always filters prior context to episodes with the same `user_id`;
  its `only_owner` parameter is removed.
- `ThreadExpiry(..., store=store)` now requires a `MaintainableStore` (`TypeError`) and forgets
  expired session memory (`session:<id>/context` and `session:<id>/knowledge`) via the crash-recoverable
  `sessions/forget/<id>` record, detaching the session's episodes (`session_id=None` in `episodes/index`)
  ([scope-lifespans](docs/proposals/scope-lifespans.md) §C.2, [thread-expiry](docs/proposals/thread-expiry.md) §B.3.2).
- `EpisodeRetention` now treats every episode with neither `user_id` nor `session_id` (including
  anonymous one-shot runs and detached anonymous session episodes, which used to be protected
  forever whenever consolidation was in use) as having no owner to consolidate it, making it
  eligible for age and budget pruning as `pruned_unconsolidated`.
- `mcp` dependency widened to `mcp>=1.28.1,<3` (`mcp` 1 and 2 supported in `make_mcp_tools`,
  [open-source-groundwork](docs/proposals/open-source-groundwork.md) §B.6).

### Added

- `SessionDenied(RuntimeError)` (carrying `.session_id` and the refused `.user_id`) and
  `sessions/owners/<id>` persistence across plain `Session.end()` (deleted on `end(forget=True)`
  and `ThreadExpiry` with a store).
- `Session.end(forget=True)` to delete session memory and detach the session's episodes alongside
  the checkpointer thread (or after a plain `end()`), and `ExpiryReport.forgotten_sessions`.

## v0.3.1 — 2026-11-02

Phase 4.15. Compatible with `v0.3.0`. One long-lived `Agent` can now serve many sessions. How to
move an app that built one `Agent` per session: `docs/upgrading.md`, `v0.3.1`.

### Added

- `run_context: Any = None` on `Agent.run` and `Agent.session`, forwarded via `Run` and `Session`
  to LangGraph's `graph.stream(context=...)` ([run-context](docs/proposals/run-context.md)).
- `run_context() -> Any` helper and `NoRunContext(RuntimeError)` exception to access process-local
  session objects inside tools, hand-built nodes, routers, and `ContextManager`s without writing
  credentials to graph state, checkpointer storage, or memory.
- Fail-closed context guard: checkpoint metadata records `has_context: bool` (never the object itself).
  Subsequent `Session.send()` and `resume()` calls without an object raise `NoRunContext` before
  starting or modifying interrupts. `react_node` re-raises `NoRunContext` immediately to fail closed
  instead of converting it to a tool error message for the model.
- The object must be truthy: LangGraph reads a falsy one (`{}`, `[]`) as none, so `run_context()`
  raises `NoRunContext`.

## v0.3.0 — 2026-11-01

Phases 4.13 and 4.14. Breaking: renames without aliases, and old store files are not migrated. How
to move: `docs/upgrading.md`, `v0.3.0`.

### Changed

- `SQLiteStore` is now `PersistentStore`, an adapter over LangGraph's `SqliteStore`. Opening a store
  file from an earlier release raises and leaves the file untouched; `v0.2.3`'s
  `vsigma memory export` can still read it.
- `run_maintenance` is now `MemoryPass`, and its report is `MemoryPassReport`. `MaintenanceReport` is
  now what `Agent.maintain()` returns.
- `langgraph-checkpoint-sqlite` is a required dependency.

### Added

- `Agent.maintain()` runs the expiry and recovery sweep, the memory pass, old-checkpoint pruning and
  the transcript budget in one call. By default it prunes old checkpoints and bounds transcripts to
  1 GiB, with no LLM calls; ending conversations (`expiry=`) and consolidating (`distiller=`) are
  explicit. Every job runs, and failures are raised together as an `ExceptionGroup`.
- `EpisodeRetention` and `RetentionPolicy` prune episode transcripts, keeping the episode's index
  and summary and leaving a tombstone.
- The memory pass files knowledge with its owner: `persona(user_id)`, or `session(session_id)` when
  an episode has no user.
- The `MaintainableStore` protocol (`namespaces`, `namespace_sizes`, `delete`), on `PersistentStore`
  and `InMemoryStore`.
- The CLI command `vsigma memory maintain`.

### Known issues

- `Agent.maintain()` requires that its agent be the only live `Agent` holding the store, but it can
  only see agents in its own process. Two processes sharing a store are not detected.

## v0.2.3 — 2026-10-31

Phase 4.12, compatible with `v0.2.2`: nothing to migrate. What is new, and how to set it up:
`docs/upgrading.md`, `v0.2.3`.

### Added

- Declarative checkpointer thread expiry (`ThreadExpiry`) across `InMemorySaver` and `SqliteSaver` (`idle_ttl`, `keep_last` per user, `paused_idle_ttl`, `run_ttl`).
- Operational status namespace `episodes/status` tracking run lifecycle (`running`, `escalated`, `done`, `failed`, `abandoned`).
- Recovery sweep in `ThreadExpiry(store=store)`: recovers dead running episodes whose lock is free, and escalated episodes whose thread is gone or no longer holds its pause, recording them `abandoned`.

### Changed

- The package author and the `LICENSE` copyright holder are `abbcdfin`.

## v0.2.2 — 2026-10-29

The first public release. The code is as in `v0.2.1`: nothing to migrate.

### Added

- The MIT `LICENSE` file, and package metadata: licence, project links, keywords and
  classifiers.

### Changed

- The README's links point to GitHub, so they also work on PyPI.
- `docs/upgrading.md`: discarding a question still pending when a session reopens is the app's
  choice. With a persistent checkpointer the pause survives a reopen or a restart and can be
  resumed.

## v0.2.1 — 2026-10-29

Phase 4.11, a bug fix for `v0.2.0` and compatible with it. Migration: `docs/upgrading.md`,
`v0.2.1`.

### Fixed

- Two operations at once on one thread. A second turn, resume or `end()` on a session or
  one-shot run that is busy now raises `SessionBusy` at once. Before, two turns started together
  overwrote each other's messages or dropped an interrupt, and `end()` during a turn was undone
  when the turn finished. One process per checkpointer is the supported setup.
- A late answer could land on a newer question when a run paused twice. `resume(value,
  interrupt_id=...)` now raises `PauseChanged` if the pause it names is no longer pending.
  Without `interrupt_id`, `resume` behaves as before.
- `mcp` is capped below 2. A fresh install resolved `mcp` 2.x, which vsigma is not tested
  against.

### Added

- `SessionBusy` and `PauseChanged`, both `RuntimeError`s.
- `interrupt_id` on `Session`, `Run` and `SessionPaused`.

## v0.2.0 — 2026-10-27

Phases 4.8–4.10. **Breaking.** Migration: `docs/upgrading.md`, `v0.2.0`. Requires
`langgraph>=1.2.4`.

### Changed

- Phase 4.8, the thread-native runtime: a session is a LangGraph thread (`thread_id =
  session_id`), and its dialog lives on the checkpointer. `Services` becomes `Interceptor`;
  memory and the trajectory observer are optional. `Session.send` raises `SessionPaused` on a
  paused session, and `Session.resume` continues it.
- Phase 4.9, LangGraph messages: state holds LangChain messages, merged by id with
  `add_messages`.

### Added

- Phase 4.10, thread retention: opt-in `checkpoints="latest"` and `retain_messages=N`, and
  `Session.end()` / `Run.end()` to delete a finished thread.

### Known issues

- Nothing stops two operations at once on one session: concurrent turns can overwrite each
  other's messages or drop an interrupt, and `end()` during a turn is undone. Fixed in `v0.2.1`.
- A late answer can land on a newer question. `v0.2.1` adds `interrupt_id` to prevent it.

## v0.1.0 — 2026-10-27

Phase 4.7, the first release, and the last before the breaking changes of `v0.2.0`. Pin it
until you migrate.

### Known issues

- A message sent to a paused session resumes whatever pause is pending, so a late answer can
  land on a newer question. `v0.2.1` adds `interrupt_id` to prevent it.
