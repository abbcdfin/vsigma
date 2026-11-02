# Active Plan — Phase 4.17: Session Admission

*Design: [`docs/proposals/session-admission.md`](../docs/proposals/session-admission.md).*

## Steps

- [x] **1. `SessionDenied`, owner check, and `sessions/owners` (`src/vsigma/episodes.py`, `src/vsigma/run.py`, `src/vsigma/sessions.py`, `src/vsigma/expiry.py`, `src/vsigma/__init__.py`)**
  - Add `SESSIONS_OWNERS_NS = "sessions/owners"` in `src/vsigma/episodes.py`.
  - Add `SessionDenied(RuntimeError)` carrying `session_id` and refused `user_id` (never the owner), exported from `vsigma`, `vsigma.run`, and `vsigma.sessions`.
  - Implement `_require_owner(session_id, user_id, snapshot, store)`:
    - When `snapshot.metadata is not None`: if `"run_id" in snapshot.metadata or "user_id" in snapshot.metadata`, check `snapshot.metadata.get("user_id") == user_id` (LangGraph omits `None`-valued keys when serializing checkpoint metadata, so an open vsigma turn has `"run_id"` and no `"user_id"`, whereas a checkpoint written by `graph.update_state` has neither `"run_id"` nor `"user_id"` and passes).
    - When `snapshot.metadata is None` and `store is not None`: read `sessions/owners/<session_id>`; if not `None` and `!= user_id`, raise `SessionDenied(session_id, user_id)`.
  - Call `_require_owner` in `Agent.run` (first under `thread_lock` after `complete_forget_locked` and `get_state`, before `_require_context` and `SessionPaused`) and in `Session.snapshot`.
  - In `Session.end`: on plain `end()` (`not forget`) of an owned session (`metadata.get("user_id")` is not `None`) when `store is not None`, write `sessions/owners/<session_id> = user_id` under `thread_lock`.
  - In `forget_session_locked` and `complete_forget_locked`: delete `sessions/owners/<session_id>` before deleting `sessions/forget/<session_id>`.

- [x] **2. Prior context from same owner only (`src/vsigma/consolidation.py`, `src/vsigma/memory_pass.py`, `src/vsigma/maintenance.py`)**
  - Always filter `candidate_eps` by `ep.user_id == this_owner.user_id` in `episode_trajectory` and remove the `only_owner` parameter from `episode_trajectory` and `_consolidate_batch`.

- [x] **3. Existing tests & Phase 4.17 verification (`tests/`)**
  - Update existing tests that intentionally changed `user_id` or read an owned session with `user_id=None` or passed `only_owner`, recording each one.
  - Add parameterized tests (`InMemorySaver` and `SqliteSaver`) covering all cases in `docs/proposals/session-admission.md` §F.

- [x] **4. Docs (`docs/getting_started.md`, `docs/upgrading.md`, `docs/architecture_design.md` §5, `CHANGELOG.md`, `vdesign/plan.md`, `vdesign/checkpoint.md`)**
  - Update docs and run full verification gates (`ruff`, `mypy`, `pytest`, `uv lock --locked`).
