# Phase 4.14 — Maintenance: one call, pruning by default

**Status:** **COMPLETED** 2026-10-31. Completes `v0.3.0`.
Follows [`docs/proposals/maintenance.md`](../docs/proposals/maintenance.md) and
[`docs/proposals/episode-retention.md`](../docs/proposals/episode-retention.md).

## Completed Work

- [x] **Step 1: MemoryPass Renames, Extraction & Private Batch Helper**
  - `src/vsigma/memory_pass.py`: `MemoryPass`, `MemoryPassReport`, `_consolidate_batch`, `store_guard`.
  - Markers record stored size from `namespace_sizes()[ns]`.
  - Ended episodes only (`done`, `failed`, `abandoned`). Tombstones skipped.
  - `only_owner: bool = False` in `episode_trajectory`.
- [x] **Step 2: `EpisodeRetention` & `RetentionPolicy`**
  - `src/vsigma/retention_episodes.py`: `EpisodeRetention`, `RetentionPolicy`, `RetentionReport`.
  - Canonical ASCII hashing (`sort_keys=True, ensure_ascii=True`).
  - Crash recovery completes interrupted prunes before new work.
  - Budget pruning prioritizes consolidated over unconsolidated; 3-turn prior context protection where pruned turns count towards window without blocking earlier prunes.
  - Naive/aware datetime compatibility; one-second precision ties broken by index order.
- [x] **Step 3: `Agent.maintain()` with Owner Routing & Multi-Job Coordination**
  - `src/vsigma/maintenance.py`: `maintain()`, `MaintenanceReport`.
  - Four jobs coordinated: ThreadExpiry sweep, MemoryPass with owner routing, checkpoint pruning skipping busy and paused threads, and EpisodeRetention.
  - Starvation-free owner batching loop with error isolation (`ExceptionGroup`).
  - Upfront validation for invalid configurations.
  - `Agent.maintain()` wired to `maintain()`.
- [x] **Step 4: CLI `vsigma memory maintain`**
  - `src/vsigma/cli.py`: Added `vsigma memory maintain` with `--db`, `--source`/`--all-sources`, `--scope`, `--keep-last`, `--max-bytes`, `--max-age`, `--prune-unconsolidated`.
- [x] **Step 5: Documentation & Graduation**
  - `docs/upgrading.md`: Breaking renames without aliases, new `Agent.maintain()` API, CLI `maintain`, `RetentionPolicy`, and `EpisodeRetention`.
  - `docs/architecture_design.md` §§8-9 updated.
  - `docs/getting_started.md`: Added `agent.maintain()` example.
  - Proposals `docs/proposals/maintenance.md` and `docs/proposals/episode-retention.md` status changed to `GRADUATED`.
  - `vdesign/plan.md` marked Phase 4.14 `[x]`.
  - `vdesign/checkpoint.md` updated with completion summary and Next Steps.
- [x] **Tests & Quality Gates**
  - 326 tests passing (`uv run pytest`).
  - `uv run ruff check` clean.
  - `uv run mypy` clean across 72 source files.
  - `uv lock --locked` verified.
