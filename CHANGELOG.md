# Changelog

What each vsigma release contains. How to move your code from one to the next is in
[`docs/upgrading.md`](docs/upgrading.md), whose sections are titled by version. Below 1.0, the
second number changes for breaking releases and the third for compatible fixes and additions.
Releases are tagged on `master`.

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

The first public release, on PyPI. The code is as in `v0.2.1`: nothing to migrate.

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
