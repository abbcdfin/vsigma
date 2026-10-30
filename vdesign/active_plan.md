# Active Plan — Phase 4.12: Thread Expiry

**Target:** Implement [`docs/proposals/thread-expiry.md`](../docs/proposals/thread-expiry.md) (ACCEPTED as Phase 4.12, target release `v0.2.3`), incorporating all review feedback from the Architect.
**Scope:** Checkpointer thread expiry (`InMemorySaver` and `SqliteSaver`) and the runtime episode status lifecycle with `ThreadExpiry`'s store recovery sweep. Session memory forgetting via `sessions/forget` remains deferred until the `BaseStore` adapter lands.

---

## Invariants & Design Contracts

1. **Locking & Proof of Lock (`LockedThread`):**
   - Every operation that inspects and mutates a thread holds `thread_lock(cp, thread_id) -> Iterator[LockedThread]`.
   - `LockedThread(checkpointer, thread_id)` tracks lock validity: using it after the block exits raises `RuntimeError`.
   - Every lock-holding helper (`prune_checkpoints`, thread deletion, `episodes/status` writes) requires `locked: LockedThread` rather than an unverified `thread_id`.
   - `prune_checkpoints(graph, locked)` verifies `locked.checkpointer is graph.checkpointer`.
   - `keep_latest(graph, thread_id)` stays public and unchanged: acquires `thread_lock` and calls `prune_checkpoints`.
2. **Zero-State-Deserialization Scan:**
   - `ThreadExpiry.threads()` reads root (`""`) checkpoint metadata and pending writes without deserializing checkpoint state.
   - `last_active` is decoded directly from the uuid6 checkpoint ID (`((u.int >> 80) << 12) | ((u.int >> 64) & 0x0FFF)` converted to UTC `datetime`), pinned against `ts` within milliseconds.
3. **Conditional Atomic Deletion:**
   - Deletion re-reads the thread's latest root checkpoint ID under `LockedThread` and aborts if it moved since scan. On `SqliteSaver`, the re-read and `DELETE` share one `BEGIN IMMEDIATE` transaction under `cp.lock`.
   - Deletion purges all namespaces, writes, and unreferenced blobs.
4. **Policy & Exemption Rules:**
   - `ExpiryPolicy(idle_ttl=None, keep_last=None, paused_idle_ttl=None, run_ttl=None)`:
     - `paused_idle_ttl` defaults to `idle_ttl`.
     - `run_ttl` defaults to `idle_ttl`.
     - Paused threads follow `paused_idle_ttl` alone and do not count toward `keep_last`.
     - `keep_last` on its own limits only unpaused sessions (noted in docstring: one-shot runs and paused threads never expire under `keep_last` alone).
     - Empty policy, non-positive TTLs, and `keep_last < 1` raise `ValueError` at construction.
5. **Episode Status Lifecycle:**
   - Operational namespace `episodes/status` records `running`, `escalated`, `done`, `failed`, and `abandoned` under `LockedThread` whenever memory is configured.
   - Write order at run start: inside `thread_lock`, first `record_episode`, then record `running`.
   - Final status timing: a status is final only once storage agrees:
     - Discarded pause (`abandon=True` on new turn): record old run `abandoned` *after* `_stream_run` returns, not at the start.
     - `end()` and expiry: delete thread first, then record `abandoned`.
     - `resume()` never writes `running`. If process dies mid-resume, episode remains `escalated`.
     - A resume that raises: check if thread still holds the pause; if it does, leave `escalated` (do not record `failed`).
   - Final statuses (`done`, `failed`, `abandoned`) never change; if a run finishes and finds status already final, log a warning and do not overwrite.
6. **Dead Run Recovery Sweep:**
   - When configured with `store`, `ThreadExpiry.run()` sweeps `episodes/status` after thread deletes.
   - `dry_run` covers the sweep too: reports what it would mark and writes nothing.
   - `ExpiryReport` lists run IDs recorded `abandoned`.
   - Under `thread_lock`:
     - If `running`: lock is free $\rightarrow$ process died mid-run $\rightarrow$ record `abandoned`.
     - If `escalated`: check 3 cases: (1) thread is gone, (2) thread's latest root checkpoint has a different `run_id`, or (3) thread is no longer paused $\rightarrow$ record `abandoned`.
     - If status has no `episodes/index` entry: skip and report/log, do not guess.

---

## Step-by-Step Execution Plan

### Step 0 — Decision Check Point Verdicts & Plan Updates `[ ]`
- Update `docs/proposals/thread-expiry.md`:
  - Mark status **ACCEPTED** (2026-10-30) as Phase 4.12.
  - Record all 9 §G verdicts as `**decided: yes**` (2026-10-30).
- Update `docs/proposals/scope-lifespans.md`:
  - Keep status **OPEN**; add status line noting §C.1 is built in Phase 4.12 (`v0.2.3`), and §C.2–§C.3 wait for the `BaseStore` adapter.
  - Record all 8 §H verdicts as `**decided: yes**` (2026-10-30).
- Update `vdesign/plan.md`:
  - Add `## Phase 4.12 — Thread expiry: ending idle sessions and runs, and episode status recovery [/]`.
  - Move `thread-expiry` out of `## Candidates`.
- Commit: `docs: record Decision Check Point verdicts for thread-expiry and scope-lifespans`.

### Step 1 — Module Layout Cleanup & Proof-of-Lock (`threads.py` + `prune_checkpoints`) `[ ]`
- Create `src/vsigma/threads.py`:
  - `SessionBusy(thread_id: str)`.
  - `LockedThread`: holds `checkpointer`, `thread_id`, and `_active: bool`. Has `check_active()` raising `RuntimeError("Lock on thread ... is no longer held")` once released.
  - `_REGISTRY_LOCK`, `_BUSY_THREADS` (WeakKeyDictionary).
  - `thread_lock(checkpointer: Any, thread_id: str) -> Iterator[LockedThread | None]`: yields `LockedThread` (or `None` if `checkpointer is None`); marks `LockedThread` inactive on exit.
- Update `src/vsigma/retention.py`:
  - Re-export `SessionBusy`, `thread_lock`, `_REGISTRY_LOCK`, `_BUSY_THREADS` for backwards compatibility.
  - Rename/replace `_pruner` with `prune_checkpoints(graph: Any, locked: LockedThread) -> None`:
    - Validates `locked.check_active()`.
    - Validates `locked.checkpointer is graph.checkpointer`.
    - Prunes checkpoints for `locked.thread_id` (deleting unreferenced blobs on in-memory; atomic SQLite transaction on SQLite).
  - Update `keep_latest(graph: Any, thread_id: str) -> None`:
    - Public, unchanged signature.
    - Uses `with thread_lock(graph.checkpointer, thread_id) as locked: assert locked is not None; prune_checkpoints(graph, locked)`.
- Update `src/vsigma/run.py` and `src/vsigma/sessions.py`:
  - Import `SessionBusy, thread_lock` from `.threads` and `prune_checkpoints, validate_checkpointer_support` from `.retention`.
  - In `Agent.run` and `Run._resume_locked`, call `prune_checkpoints(self.graph, locked)`.
- Update `tests/test_concurrency.py`:
  - Inspect `_BUSY_THREADS` from `vsigma.threads`.
  - Add test: `LockedThread` used after its block raises `RuntimeError`.
- **Verification:** `uv run ruff check && uv run mypy && uv run pytest` (no behaviour change).
- Commit: `refactor(threads): extract threads.py with LockedThread proof-of-lock and prune_checkpoints`.

### Step 2 — `ThreadInfo` and `ThreadExpiry.threads()` (`src/vsigma/expiry.py`) `[ ]`
- Data structures in `src/vsigma/expiry.py`:
  - `ThreadInfo(thread_id: str, kind: Literal["session", "run"], user_id: str | None, run_id: str, last_active: datetime, idle: timedelta, paused: bool)`.
  - `ExpiryPolicy(idle_ttl: timedelta | None = None, keep_last: int | None = None, paused_idle_ttl: timedelta | None = None, run_ttl: timedelta | None = None)`:
    - Docstring explicitly states that `keep_last` on its own limits only unpaused sessions (one-shot runs and paused threads never expire under `keep_last` alone).
    - `paused_idle_ttl` defaults to `idle_ttl`.
    - `run_ttl` defaults to `idle_ttl`.
  - `ExpiredThread(thread_id: str, kind: Literal["session", "run"], user_id: str | None, run_id: str, reason: str)`.
  - `ExpiryReport(expired: list[ExpiredThread], skipped_busy: list[str], skipped_changed: list[str], recovered_episodes: list[str])`.
- Construction check:
  - `ThreadExpiry(checkpointer: Any, policy: ExpiryPolicy, store: MemoryStore | None = None)`.
  - Validates `checkpointer` is instance of `InMemorySaver` or `SqliteSaver`; raises `NotImplementedError` otherwise.
- Implement `ThreadExpiry.threads(*, now: datetime | None = None) -> Iterator[ThreadInfo]`:
  - `InMemorySaver`: iterate `storage.keys()`; read root `""` checkpoints; take `max(checkpoint_ids)`; extract uuid6 timestamp; check pending writes on `"__interrupt__"`; read `metadata`.
  - `SqliteSaver`: query root checkpoints, `max(checkpoint_id)`, metadata JSON column, and check pending writes on `"__interrupt__"`.
- Tests in `tests/test_expiry.py`:
  - (§D.1) `last_active` decoded from uuid6 checkpoint ID matches checkpoint `ts` within milliseconds (pinned for root and subgraph).
  - (§D.2) `threads()` reports `kind`, `user_id`, `run_id`, `last_active`, `idle`, and `paused` across finished session, one-shot run, paused session, and subgraph pause. Parametrized on `InMemorySaver` and `SqliteSaver`.
  - Unsupported checkpointer raises `NotImplementedError` at construction.
- Commit: `feat(expiry): ThreadInfo and ThreadExpiry.threads() across checkpointers`.

### Step 3 — `ExpiryPolicy` Validation and Race-Free Checkpointer Deletion `[ ]`
- Validate `ExpiryPolicy` at construction:
  - Raise `ValueError` if all four parameters are `None`.
  - Raise `ValueError` if any duration `<= timedelta(0)`.
  - Raise `ValueError` if `keep_last < 1`.
- Policy evaluation in `ThreadExpiry`:
  - `paused`: eligible if `idle > (paused_idle_ttl or idle_ttl)`; exempt from `keep_last`.
  - `kind == "run"`: eligible if `idle > (run_ttl or idle_ttl)`; exempt from `keep_last`.
  - `kind == "session"` and not paused: eligible if `idle_ttl is not None and idle > idle_ttl`, or if rank in user's active session list > `keep_last` (`user_id=None` forms one group).
- Checkpointer deletion helper:
  - Internal `_delete_thread_locked(cp: Any, locked: LockedThread) -> None` taking `LockedThread`.
  - On `InMemorySaver`: delete from `storage`, `writes`, unreferenced `blobs`.
  - On `SqliteSaver`: inside `cp.lock` and `BEGIN IMMEDIATE`, re-verify checkpoint id, delete from `checkpoints` and `writes`, commit.
- Implement `ThreadExpiry.run(*, now: datetime | None = None, dry_run: bool = False) -> ExpiryReport`:
  - Scan candidates.
  - If `dry_run=True`: populate `expired` without locking or deleting.
  - For each candidate:
    - Attempt `thread_lock(checkpointer, thread_id)` non-blockingly; if `SessionBusy`, add to `skipped_busy` and continue.
    - Under `locked: LockedThread`: re-read latest root checkpoint ID. If mismatch or absent, add to `skipped_changed` and continue.
    - Call `_delete_thread_locked(checkpointer, locked)`.
- Tests in `tests/test_expiry.py` (§D.3):
  - Policy validation errors (empty policy, non-positive TTLs, `keep_last < 1`).
  - Single limit alone (`idle_ttl`, `keep_last`, `paused_idle_ttl`, `run_ttl`).
  - Two limits at once (`idle_ttl` + `keep_last`).
  - `keep_last` per user, and the shared group without a user (`user_id=None`).
  - Paused thread follows `paused_idle_ttl` alone and is exempt from `keep_last`.
  - Run follows `run_ttl`.
  - Thread with a turn in progress (`BlockingLLM`) is skipped (`skipped_busy`) and its turn completes intact.
  - Thread resumed/changed after scan is skipped (`skipped_changed`).
  - Another thread is untouched.
  - `dry_run=True` deletes nothing.
  - Unsupported checkpointer raises.
- Commit: `feat(expiry): ExpiryPolicy and race-free checkpointer deletion`.

### Step 4 — Episode Status Lifecycle & Recovery Sweep `[ ]`
- Update `src/vsigma/episodes.py`:
  - `EPISODE_STATUS_NS = "episodes/status"`.
  - `FINAL_EPISODE_STATUSES = frozenset({"done", "failed", "abandoned"})`.
  - `record_episode_status(store: MemoryStore, run_id: str, status: str, locked: LockedThread | None = None) -> None`:
    - If existing status in `FINAL_EPISODE_STATUSES`: do not overwrite (if overwritten by finished run, log warning).
    - Otherwise write status with `updated_at`.
  - `get_episode_status(store: MemoryStore, run_id: str) -> str | None`.
  - `list_episode_statuses(store: MemoryStore) -> dict[str, str]`.
- Wire status transitions in `Agent.run` and `Run`:
  - Inside `with thread_lock(...) as locked`:
    - Write order at start: first `record_episode`, then `record_episode_status(memory, rid, "running", locked=locked)`.
    - Around `_stream_run`:
      - If exception raised during run: record `failed` before re-raising.
      - On return: if `run.interrupted`, record `escalated`; else record `done`.
      - If `session_id is not None and abandon`: record the prior run `abandoned` *after* `_stream_run` returns.
  - In `Run._resume_locked`:
    - Does NOT write `running`.
    - If exception raised during resume: check if thread still holds the pause (`snapshot.interrupts`). If it does, leave `escalated`; if not, record `failed`.
    - On return: if `self.interrupted`, record `escalated`; else record `done`.
  - In `Session.end()` and `Run.end()`:
    - Under `locked`: delete thread first, then if `memory is not None`, record latest `run_id` as `abandoned` (if not already final).
- Wire store sweep in `ThreadExpiry.run(*, now=None, dry_run=False)`:
  - If `store is not None`:
    - For each thread deleted by expiry: record its `run_id` as `abandoned` (if `not dry_run`).
    - Sweep all unfinished episodes (`running` or `escalated`) from `episodes/status`:
      - Look up `owner = episode_owner(store, run_id)`.
      - If no owner entry found in `episodes/index`: skip and log warning.
      - Resolve `thread_id = owner.session_id or owner.run_id`.
      - Attempt `with thread_lock(checkpointer, thread_id) as locked`: if `SessionBusy`, skip.
      - Under `locked`:
        - If `running`: lock is free $\rightarrow$ process died $\rightarrow$ add to `recovered_episodes`, mark `abandoned` (if `not dry_run`).
        - If `escalated`: check 3 cases: (1) thread is gone, (2) thread's latest root checkpoint has `run_id != episode.run_id`, or (3) thread is no longer paused $\rightarrow$ add to `recovered_episodes`, mark `abandoned` (if `not dry_run`).
- Tests in `tests/test_expiry.py` (§D.4 + review items):
  - Normal turn status: `running` -> `done` (or `escalated`).
  - First turn that died before first checkpoint (`episodes/index` + `status="running"`, no checkpoint, free lock) recorded `abandoned` by next `run()`.
  - Episode of a turn in progress (holding lock) is left `running`.
  - Expiry deletes a paused thread, and the same `run()` records that run `abandoned`.
  - Escalated run whose session has since run another turn is recorded `abandoned`, while one still holding its pause is left alone.
  - Resume never writes `running`.
  - Resume that raises leaves episode `escalated` if the pause is still held on the thread.
  - `dry_run=True` writes no status to store.
  - A finished run that encounters an already-final status logs a warning and does not overwrite.
- Commit: `feat(expiry): episode status lifecycle and unfinished episode recovery sweep`.

### Step 5 — Documentation, Roadmap Updates, and Phase 4.12 Tag `[ ]`
- Update `docs/architecture_design.md`:
  - §8 Scope: document per-rung lifespans, operational namespaces (`episodes/status`), and the two entry points.
  - §4 Runtime: document `ThreadExpiry`.
- Update `docs/upgrading.md` with a `v0.2.3` section detailing `ThreadExpiry`, `ExpiryPolicy`, and `episodes/status`.
- Update `docs/getting_started.md` session lifecycle section with `ThreadExpiry` example.
- Update `CHANGELOG.md` under `## Unreleased`.
- Export `ThreadExpiry`, `ExpiryPolicy`, `ExpiryReport`, `ThreadInfo`, `ExpiredThread` from `src/vsigma/__init__.py`.
- Mark `docs/proposals/thread-expiry.md` **GRADUATED (Phase 4.12)**.
- Update `vdesign/plan.md` marking Phase 4.12 `[x]`.
- Collapse `vdesign/active_plan.md`.
- Update `vdesign/checkpoint.md`.
- Run full gate: `uv lock --locked && uv run ruff check && uv run mypy && uv run pytest`.
- Create annotated tag `phase-4.12`.
- Commit: `docs: complete Phase 4.12 thread expiry and episode status recovery`.
