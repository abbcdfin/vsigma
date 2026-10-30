# Active Plan — Phase 4.12: Thread Expiry

**Target:** Implement [`docs/proposals/thread-expiry.md`](../docs/proposals/thread-expiry.md) (ACCEPTED as Phase 4.12, target release `v0.2.3`).
**Scope:** Checkpointer thread expiry (`InMemorySaver` and `SqliteSaver`) and the runtime episode status lifecycle with `ThreadExpiry`'s store recovery sweep. Session memory forgetting via `sessions/forget` remains deferred until the `BaseStore` adapter lands.

---

## Invariants & Design Contracts

1. **Locking & Concurrency:** Every check-then-act on a thread holds `thread_lock(checkpointer, thread_id)` non-blockingly; busy threads raise `SessionBusy` immediately, and expiry skips them without waiting.
2. **Zero-State-Deserialization Scan:** `ThreadExpiry.threads()` reads root (`""`) checkpoint metadata and pending writes without deserializing checkpoint state.
3. **UUIDv6 Timestamp Decoding:** `last_active` is decoded directly from the uuid6 checkpoint ID (`((u.int >> 80) << 12) | ((u.int >> 64) & 0x0FFF)` converted to UTC `datetime`), pinned against `ts` within milliseconds.
4. **Conditional Atomic Deletion:** Deletion re-reads the thread's latest root checkpoint ID under `thread_lock` and aborts if it moved since scan. On `SqliteSaver`, the re-read and `DELETE` share one `BEGIN IMMEDIATE` transaction under `cp.lock`. Deletion purges all namespaces, writes, and unreferenced blobs.
5. **Paused Thread Exemption:** Paused threads (`__interrupt__` write on root checkpoint) follow `paused_idle_ttl` (defaulting to `idle_ttl`) alone and do not count toward `keep_last`.
6. **Episode Status Lifecycle:** Operational namespace `episodes/status` records `running`, `escalated`, `done`, `failed`, and `abandoned` under `thread_lock` whenever memory is configured. Final statuses (`done`, `failed`, `abandoned`) never change.
7. **Dead Run Recovery Sweep:** When configured with a `store`, `ThreadExpiry.run()` sweeps `episodes/status` after deletes. Under `thread_lock`, a free lock on a `running` episode or an `escalated` episode whose pause is gone transitions to `abandoned`.

---

## Step-by-Step Execution Plan

### Step 1 — Module Layout Cleanup (`threads.py` + `prune_latest`) `[ ]`
*Addresses Architect review item 4 from Phase 4.11 to give `expiry.py` clean imports.*
- Create `src/vsigma/threads.py`:
  - Move `SessionBusy`, `_REGISTRY_LOCK`, `_BUSY_THREADS`, and `thread_lock` from `retention.py`.
- Update `src/vsigma/retention.py`:
  - Re-export `SessionBusy`, `thread_lock`, `_REGISTRY_LOCK`, and `_BUSY_THREADS` for backwards compatibility.
  - Add public `prune_latest(graph: Any, thread_id: str) -> None` (calls `_pruner(graph)(thread_id)` assuming caller holds `thread_lock`).
  - Update `keep_latest` to call `prune_latest` inside `with thread_lock(...)`.
- Update `src/vsigma/run.py` and `src/vsigma/sessions.py`:
  - Import `SessionBusy, thread_lock` from `.threads` and `prune_latest, validate_checkpointer_support` from `.retention` (no private `_pruner` import).
- **Verification:** `uv run ruff check && uv run mypy && uv run pytest` (234 passing).

### Step 2 — `ThreadInfo` and `ThreadExpiry.threads()` (`src/vsigma/expiry.py`) `[ ]`
- Define data structures in `src/vsigma/expiry.py`:
  - `ThreadInfo(thread_id: str, kind: Literal["session", "run"], user_id: str | None, run_id: str, last_active: datetime, idle: timedelta, paused: bool)`.
  - `ExpiryPolicy(idle_ttl: timedelta | None = None, keep_last: int | None = None, paused_idle_ttl: timedelta | None = None, run_ttl: timedelta | None = None)`.
  - `ExpiredThread(thread_id: str, kind: Literal["session", "run"], user_id: str | None, run_id: str, reason: str)`.
  - `ExpiryReport(expired: list[ExpiredThread], skipped_busy: list[str], skipped_changed: list[str], recovered_episodes: list[str])`.
- Construction check:
  - `ThreadExpiry(checkpointer: Any, policy: ExpiryPolicy, store: MemoryStore | None = None)`.
  - Validate checkpointer is an instance of `InMemorySaver` or `SqliteSaver` (with `_SqliteSaver` import guard); raise `NotImplementedError` otherwise.
- Implement `ThreadExpiry.threads(*, now: datetime | None = None) -> Iterator[ThreadInfo]`:
  - `InMemorySaver`: iterate `storage.keys()`; read root `""` checkpoint dictionary; take `max(checkpoint_ids)`; extract uuid6 timestamp; check pending writes for channel `"__interrupt__"`; read `metadata`.
  - `SqliteSaver`: query `SELECT thread_id, checkpoint_id, metadata, EXISTS(SELECT 1 FROM writes w WHERE w.thread_id = c.thread_id AND w.checkpoint_ns = '' AND w.checkpoint_id = c.checkpoint_id AND w.channel = '__interrupt__') FROM checkpoints c WHERE checkpoint_ns = '' AND checkpoint_id = (SELECT MAX(checkpoint_id) FROM checkpoints c2 WHERE c2.thread_id = c.thread_id AND c2.checkpoint_ns = '')`.
- Export `ThreadExpiry`, `ExpiryPolicy`, `ThreadInfo`, `ExpiredThread`, `ExpiryReport` in `src/vsigma/__init__.py`.
- **Tests (`tests/test_expiry.py`):**
  - Parametrized over `InMemorySaver` and `SqliteSaver`:
    - `last_active` matches checkpoint `ts` within milliseconds.
    - Fields (`kind`, `user_id`, `run_id`, `paused`, `idle`) across completed sessions, one-shot runs, paused sessions, and subgraphs paused at `interrupt()`.
  - Unsupported checkpointer raises `NotImplementedError` at construction.

### Step 3 — `ExpiryPolicy` Validation and Race-Free Checkpointer Deletion `[ ]`
- Validate `ExpiryPolicy` at construction:
  - Raise `ValueError` if all four parameters are `None`.
  - Raise `ValueError` if any duration is `<= timedelta(0)`.
  - Raise `ValueError` if `keep_last < 1`.
- Policy evaluation logic:
  - If `paused`: eligible only if `paused_ttl` (or `idle_ttl` if `paused_idle_ttl is None`) exceeded; exempt from `keep_last`.
  - If `kind == "run"`: eligible only if `run_ttl` (or `idle_ttl` if `run_ttl is None`) exceeded.
  - If `kind == "session"` and not paused: eligible if `idle > idle_ttl`, or if rank in user's active session list > `keep_last` (`user_id=None` forms one group).
- Implement deletion in `ThreadExpiry.run(*, now: datetime | None = None, dry_run: bool = False) -> ExpiryReport`:
  - If `dry_run=True`: evaluate policy and populate `report.expired` without locking or deleting.
  - For each candidate:
    - Attempt `thread_lock(checkpointer, thread_id)` non-blockingly; if `SessionBusy`, add to `skipped_busy` and continue.
    - Under lock: re-read latest root checkpoint ID. If mismatch or gone, add to `skipped_changed` and continue.
    - Delete thread:
      - `InMemorySaver`: delete from `storage[thread_id]`, `writes`, and unreferenced `blobs`.
      - `SqliteSaver`: inside `cp.lock` and `BEGIN IMMEDIATE`, delete from `checkpoints` and `writes` where `thread_id = ?`, then commit.
- **Tests (`tests/test_expiry.py`):**
  - Policy validation errors (empty, negative TTL, `keep_last < 1`).
  - Single and combined limits (`idle_ttl`, `keep_last`, `paused_idle_ttl`, `run_ttl`).
  - `keep_last` per user and for `user_id=None`.
  - Paused session survives `keep_last`.
  - Turn in progress (`BlockingLLM`) triggers `skipped_busy` and completes intact.
  - Checkpoint change between scan and lock triggers `skipped_changed`.
  - `dry_run=True` reports candidates without deletion.

### Step 4 — Episode Status Lifecycle (`episodes/status`) & Recovery Sweep `[ ]`
- Update `src/vsigma/episodes.py`:
  - `EPISODE_STATUS_NS = "episodes/status"`
  - `FINAL_EPISODE_STATUSES = frozenset({"done", "failed", "abandoned"})`
  - Helpers: `record_episode_status(store, run_id, status)`, `get_episode_status(store, run_id)`, `list_episode_statuses(store)`.
  - Invariant: writing to an episode already in `FINAL_EPISODE_STATUSES` is a no-op.
- Wire status transitions under `thread_lock`:
  - `Agent.run`:
    - If `session_id is not None and abandon`: transition prior pending `run_id` on that thread to `abandoned`.
    - At start: write `status: "running"`.
    - Try `_stream_run`: on exception, write `status: "failed"` before re-raising.
    - On completion: write `escalated` if interrupted, else `done`.
  - `Run._resume_locked`:
    - Try `_stream_run`: on exception, write `status: "failed"` before re-raising.
    - On completion: write `escalated` if interrupted, else `done`.
  - `Session.end()` & `Run.end()`:
    - If memory configured: read latest `run_id`, delete thread first, then record `run_id` as `abandoned` (if not final).
- Wire store sweep in `ThreadExpiry.run()` when `store is not None and not dry_run`:
  - After thread deletions: for each deleted thread with known `run_id`, record `abandoned`.
  - Recovery sweep: query all unfinished episodes (`running` or `escalated`) from `episodes/status`.
  - For each unfinished episode, determine `thread_id = (owner.session_id or run_id)`.
  - Try `thread_lock(checkpointer, thread_id)`: if `SessionBusy`, skip.
  - Under lock:
    - If `running`: lock is free, so process died or crashed mid-turn -> mark `abandoned`.
    - If `escalated`: check if thread exists on checkpointer with matching latest `run_id` and is still paused; if not -> mark `abandoned`.
- **Tests (`tests/test_expiry.py` and `tests/test_episodes.py`):**
  - Normal turn status lifecycle: `running` -> `done` (or `escalated`).
  - Exception during run/resume writes `failed` and re-raises.
  - `abandon=True` and `end(abandon=True)` write `abandoned`.
  - Final statuses never overwrite.
  - Crashed turn before first checkpoint (`running`, no thread) recovered as `abandoned`.
  - Live turn in progress is skipped and stays `running`.
  - Orphaned `escalated` run whose session moved on recovered as `abandoned`.

### Step 5 — Documentation, Roadmap Updates, and Phase 4.12 Tag `[ ]`
- Update `docs/upgrading.md` with an additive section on `ThreadExpiry` and `ExpiryPolicy`.
- Update `docs/getting_started.md` session lifecycle section.
- Mark `docs/proposals/thread-expiry.md` **GRADUATED (Phase 4.12)**.
- Update `vdesign/plan.md` marking Phase 4.12 `[x]`.
- Collapse `vdesign/active_plan.md`.
- Update `vdesign/checkpoint.md` with test counts and outcomes.
- Run full gate: `uv lock --locked && uv run ruff check && uv run mypy && uv run pytest`.
- Create annotated tag `phase-4.12`.
