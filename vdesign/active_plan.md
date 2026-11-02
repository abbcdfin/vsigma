# Active Plan — Phase 4.16: Forgetting Session Memory

*Design: [`docs/proposals/scope-lifespans.md`](../docs/proposals/scope-lifespans.md) §C.2, [`docs/proposals/thread-expiry.md`](../docs/proposals/thread-expiry.md) §B.3.2.*

## Steps

- [x] **1. Episode detach & forget record protocol (`src/vsigma/episodes.py`, `src/vsigma/expiry.py`, `src/vsigma/retention_episodes.py`)**
  - Update `src/vsigma/episodes.py` module docstring to document that `session_id` in `episodes/index` is set to `None` when a session is forgotten (§C.2 step 4), the one exception to immutable ownership.
  - Add `SESSIONS_FORGET_NS = "sessions/forget"` and `detach_session_episodes(store, session_id)` in `src/vsigma/episodes.py`.
  - In `ThreadExpiry.__init__`, raise `TypeError` when `store is not None` and not a `MaintainableStore`.
  - Add `forgotten_sessions: list[str] = field(default_factory=list)` to `ExpiryReport`.
  - Implement crash-safe, idempotent 5-step session forgetting (`forget_session_locked`) and pending-record completion (`complete_forget_locked`), recording paused runs as `"abandoned"` right after thread deletion and before detaching episodes.
  - Ensure `EpisodeRetention` treats detached episodes without `user_id` (`user_id=None, session_id=None`) as unowned (nothing will consolidate them, so they are prunable even when consolidation is in use).

- [x] **2. Runtime & session wiring (`src/vsigma/expiry.py`, `src/vsigma/run.py`, `src/vsigma/sessions.py`)**
  - `ThreadExpiry.run`: complete all leftover `sessions/forget` records first under each thread's lock, then expire threads (using the 5-step forget sequence for session threads when `store` is configured), and sweep unfinished episodes. Respect `dry_run=True` (write and delete nothing).
  - `Agent.run`, `Session.resume`, `Run._resume_locked`, and `Session.end`: complete any pending forget record for `session_id` under `thread_lock` before reading thread state.
  - `Session.end(*, abandon: bool = False, forget: bool = False)`: support `forget=True` (refusing paused sessions without `abandon=True`, working after a plain `end()`, degrading to plain `end()` when `memory is None`, and raising `TypeError` when `memory` is not a `MaintainableStore`).

- [x] **3. Verification (`tests/test_expiry.py`)**
  - Parameterized on `InMemorySaver` and `SqliteSaver` covering all cases in `docs/proposals/thread-expiry.md` §D step 7 and watch-fors:
    - Failure after thread delete completed by next `expiry.run()`.
    - Failure before thread delete completed by next turn (`send`/`run`), starting with no thread and no old memory (plus `resume` and `end`).
    - Stale forget record whose thread moved on is dropped and deletes nothing.
    - Session ID reused before completion starts with no old memory (`RecallContext`).
    - Reused session ID turn has none of the old turns in its `episode_trajectory` distill source.
    - Memory pass writes nothing back to `session:<id>/knowledge` for forgotten sessions; detached episode with `user_id` consolidates to `persona(user_id)`; detached episode without `user_id` is skipped as unowned and prunable by `EpisodeRetention`.
    - `dry_run=True` writes and deletes nothing.
    - `Session.end(forget=True)` refuses paused session without `abandon=True`, records paused run `"abandoned"` before detaching when `abandon=True`, and works after a plain `Session.end()`.
    - Repeating `complete_forget_locked` after interruption at each step is idempotent.

- [x] **4. Docs (`docs/getting_started.md`, `docs/upgrading.md`, `docs/architecture_design.md` §8, `vdesign/plan.md`, `vdesign/checkpoint.md`)**
  - Update docs and run full check gates (`ruff`, `mypy`, `pytest`, `uv lock --locked`).

