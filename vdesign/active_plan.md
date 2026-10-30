# Active Plan — Phase 4.12: Thread Expiry [x]

**Status:** Completed 2026-10-30.

All 5 steps implemented, verified, and passing all 274 test cases across `tests/`:
1. **Step 0:** Decision Check Point verdicts recorded for `thread-expiry.md` (ACCEPTED) and `scope-lifespans.md` (OPEN).
2. **Step 1:** Module layout cleanup & proof-of-lock: extracted `src/vsigma/threads.py` with `LockedThread` proof-of-lock, re-exports, and `prune_checkpoints`.
3. **Step 2:** `ThreadInfo` and zero-deserialization scan in `ThreadExpiry.threads()` across `InMemorySaver` and `SqliteSaver`.
4. **Step 3:** `ExpiryPolicy` validation, race-free checkpointer deletion under `thread_lock`, and `ThreadExpiry.run()`.
5. **Step 4:** Operational status lifecycle in `episodes/status` and unfinished episode recovery sweep in `ThreadExpiry(store=store).run()`.
6. **Step 5:** Docs updated (`architecture_design.md`, `upgrading.md`, `getting_started.md`, `CHANGELOG.md`), exports added to `__init__.py`, `thread-expiry.md` graduated, roadmap updated, local tag `phase-4.12`.
