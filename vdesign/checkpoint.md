# Checkpoint

> Volatile session-handoff memory (per `.vibe/vibe.md`). For the durable roadmap see
> [`plan.md`](./plan.md); for settled design see `docs/`. Last updated: **2026-10-29**.

## Phase 4.8 completed (2026-10-25)

- **What:** Phase 4.8, the thread-native runtime. **COMPLETED 2026-10-25** across all three slices + review passes:
  - **Slice 1 (Append-only messages & fail-loud reducer):**
    - `append_messages` reducer with a fail-loud guard (content-based at first; object identity since review, §G.11).
    - `react_node`, `critic_node`, `human_interrupt_node`, and `plan_node` return message deltas only.
    - Initial prompt written as `input:<uuid4>` in `episode(run_id)/context`.
    - `episode_trajectory` concatenates rows without cumulative duplication; `REFLEXIVE_WINDOW` cap lives in `reflexive_consolidator`.
    - View-only context proved with `SlidingWindowContext`.
  - **Slice 2 (Session = Thread):**
    - `thread_id = session_id or run_id`, tagged with metadata (`run_id`, `task_id`, `user_id`, `channel`).
    - Stateless `Session` projection reading checkpointer snapshot directly (`graph.get_state()`).
    - Verb split: `send` raises `SessionPaused` if thread is paused at an interrupt (unless `abandon=True`); `resume` continues the run.
    - `Run` reads `interrupted`, `interrupt`, and `status` from `snapshot.interrupts`.
    - System prompt moved to `state["system"]` on every turn.
    - Durability defaults to `"exit"`.
    - `episode_trajectory` prepends prior turns from the same session for distillation.
  - **Slice 3 (Optional Memory & Interceptor rename):**
    - `Services` renamed to `Interceptor` with `kw_only=True` (`src/vsigma/interceptor.py`).
    - `memory: MemoryStore | None = None` default across templates and agents (zero external store writes on pure thread checkpointing).
    - Fail-loud store guards: `RecallContext.assemble` raises `ValueError` if `memory is None`; `record_episode` / `episode_trajectory` require store.
    - Wording sweep: "the seam" replaced with *interception points* (preserving LLM seam in `llm.py`).
    - Full docs updated in `architecture_design.md` §§3-5, 11, `README.md`, `getting_started.md`, and `docs/upgrading.md`.
  - **Review Fixes:**
    - Dropped compatibility aliases (§G.8): `Services`, `seam.py`, `Agent.services`, `services=` dropped completely.
    - `durability` typed to `Durability` (`"sync" | "async" | "exit"`).
    - Turn result reset between turns (`"result": None` on turn start).
    - Paused check moved into `Agent.run` (`SessionPaused` raised with `abandon=True` escape hatch).
    - Resume keeps tags (`run_id`, `task_id`, `session_id`, `user_id`, `channel`).
    - Run snapshot targeting: `Run.snapshot` queries checkpoint history by `run_id`, refusing resume on abandoned runs.
    - Distillation session context ordering & sizing: strictly ordered by insertion index, filtering tool noise.
    - Stream-driven trajectory observability (§G.9): events emitted from LangGraph execution stream.
    - Context module cleanup: explicit `memory: MemoryStore | None` arg on `assemble`, dead system prompt handling removed.
    - Architectural rationale comments restored in `interceptor.py`, `agents/__init__.py`, `run.py`.
    - Opt-in observer (§G.10): `Interceptor.observer: Observer | None = None` default, avoiding unbounded memory leaks.
    - Reducer guard switched to object identity (§G.11); `Session.resume` carries `recursion_limit`.
    - `Run.snapshot` raises when no checkpoint matches run; `Run.trajectory` raises unless observer is a `TrajectoryLog`.
    - `dialog_turns` lives in `critique.py` beside `CRITIQUE_MARKER`, imported at the top under its own name; doc gaps filled in `upgrading.md` and `architecture_design.md`, the web-app notes point at real lines.
    - Subgraph limit documented (§G.11, `upgrading.md`): a subgraph node sharing `messages` raises if it runs straight through and silently duplicates history if it pauses inside.
    - `architecture_design.md` §4 now records the messages channel; leftover "the seam" wording and `SlidingWindowContext`'s removed system-prompt pinning corrected.
  - **Gate Status:** 181 tests passing; `ruff` and `mypy --strict` 100% clean over 60 source files.
  - **Artifacts:** `docs/proposals/thread-native-runtime.md` marked **GRADUATED**; `session-dialog-compaction` marked **SUPERSEDED**; `episode-retention` prerequisites updated. Review spawned `docs/proposals/langgraph-messages.md`, accepted as Phase 4.9.

## Phase 4.9 completed (2026-10-25)

- **What:** Phase 4.9, LangGraph messages (`docs/proposals/langgraph-messages.md`). **COMPLETED 2026-10-25**:
  - `ReActState.messages` migrated from `append_messages` to LangGraph's native `add_messages` (`langchain_core.messages.AnyMessage`).
  - Messages carry unique ids and merge by id; returning cumulative history updates in place without duplicating.
  - Subgraphs sharing `messages` nest cleanly as nodes, whether running straight through or paused at `interrupt()`.
  - Boundaries stay plain OpenAI dicts: the `LLM` protocol, memory capture rows, and `Session.dialog`.
  - `wrap` strips received window messages, preventing context injections from persisting to the thread.
  - `vsigma.state.append_messages` and its fail-loud guard are completely removed.
  - `langchain-core>=1.4` declared as direct dependency in `pyproject.toml`.
  - Gate status: 186 tests passing; `ruff` clean; `mypy --strict` 100% clean over 61 source files.
  - Proposal `docs/proposals/langgraph-messages.md` graduated.

## Phase 4.10 completed (2026-10-26)

- **What:** Phase 4.10, thread retention (`docs/proposals/thread-retention.md`). **COMPLETED 2026-10-26**:
  - `keep_latest(graph, thread_id)`: keeps root latest checkpoint when unpaused and all namespace latest checkpoints when paused. Deletes unreferenced blobs on `InMemorySaver`; atomic SQLite transaction under `cp.lock` in `BEGIN IMMEDIATE` for `SqliteSaver`. Delegates to `prune` where implemented (Redis). Unsupported checkpointers or `DeltaChannel` raise.
  - `checkpoints="latest"`: opt-in on `Agent` and the four prebuilt templates (`make_react_agent`, `make_guarded_react_agent`, `make_plan_execute_agent`, `make_reflexion_agent`). Prunes old checkpoints after each turn and resume (after run-end hooks). Support validated at construction.
  - `retain_messages=N`: opt-in on templates; adds plain `"retain"` node emitting `RemoveMessage(id=...)` on `done` turns. Construction check enforces `retain_messages >= SlidingWindowContext.max_messages`.
  - `Session.end()` and `Run.end()`: delete thread from checkpointer (`delete_thread`). Refuse paused threads unless `abandon=True`. Memory store contents (transcripts, summaries, knowledge) survive.
  - Review fixes: input validation on `retain_window(max_messages < 1)` and `Agent(checkpoints=...)`, unified `_pruner` checkpointer dispatch and `isinstance(ch, DeltaChannel)`, dropped dead dict branches in `window` and `retain_window`, direct `delete_thread` calls, strengthened subgraph namespace test under LangGraph's default durability, and an optional `DeltaChannel` import so `import vsigma` works on a LangGraph without it.
  - Gate status: 226 tests passing; `ruff` clean; `mypy --strict` 100% clean over 63 source files.
  - Proposal `docs/proposals/thread-retention.md` graduated.

## Phase 4.11 completed (2026-10-29)

- **What:** Phase 4.11, thread concurrency (`docs/proposals/thread-concurrency.md`). **COMPLETED 2026-10-29**:
  - Per-`(checkpointer, thread_id)` busy-thread registry (`thread_lock` in `src/vsigma/retention.py`), keyed by checkpointer instance (`WeakKeyDictionary`) so all `Agent`s sharing one checkpointer share the same per-thread lock state, and shrinking to empty when operations finish.
  - Non-blocking lock acquisition across `Agent.run` (including the `SessionPaused` check, episode recording, `_stream_run`, `_record()`, and `"latest"` pruning), `Session.resume`, `Run.resume`, `Session.end()`, `Run.end()`, and direct `keep_latest`; raises `SessionBusy(thread_id)` (`RuntimeError` subclass) at once when busy.
  - LangGraph's `Interrupt.id` exposed as `Session.interrupt_id`, `Run.interrupt_id`, and `SessionPaused.interrupt_id`.
  - Optional `interrupt_id: str | None = None` on `Session.resume` and `Run.resume`: compared under the thread lock; raises `PauseChanged(interrupt, interrupt_id)` (`RuntimeError` subclass carrying the now-pending pause, if any) on mismatch or when no pause is pending.
  - Capped `mcp>=1.28.1,<2` in `pyproject.toml` and `uv.lock`.
  - Review fixes: registry shrinks on completion and skips locking when `checkpointer is None`; `getting_started.md` saves `qid = session.interrupt_id` when showing the pause; `upgrading.md` restores the `v0.2.0` warning under `Releases` and fixes cross-references.
  - Gate status: 234 tests passing; `ruff` clean; `mypy --strict` 100% clean over 64 source files.
  - Proposal `docs/proposals/thread-concurrency.md` graduated.

## Phase 4.12 completed (2026-10-30)

- **What:** Phase 4.12, thread expiry and episode status recovery (`docs/proposals/thread-expiry.md`). **COMPLETED 2026-10-30**:
  - `src/vsigma/threads.py`: extracted `LockedThread`, `SessionBusy`, and `thread_lock` with proof-of-lock semantics; `retention.prune_checkpoints` requires `LockedThread`.
  - `ThreadExpiry.threads()`: scans checkpointer threads on `InMemorySaver` and `SqliteSaver` without deserializing checkpoint state blobs; timestamp extracted from UUIDv6 checkpoint id.
  - `ExpiryPolicy`: declarative limits (`idle_ttl`, `keep_last` per user, `paused_idle_ttl`, `run_ttl`).
  - Race-free deletion: deletion holds `thread_lock`, re-verifies latest checkpoint id under lock, and atomically deletes thread across all checkpoints, writes, and unreferenced blobs.
  - Episode status lifecycle: operational namespace `episodes/status` tracks `"running"`, `"escalated"`, `"done"`, `"failed"`, `"abandoned"` under `thread_lock`. Final statuses never overwritten.
  - Recovery sweep: `ThreadExpiry(store=store).run()` recovers dead runs as `abandoned` when lock is free, and escalated runs when the thread is gone, no longer holds pause, or latest run is another.
  - Gate status: 274 tests passing; `ruff` clean; `mypy --strict` 100% clean over 67 source files.
  - Proposal `docs/proposals/thread-expiry.md` graduated.

## Next Steps

1. **Finish publishing `v0.2.2`.** Published 2026-10-29: GitHub and GitLab both carry `dev`,
   `master` and the tag `v0.2.2` at the catch-up commit, one commit on top of GitHub's previous
   `dev`. GitLab keeps the private line on `archive/private-history`, plus `release/0.1` and the
   old release tags. Left to do:
   - [ ] Upload to PyPI by hand, from a machine that reaches it: TestPyPI first, then PyPI
     (`uv publish`, built from the tag).
   - [ ] GitLab: switch "Allowed to force push" back off on `dev` and `master`.
   - [ ] The developer resets their clone once: `git fetch gitlab && git checkout dev &&
     git reset --hard gitlab/dev`, after checking nothing is unpushed.
   - [x] This repo's git identity: decided 2026-10-30. Commits carry
     `abbcdfin <abbcdfin@placeq.com>` (repo-local config); the package author and the
     `LICENSE` holder are `abbcdfin` from the next release.

   From now on everything committed is public: no consumer internals, hosts or local paths in
   files, commit messages or `vdesign/`. Every commit goes to both remotes. GitHub gets
   branches and new release tags only, each named in the push: never `--tags`, `--mirror`, the
   old release tags or the archive branch, whose history holds private references.
2. **Consumers.** Both resolve vsigma to a local checkout through editable installs, so they
   are broken now (`from vsigma import Services` raises). `v0.1.0` is the pin until they migrate.
   - **The web-app consumer: migrate to `v0.2.1`, not `v0.2.0`.** One app-wide checkpointer
     plus a per-connection "busy" guard makes the thread-concurrency race reachable. The user
     forwards the guide, with private notes and two patches that implement it (pin `v0.1.0`;
     migrate to `v0.2.1`), rebuilt 2026-10-29. Verified: its offline suite passes on Python
     3.13 and 3.11 (fresh resolution: langgraph 1.2.18, langchain-core 1.7.1, mcp 1.30.0); both
     new tests fail without the code they guard; no new ruff findings; `uv lock` resolves vsigma
     from the release tag. Its front end must echo `interrupt_id` before the stale-answer check
     takes effect. Decided choices (`upgrading.md`, `v0.2.0`): pin first; one app-wide
     `SqliteSaver` on a persistent volume; a question pending at reopen is dropped; no thread
     deletion until expiry ships.
   - **bumblebee was not trial-migrated**: it is not on this machine. Its notice rests on the
     guide alone.
3. **Review candidates for next Decision Check Point:**
   - `scope-lifespans` (revision 2), the umbrella for bounding what an agent keeps, with its
     components `thread-concurrency` (graduated, Phase 4.11), `thread-expiry` (revision 3; →
     `v0.2.3`) and `episode-retention` (revision 5; retention in the memory pass). The auditor's
     follow-up findings are answered in `d29cf5c`; awaiting their sign-off. Next to write:
     the `BaseStore` adapter proposal (thread-native-runtime §G.7), the foundation for deleting
     memory, including migrating existing `SQLiteStore` data, ordering, listing and deleting.
   - `session-admission` (private by default, explicit sharing via `sessions/index`).
   - `memory-scope-promotion` Slice 2 (`PromotionRequest`, widening scope).
   - Capability extension (`dynamic-tools`, `procedure-extraction`, `agent-authored-tools`).
4. **Live smoke test:** run live tests once LLM auth is provided.

## Where we are

- **Branch:** `dev` and `master` at the `v0.2.2` release, the same commit on GitHub (`origin`,
  public) and GitLab (`gitlab`, private), switched 2026-10-29. GitLab alone keeps the private
  line (`archive/private-history`), `release/0.1` (from `phase-4.7`, holding `v0.1.0`) and the
  release tags `v0.1.0`, `v0.2.0` and `v0.2.1`; GitHub has only `v0.2.2`. Default branches: `dev`
  on GitLab; GitHub's points at the same commit. Phase tags stay local.
  Phase tags: `phase-0`, `phase-1-mvp`, `phase-2`,
  `pre-toolkit-layering`, `phase-2.5`, `templates-react-ladder`, `pre-phase-4`, `phase-4`,
  `phase-4.2`, `phase-4.3`, `phase-4.4`, `phase-4.6`, `phase-4.7`, `phase-4.8`, `phase-4.9`,
  `phase-4.10`, `phase-4.11`.
- **Status:** Phase 4.11 DONE (2026-10-29), **234 tests**; released as `v0.2.1`, then as `v0.2.2`,
  the first public release (2026-10-29). The
  bullets below are the history since Phase 4.
- **History:** **Phase 4 core complete** (2026-07-04) — real memory landed. `make check`
  green (**68 tests**); demo runs at `python -m examples.web_browsing.demo`.
  Since then: `RecallContext` v0 (read-side loop, plan 4.5 head-start);
  **Phase 4.1 complete** — `LoggingLLM` live tap at the LLM protocol seam with
  `enable_llm_logging()`/`disable_llm_logging()` (stdout/file, silent by default);
  **Phase 3 complete** (built after 4, deliberately) — `critic_node` + `Validator`
  (`critique.py`), `make_reflexion_agent` (both routers `break_on_attempts`; never-valid
  → escalate, not spin; backstop 2·limit), worked example `examples/code_kata/`
  (test-running subprocess validator; `python -m examples.code_kata.demo`); validator
  combinators (`all_of`, `llm_judge`; `expect_json` demoted to doc snippet — instances
  live with consumers, contracts in core); **Phase 4.2 complete** — `Session` handle
  (multi-round dialog, rehydrates on SQLite, `send`→`resume` unification, envelope
  metadata; [interaction-model proposal](../docs/proposals/interaction-model.md) fixes
  the session-vs-bus boundary); **run-end hook fires at every terminal edge** (2026-07-14)
  — mechanical episode summary written on escalation too (retries can correlate failed
  attempts), distillation stays `done`-only (rationale in architecture_design §9);
  **Phase 4.3 complete** (2026-07-15) — maintenance pass per the
  [maintenance-pass proposal](../docs/proposals/maintenance-pass.md) (nanobot-inspired):
  `vsigma memory consolidate` (episode markers in `maintenance/state`, batch → bundle
  gate, markers advance only on successful ingest, `--dry-run` review) +
  `vsigma memory export`;
  **Phase 4.4 complete** (2026-07-16) — thinking models at the LLM seam per the
  [thinking-models proposal](../docs/proposals/thinking-models.md) (bumblebee signal):
  `LLMResponse.reasoning` (separate field captured, inline `<think>` stripped — `text`
  always clean, so `distill` can't be broken by transport), `LoggingLLM` `[thought: …]`,
  `extra_body`/`max_tokens` (+ env) for disabling thinking; run-end consolidation
  ladder documented — accepted default for local thinking models is the *distiller*
  (same endpoint, thinking off) so same-day learning survives;
  **Phase 4.6 complete** (2026-07-19) — `AsyncBridge` promoted to public API per the
  [async-bridge proposal](../docs/proposals/async-bridge.md) (ACCEPTED; bumblebee
  Playwright second-consumer trigger): stdlib-only `bridge.py`, `run(timeout=)` cancels
  the coroutine on expiry, `wrap(fn, timeout=120.0)` → sync `Tool.func`, `mcp.py`
  refactored onto it (`_AsyncBridge` deleted), getting_started recipe naming the
  `asyncio.run()`-per-call trap.
- **Web-app dogfooding pass complete** (2026-07-23) — resolved 6 high-friction items from a live-model consumer's feedback log:
  - **Item 1 (JSON repair at LLM seam):** `OpenAICompatibleLLM` catches `JSONDecodeError` on tool args and feeds error observation back to graph so model self-corrects.
  - **Item 2 (Tool error boundary):** `react_node` wraps `tool.run` in `try/except Exception` to return exception observations rather than crashing the graph.
  - **Item 3 (Terminal tools):** `return_direct=True` on `Tool` / `@tool` / `from_langchain` immediately finishes the run with `status="done"` upon successful execution, saving extra LLM turns.
  - **Items 4 & 5 (First-class LLM knobs):** `temperature` and `tool_choice` exposed on `OpenAICompatibleLLM` and `default_llm()` (env `VSIGMA_LLM_TEMPERATURE`, `VSIGMA_LLM_TOOL_CHOICE`).
  - **Item 7 (Schema normalization):** `normalize_tool_schema` hoists nested Pydantic `$defs`/`definitions` to parameters root in `Tool.spec` to prevent `llama.cpp` 400 errors.
  - **Item 12 (Injectable checkpointer):** template builders (`make_*_agent`) accept `checkpointer: BaseCheckpointSaver[Any] | None = None` (defaults to `MemorySaver()`).
  - **Item 16 (Thread-safe SQLiteStore):** `SQLiteStore` uses `check_same_thread=False`, `threading.Lock()`, and `PRAGMA journal_mode=WAL` for web service threadpools.
  - **Item 17 (Clean JSON in assistant tool_calls history):** `react_node` ensures `function.arguments` in history assistant turns is always valid JSON (`"{}"` on error) so strict servers don't HTTP 400 on turn 2, while including the raw argument string excerpt in the tool observation for LLM self-correction.
  - **Follow-up (LoggingLLM raw_decode salvage):** `LoggingLLM` recovers tool arguments from duplicated/concatenated JSON via `json.JSONDecoder.raw_decode`, with detailed parse-error logging; regression tests cover the salvage.
  **127 tests.**
- **Phase 4 modules:** `episodes.py` (ownership index — Phase 4.7), `scope.py` (ladder +
  visibility + `recall`), `records.py`
  (`MemoryRecord`/`Provenance`), `stores.py` (`SQLiteStore`), `construction.py` (`distill`),
  `bundle.py` (write/validate/ingest gate), `cli.py` (`vsigma memory validate|ingest`),
  `consolidation.py` (reflexive hook via `Interceptor.run_end` + `deep_consolidate`),
  `SlidingWindowContext` in `context.py`. Bundle schema:
  `docs/schemas/memory-bundle.schema.json` (v0.1, drift-pinned by test).
- **Phase 4 deferred (recorded in plan.md):** semantic index/embeddings, vector adapters,
  strategy library, source-hash prune, recall-injecting context assembly.

## Architecture as of now (important — changed in 2.5)

- **vsigma augments LangGraph, doesn't replace it.** `GraphBuilder` was **removed**.
- **Two levels:**
  - *Templates* (fast path; return an `Agent` whose `.graph` is a real compiled graph) —
    a graceful-degradation ladder: `make_react_agent` (flat baseline) →
    `make_guarded_react_agent` (+ human-interrupt) → `make_plan_execute_agent` (+ planning).
  - *Primitives* — `wrap`/`Interceptor` (interception points: node→node) on a raw
    `StateGraph`; `add_wrapped`/`add_router` smooth LangGraph's strict typing; run via
    `Agent` → `Run`.
- **Modules:** `interceptor.py` (Interceptor/wrap/add_wrapped/add_router; `seam.py` until
  Phase 4.8), `run.py` (Agent + Run),
  `agents/` (templates). `builder.py` deleted.
- Tools: `Tool` is the spine; sources adapt in (`tool()`, `from_langchain`, `make_mcp_tools`).
- Offline testing: `FakeLLM` + `call`/`say`; `StaticLLM` always-finish stub.

## Key context

- **LLM is OpenAI-compatible, auth still pending** (`default_llm()` from `VSIGMA_LLM_*`;
  offline default `FakeLLM`). Not yet smoke-tested against a live model/web.
- **Dogfooding:** bumblebee (feedback in its `docs/vsigma_feedback.md`) is the consumer. Its 2 logged items are resolved (LangChain tool
  adapter + `react_node` fail-fast; offline `FakeLLM` discoverability/helpers).
  - **3rd item (code-edit signal, not logged):** bumblebee patched the templates so
    `recursion_limit` scales with `limit` (was hardcoded 25 → loop-breaker couldn't escalate at
    high `limit`, raising `GraphRecursionError`). Resolved upstream the right way: all three
    templates now take an explicit `recursion_limit` override; guarded/plan default to a
    documented `_backstop(limit)` (`limit + 20`, per-`invoke` sizing since resume resets the
    counter); flat exposes the knob too (its only bound). +3 tests (30 total green).
  - **4th item (code-edit signal, 2026-07-14):** bumblebee patched `episode_trajectory`
    in place — real O(N²) find (cumulative `messages` reserialized per step) but its fix
    masked all history when the window's last step was a critic update (no `messages`).
    Landed corrected upstream: keep the last step that *has* messages; reflexion
    regression test added. Pattern note: second in-place patch instead of logged
    feedback — worth reinforcing the feedback-file channel with agy.
- **Proposals (`docs/proposals/`):** consolidation pass done (2026-07-04) — the settled
  *how* now lives in `docs/architecture_design.md` (⚙ implemented / ✎ design intent).
  **GRADUATED:** `toolkit-layering` (§2), `composition-model` (§§3–4; GraphBuilder part
  superseded), `mcp-tool-adapter` (§7), `running-context-model` (§5),
  `memory-construction` **core** (§§8–9). Still live proposal material: the memory outer
  ring (semantic index, vector adapters, strategy library, source-hash prune, cross-store
  merge) inside `memory-construction.md`. **ACCEPTED + implemented:** `async-bridge` (§4.6).
- **Open DRAFT proposals (awaiting review, none implemented):**
  - *Capability extension (autonomy ladder):* `dynamic-tools` (foundation — `ToolSelector`,
    gated `ToolCatalog`, `load_skills` SKILL adapter), `procedure-extraction` (rung 2 —
    instruction-only SKILL.md from trajectories, mechanical gate), `agent-authored-tools`
    (rung 3 — skill pipeline; author never operates the gate). Shared: staging→gate→admit,
    mirroring the memory bundle. Review order: dynamic-tools first (others depend on it).
    Open Qs: default-closed gate posture for bumblebee; ToolCatalog own module vs.
    tools.py; procedure-extraction CLI verb naming.
  - *Context economics (two focused proposals, both feed 4.5 measured assembly):*
    `session-dialog-compaction` (bound what a Session stores/replays — opt-in
    `dialog_policy`, raw trajectory + distilled residue survive) and `token-budget-context`
    (bound what we send per call, at the assembly seam — app-set budget, `usage` on
    `LLMResponse`, char/usage-calibrated estimation, no tokenizer dep).
    **`session-dialog-compaction` is superseded at Phase 4.8's close**: its replayed dialog no
    longer exists.
  - *Session admission* (`session-admission`, new 2026-09-10) — from a bumblebee dogfooding
    review of Phase 4.7. A `session_id` is a **bearer capability**: no participant list exists,
    so a second persona naming someone else's session reads its dialog, turn envelopes,
    episode summaries and (since 4.7) distilled knowledge. Reproduced on a clean store.
    `architecture_design` §5 claimed `visible_scopes`/`recall` enforced "session → its
    participants" — **that claim is now corrected**, and the ✎ note there says plainly that a
    host must treat `session_id` as a secret and scope it per principal until this lands
    (bumblebee's composite `(user_id, session_id)` is the right interim move). Two useful
    findings: the turn envelope already records the author, so the check's data is durable and
    merely unread; and `visible_scopes(state)` takes no store, so it could *never* have
    enforced a durable list — only `recall` is positioned to. Not a 4.7 regression (dialog has
    been session-scoped since 4.2), but 4.7's §A.4 argument about `AGENT` — "`visible_scopes`
    returns it for every persona" — applies one rung down, which is how it was missed.
    Design: `sessions/index` (`session_id → owner, visibility, participants, created_at`),
    private by default. The **owner declares a session `private` or `open`** (user's addition,
    2026-09-10) — visibility is *policy*, participants is *state*, deliberately two fields
    rather than a magic `participants: ["*"]`; it also makes the unowned-session case explicit
    instead of inferred from a null owner, which was the last fallback-shaped rule left in the
    design.
  - *Episode retention* (`episode-retention`, new 2026-09-06) — came out of asking whether
    `run_id`s accumulate. They do not (uuid4), but their storage does, and nothing in `src/`
    deletes anything. Prune whole episodes keyed by `task_id`, keep-last-N; `run_id` stays the
    only storage key. Trajectory + `deep:` marker deleted, `episodes/index` kept as a
    disposition-stamped tombstone (content hash **mandatory**), knowledge untouched;
    consolidated-only by default; escalated episodes never prunable (a `resume()` would
    recreate the deleted namespace). Revision 2 folded in a review round — see §G.4 for the
    six revision-1 errors withdrawn. Two prereqs: no delete on `MemoryStore` (recommend a
    `SQLiteStore` backend extra, as `namespaces()` already is), and **the deep pass is
    silently capped at 20 steps** — `episode_trajectory` truncates to
    `steps[-REFLEXIVE_WINDOW:]` and `run_maintenance` calls that same function, so a `deep:`
    marker does not mean the episode was processed. That is a Phase 4.3 defect independent of
    retention, and worth fixing on its own. **Phase 4.8 slice 1 fixes it.** Non-goals worth remembering: LangGraph checkpoints are in a store vsigma
    cannot reach and may be the bigger pile, and `seam.py:66` persisted cumulative `messages`
    so one long run grew superlinearly on its own — fixed by Phase 4.8's append-only `messages`.
  - *Memory scope promotion — **Slice 2 only*** (`memory-scope-promotion` §C, revision 2 —
    bumblebee dogfooding review + a second review round). **Slice 1 graduated out of this
    list into Phase 4.7** (see plan.md) because it fixes verified defects rather than adding
    a capability. Key reframe behind both: there is **no record→record promotion in vsigma
    at all**; both consolidation rounds are *construction* from raw trajectory, so the defect
    is that construction picks its own destination scope, widest-first. Slice 2 adds the
    missing operation: trust vs. scope axes kept apart, `PromotionRequest(source_scopes,
    target_scope, policy)`, policy-driven transformation (`verbatim`/`generalize`/`redact`,
    never mandatory generalization), a gate-policy seam extending the structural-only
    `validate_bundle` (constructor cannot select or bypass), structured lineage at bundle
    schema v0.2, operation-marker idempotence (content addressing doesn't survive generative
    policies), per-policy recall dedup. Open Qs in §F (CLI verb; `PromotionRequest` as public
    API; may promotion touch confidence; v0.2 schema collision with maintenance-pass §D
    tombstones); decisions logged in §G.
- **Roadmap discipline (aligned to the updated vibe framework, 2026-09-06):** lifecycle is
  DRAFT → ACCEPTED → BUILDING → DONE, plus REJECTED-with-reason. plan.md's holding pen is now
  `## Candidates`, sitting after the numbered phases instead of between them; its stale
  day-one `## Next steps` was deleted (checkpoint.md owns Next Steps). Phases are now in
  numeric order in the file, and the header states what a number means: **an acceptance
  record, not a queue position** — 4.5 was accepted first and overtaken by 4.6 and 4.7, as
  Phase 3 was built after Phase 4. Renumbering to chase build order would churn ~30
  references, three in shipped source. Phases 4.7, 4.8, 4.9, 4.10 and 4.11 are DONE.
- **Divergence from the framework, decided 2026-09-06 (keep as is):** it places DRAFT
  proposals in `vdesign/proposals/`; this repo keeps all 15 in `docs/proposals/`, graduated
  and open alike, with the settled *how* consolidated into `architecture_design.md`. A
  proposal's value is the argument it preserves after the verdict, so it belongs in the
  stable tree from birth. Rationale recorded in plan.md's header.

## Trajectory capture fix (2026-09-06, found mid-Phase-4.7)

- **The seam was silently losing trajectory across a human interrupt.** Row keys were
  `f"{label}:{attempt}"`, and `human_interrupt_node` resets `attempt_count` to 0 on resume
  (`resilience.py:50`, deliberately — otherwise `break_on_attempts` re-escalates immediately).
  So post-resume firings overwrote the pre-escalation rows: measured 4 node firings → 2 rows,
  both holding post-resume content. Exactly the runs where a human contributed something.
- **Root cause:** `attempt_count` had two jobs — the retry budget (must reset) and half the
  storage key (must not repeat). Two independently-correct decisions in two files.
- **Fix (after three wrong turns, all caught in review):** the key carries **no ordinal** —
  `f"{label}:{uuid4().hex}"` — a full uuid4, untruncated. No coordination, so it cannot
  collide under any concurrency; and no truncation, since eight hex digits is 32 bits and a
  ~1% birthday collision across 9,300 firings of one node would silently overwrite a row. Sequence comes from `MemoryStore.query`'s insertion order, which
  `episode_trajectory`'s trailing window had always silently relied on and which the protocol
  now states as a contract. `wrap` returns the node's update exactly as produced.
- **The three rejected designs, and why** (all in `interceptor.py`'s docstring, since they
  are the kind of thing that gets relearned):
  1. *Resume-generation counter bumped by `human_interrupt_node`* — needs `resilience.py` and
     `interceptor.py` to cooperate, and `resilience.py` is public: a consumer's own escalation node
     must reset the budget to work at all, silently reintroducing the bug.
  2. *Counter in graph state* (`step_count` on `BaseState`) — broke two previously-working
     cases. A fan-out to two wrapped nodes raised `InvalidUpdateError`, because LangGraph
     rejects two writes to an ordinary channel in one superstep *even when the values are
     equal*; and a consumer's own schema that doesn't declare the field has it filtered out of
     every update, so the counter read zero forever (3 firings → 1 row, worse than the
     original bug).
  3. *Counter in the store* (`episodes/steps`, read-then-write) — races under `Send` fan-out
     to the **same** node. Measured: 5 Sends run on 5 threads in 0.21s, genuinely concurrent,
     so all five would have taken the same ordinal and overwritten one row.
- **The lesson:** graph state belongs to the consumer, and any allocator the seam has to
  coordinate is a race waiting for a workload. A key that needs no coordination is both
  correct and cheaper — the final version removes two store operations per node firing.
- **Resolved in Phase 4.8:** `episode_trajectory` used to truncate to
  `steps[-REFLEXIVE_WINDOW:]` (20) for `run_maintenance` too, so the deep pass never saw a
  long episode's beginning. The cap now lives in `reflexive_consolidator`; the deep pass reads
  full transcripts. **152 tests** at the time.

## Open / pending

- **Live smoke test** once LLM auth is provided.
- Pending validation demos noted in the design doc: MCP HTTP transport
  (multi-episode session rehydration retired by Phase 4.2's SQLite test).
