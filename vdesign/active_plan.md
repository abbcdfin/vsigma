# Phase 4.15 — Run Context: One Agent, Many Sessions

**Status:** BUILT (trial migration pending)
**Design:** [`docs/proposals/run-context.md`](../docs/proposals/run-context.md) (ACCEPTED 2026-11-01, target `v0.3.1`)

---

## Plan & Progress

- [x] **Step 1: Plumbing `run_context`**
  - Add `run_context: Any = None` to `Agent.run` and `Agent.session`.
  - Pass through `Session(run_context=...)` and `Run(run_context=...)`.
  - Pass `run_context` from `Run` to `_stream_run(..., run_context=...)`.
  - `_stream_run` passes `context=run_context` to `graph.stream(...)`.

- [x] **Step 2: Context Retrieval & Exception**
  - Define `class NoRunContext(RuntimeError): ...`.
  - Implement `run_context() -> Any`: reads `get_runtime().context`, raises `NoRunContext` when `None`, outside a run, or on unmanaged threads.
  - Export `run_context` and `NoRunContext` from `vsigma` and `vsigma.run`.

- [x] **Step 3: `react_node` Fail-Closed Exception Handling**
  - In `src/vsigma/react.py:76`, re-raise `NoRunContext` before catching general `Exception`.

- [x] **Step 4: Checkpoint Metadata Guard**
  - Record `has_context = run_context is not None` in checkpoint metadata (never the object).
  - In `Agent.run`: right after reading snapshot, if `metadata.get("has_context") and run_context is None`, raise `NoRunContext` before `SessionPaused` check, `abandon`, or episode recording.
  - In `Run._resume_locked`: check `has_context` first. If `metadata.get("has_context") and self.run_context is None`, raise `NoRunContext`. Copy `has_context` in tags.

- [x] **Step 5: Session Docstring Update**
  - Update `Session` docstring in `src/vsigma/sessions.py` to document that the handle holds the process-local `run_context` object while thread state is read from checkpoint snapshot.

- [x] **Step 6: Comprehensive Tests**
  - Test 1: Context reaches tool, hand-built node, router, ContextManager, subgraph, and post-resume step.
  - Test 2: Two sessions on one Agent isolated under concurrent execution with `threading.Barrier`.
  - Test 3: Tool reading with no object raises `NoRunContext`, LLM called once, no tool message, episode failed.
  - Test 4: `run_context()` raises outside run and on spawned thread.
  - Test 5: Resume with no object refused, session remains paused; new handle with renewed object finishes turn.
  - Test 6: `send` and `send(abandon=True)` with no object on guarded session refused, no new episode recorded, old unchanged.
  - Test 7: Guard holds after `checkpoints="latest"` prunes.
  - Test 8: Secret in object absent from SQLite saver and PersistentStore files (including `-wal`).
  - Test 9: One Agent on persistent store with multiple sessions: `maintain()` doesn't raise; `send` raises `SessionBusy` while lock held and succeeds once released.
  - Test 10: Backward compatibility: agent with no `run_context` runs, pauses, and resumes unchanged.

- [x] **Step 7: Documentation Updates**
  - `docs/getting_started.md`: One-agent shape, when to skip `run_context`, retry `SessionBusy`, never write secrets to state/memory, derive session IDs from authenticated user, avoid `TrajectoryLog` in long-lived agents, `run_end` hook limits.
  - `docs/upgrading.md`: `v0.3.1` notes.
  - `CHANGELOG.md`: `## Unreleased` entry.
  - `docs/architecture_design.md`: Note on run context and one-agent shape.
  - Update `vdesign/plan.md` (ticked steps 1-3, left step 4 unticked, phase `[ ]`) and `vdesign/checkpoint.md`.
