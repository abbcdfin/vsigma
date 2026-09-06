# vsigma — Plan & Roadmap

Design board and high-level roadmap. Status: `[ ]` todo · `[/]` in progress · `[x]` done.

**Lifecycle** (per `.vibe/vibe.md`): DRAFT → ACCEPTED → BUILDING → DONE, skipping nothing.
A DRAFT is unnumbered, under [Candidates](#candidates--draft-awaiting-a-decision-check-point)
below or in its own `docs/proposals/` file when it needs a real argument. It gets a **number
only on acceptance**; the active phase — and only the active phase — gets a granular board in
[`active_plan.md`](./active_plan.md); on completion the board collapses into a summary here.
A REJECTED candidate keeps its entry with the verdict and the reason, so it does not come
back unexamined.

**A phase number is an identity, not a queue position.** It records *order of acceptance* and
never changes afterwards — 4.5 was accepted before 4.6 and 4.7, and has been overtaken by
both. Phase 3 landed after Phase 4, deliberately. What is actually next is the checkbox
state here plus "Next Steps" in [`checkpoint.md`](./checkpoint.md); renumbering to chase
build order would churn ~30 references, three of them in shipped source.

**Guiding rule:** start with the simplest MVP that proves the composition seam, then grow
complexity gradually — each phase an *extension*, not a rewrite.

**Design reference:** the settled architecture lives in
[`docs/architecture_design.md`](../docs/architecture_design.md) (consolidated 2026-07-04 from
the graduated proposals; ⚙ = implemented, ✎ = design intent). Proposals in
`docs/proposals/` are historical records + the still-open outer ring. Do not restate the
model here; this file tracks *what gets built when*.

**Deliberate deviation (decided 2026-09-06):** `.vibe/vibe.md` places DRAFT proposals in
`vdesign/proposals/`. This repo keeps **all** proposals in `docs/proposals/`, draft and
graduated alike, because their value is the argument they preserve after the verdict —
splitting the set across a volatile and a stable tree would break both the graduated/open
reading and ~8 files of links. Graduation is marked in the file's status line and by
consolidation into `architecture_design.md`, not by moving it.

---

## Phase 0 — Project setup `[x]`
*Operationalize the environment before writing logic ("building is part of architecture").*

- [x] `git init`; add `.vibe/` to root `.gitignore`
- [x] Python project scaffold with `uv` (`pyproject.toml`, package layout, `Makefile`)
- [x] Test harness + lint/type-check wired so a new dev runs everything with one command
      (`make check` = ruff + mypy strict + pytest)

## Phase 1 — Minimal seam slice (the MVP) `[x]`
*Goal: a runnable agent composed from blocks, proving the wrapper seam + loop/run model.*
*This is the slice that validates the [Composition Model proposal](../docs/proposals/composition-model.md).*
*Demo target (per goal): a **web-browsing automation agent**.*

- [x] `BaseState` / `ReActState` (ids + control fields, lean — no payloads) — `state.py`
- [x] `GraphBuilder` — `add`, `set_entry`, `add_edge`, `route_conditional`, `compile` — `builder.py`
- [x] `react_node` — the ReAct loop primitive (LLM behind injectable interface) — `react.py`
- [x] Resilience: `break_on_attempts` + `HUMAN_INTERRUPT` / `human_interrupt_node` — `resilience.py`
- [x] The node wrapper seam, with **memory + observability live** and **context pass-through** — `builder.py`
- [x] Service interfaces + trivial defaults: `InMemoryStore`, `TrajectoryLog`
      (memory interface is **read+write symmetric and shape-agnostic** — seam for the
      [memory adapter layer](../docs/proposals/memory-construction.md))
- [x] `Run` handle: `agent.run()` / `run.resume()`, backed by `MemorySaver` checkpointer — `run.py`
- [x] LLM seam: `LLM` protocol + `FakeLLM` + `OpenAICompatibleLLM` (env-configured) — `llm.py`
- [x] Web-browsing tools + demo live in `examples/web_browsing/` (core stays domain-agnostic)
- [x] Runnable demo (`python -m examples.web_browsing.demo`) + 12 tests (compose; escalate→resume; browse)
- [x] **Decision Check Point: PASSED.** The seam held — only localized type-casts at the
      LangGraph boundary, no structural rewrite. Composition Model proposal is ready to graduate.

## Phase 2 — Routing, planning & tool extensibility `[x]`
- [x] Effort-estimation block as a conditional edge (cheap vs. heavy path) — `estimation.py`
- [x] Plan-and-Solve loop primitive — `planning.py`
      (also: extracted `Router` into `routing.py`, shared by resilience + estimation)
- [x] MCP tool adapter — `make_mcp_tools(connect) → MCPToolset` sources MCP tools through the
      `Tool` contract with **no engine change** (`mcp.py`; async→sync bridge; stdio + in-memory
      transports). Proven by an in-memory integration test. HTTP transport deferred.
      See [MCP tool adapter proposal](../docs/proposals/mcp-tool-adapter.md) (now VALIDATED).

## Phase 2.5 — Toolkit layering: augment-not-replace + templates `[x]`
*Foundational API change. Follows the*
*[toolkit-layering proposal](../docs/proposals/toolkit-layering.md) (now VALIDATED); revised the composition model.*

- [x] `wrap()` primitive (node → node, applies the seam) + `Services` holder — `seam.py`;
      proven by a test applying the seam to a raw `StateGraph` (no builder/template)
- [x] `Run` + `Agent` work over a vanilla compiled `StateGraph` — `run.py`
- [x] **Removed `GraphBuilder`** (value delivered by `wrap`/`Services`/`Agent`/`Run`)
- [x] `add_wrapped` / `add_router` helpers — smooth LangGraph's strict node typing on the
      raw path (surfaced by mypy-strict; keep the augment path ergonomic)
- [x] Agent templates in `vsigma/agents/` — return an `Agent` whose `.graph` is a real,
      inspectable compiled graph. A graceful-degradation ladder: `make_react_agent` (flat
      baseline, "less structure, more intelligence", bounded by `recursion_limit`) →
      `make_guarded_react_agent` (+ human-interrupt) → `make_plan_execute_agent` (+ planning).
      Flat terminator `loop_until_done`; `Agent` carries a configurable `recursion_limit` backstop.
- [x] Migrated example/demo/tests/README/`getting_started` to templates + primitives (26 tests)
- [ ] *(Deferred)* model-awareness: explicit declared-capability hints only (opt-in, measured);
      no inference-from-model-string — that's a Phase-2 orchestration concern

## Phase 3 — Critique loop & worked example `[x]`
*Landed 2026-07-04 (after Phase 4/4.1 — built out of order, deliberately). Scoping
decision: the SWE example is a **single-file code kata**, not a mini-SWE-bench — code is
the domain with a naturally deterministic critic (run the tests); recorded limitation:
converges to "passes the tests", not "is correct".*

- [x] Critique / Reflexion block — `critic_node(validator)` (`critique.py`;
      `Validator = str -> None | critique`) + `make_reflexion_agent` template: act's
      `done` routes to the critic, both routers `break_on_attempts` (never-valid
      candidates escalate, don't spin), backstop sized for 2 super-steps per attempt
- [x] SWE worked example end-to-end — `examples/code_kata/` (`make_test_validator`:
      candidate + asserts in a subprocess, traceback = critique; buggy→fixed offline
      demo showcasing `LoggingLLM`); `python -m examples.code_kata.demo`
- [x] *(follow-up, same day)* Validator combinators — `all_of` (first critique wins,
      deterministic clauses first) and `llm_judge` (LLM→Validator seam adapter; the
      deliberate non-deterministic clause — keep last, fails closed) + "Choosing a
      validator" guide in `getting_started`. Scope decision: concrete clauses are
      consumer code (`expect_json` demoted core → doc snippet; instances live in
      examples, contracts in core — same rule as tools). Deferred (2026-07-04, by
      choice): separate judge-failure counter — judge rejections/errors share the one
      attempt budget; a distinct counter is a small additive router change if field
      use ever shows judge outages burning meaningful budget before escalation

## Phase 4 — Real memory & context (core) `[x]`
*Memory work follows the [memory subsystem proposal](../docs/proposals/memory-construction.md);*
*its scope axis is the [running-context model](../docs/proposals/running-context-model.md).*
*(JIT breakdown was `active_plan.md`; core landed 2026-07-04, 55 tests.)*

- [x] **Scope ladder in code** — `user_id` (persona) on `BaseState`; structured
      `(scope-path, kind)` namespaces (`scope.py`); persona-visibility rule enforced by
      `recall` on the retrieve path (tested: private memory never crosses personas)
- [x] **Knowledge vs. context split** — the kind axis of every namespace; seam capture writes
      episodic context; construction/consolidation write knowledge (see proposal §B/§C)
- [x] **First-party stores** — `SQLiteStore` (stdlib, single file) joins `InMemoryStore`
      behind the unchanged `MemoryStore` protocol; proven backing a live agent.
      *Deferred → Phase 4.5:* small semantic index; external vector adapters
- [x] **One construction engine** (§D) — `distill` (constrained LLM output, content-addressed
      ids) with mandatory record-level **provenance** (`records.py`); re-ingest supersedes
      (incremental `add`/`update`; source-hash-driven prune deferred)
- [x] **Constructor seam + file-bundle contract** (§D) — bundle write/validate/ingest +
      `vsigma memory validate|ingest` CLI; schema **extracted** (schema-second) →
      `docs/schemas/memory-bundle.schema.json` (v0.1, drift-pinned to code by test);
      independent-constructor test passes (handmade bundle, no vsigma construction code)
- [x] **Two-round consolidation** (§E) — `reflexive_consolidator` run-end hook on
      `Services.run_end` (bounded ≤1 call, skippable, provisional 0.3) + `deep_consolidate`
      (same engine, 0.9, via the bundle gate); deep supersedes round-1 (tested); trajectory
      retention = the seam's episodic capture
  - [x] *(follow-up 2026-07-14)* hook fires at **every terminal edge**: mechanical episode
        summary written on escalation too (derived status; fixed id ⇒ resume-to-done
        overwrites) so retries can correlate failed attempts; distillation stays `done`-only
        (failure trajectories are the deep round's job — anti-knowledge risk recorded in §9)
- [x] Context management — `SlidingWindowContext` compaction in the existing seam slot.
      *Deferred → Phase 4.5:* recall-injecting assembly, budget-aware cross-store merge
- [ ] *(Deferred)* **Strategy library** (weighted retrieval, promotion policies,
      forgetting) — opt-in, measured; gated behind Phase 4.5's eval slice

## Phase 4.1 — LLM-call observability `[x]`
*Gap found in field use (2026-07-04): the seam observes at **node** grain, but the thing
worth watching is the **LLM call** inside the node — replies never reach `TrajectoryEvent`
(summary is status-only) and the episodic capture is post-hoc archaeology, not a live tap.
Fix at the LLM protocol seam, augment-not-replace — zero engine change.*

- [x] **`LoggingLLM(inner)`** — a wrapping `LLM` in `llm.py`: delegates `complete()`,
      logs through stdlib `logging` (logger `vsigma.llm`) so stdout vs. file vs. both is
      ordinary handler config. **INFO** = the reply (clipped text + tool calls with args)
      and latency; **DEBUG** = the full prompt (opt-in — the part that's "too much").
      Composes with any LLM (incl. `FakeLLM` — offline-testable) and any template.
- [x] **Enable/disable mechanism** — `enable_llm_logging(level=INFO, file=None)` /
      `disable_llm_logging()`: one-call toggle (stdout or file) that manages its own
      handlers idempotently and never clobbers user-configured logging; **silent by
      default** (library rule: a `NullHandler`, no output unless asked)
- [x] Tests: reply + tool calls at INFO; prompt only at DEBUG; response passes through
      unchanged; enable→file / enable→stdout / disable actually silences;
      end-to-end on a live template run
- [x] Docs: `getting_started` "watching the model" snippet; §11 ⚙ note in
      `architecture_design.md`
- Latency is logged here as a side effect; **structured** token/latency counters on
  trajectory events stay in Phase 4.5's eval slice (this phase is the live tap, not the
  measurement harness).

## Phase 4.2 — Sessions: multi-round dialog `[x]`
*Consumer-driven (bumblebee: "one playbook input, want multiple rounds"). Follows the
[interaction-model proposal](../docs/proposals/interaction-model.md): three grains —
mid-run (`resume`, exists), between-run (this phase), async bus (post-4.5, boundary
fixed in the proposal §C). Layering: `Session` is the spine, transports adapt in —
the future bus delivers into `session.send`, not the reverse.*

- [x] `Session` handle (`session.py`) + `Agent.session()` — `send(payload, channel=)`
      = one round; dialog (user/assistant turns only, noise filtered) carried via
      additive `Agent.run(history=)`; persisted to `session:<id>/context` →
      **rehydrates across process restarts** (tested on SQLite; retires the pending
      rehydration demo); per-turn envelope metadata `(payload, user_id, channel,
      run_id)` — model never sees channel
- [x] Grain unification: `send` on an escalated run routes to `resume()`
- [x] 7 tests (carry, noise filter, rehydrate, resume-routing, envelope, isolation);
      docs (§5 ⚙, getting_started sessions section)
- Deferred by design: dialog compaction (→ 4.5 measured assembly; designed in the
  [session-dialog-compaction proposal](../docs/proposals/session-dialog-compaction.md),
  DRAFT — opt-in `dialog_policy`, raw trajectory + distilled residue survive);
  concurrency arbitration + unsolicited signals (→ bus, proposal §C)

## Phase 4.3 — Maintenance pass: the deep round made runnable `[x]`
*Prompted by studying nanobot's memory system (Dream: cursor + scheduled batch); design
in the [maintenance-pass proposal](../docs/proposals/maintenance-pass.md). Closes "the
promotion pump nobody can run": every recent scope/failure-learning decision ended at
"that's the deep round's job".*

- [x] Processed-episode markers in operational `maintenance/state` (outside the scope
      ladder — never recallable); `unconsolidated_episodes` = set difference;
      `SQLiteStore.namespaces()` backend extra (protocol stays minimal)
- [x] `run_maintenance` + `vsigma memory consolidate --db … [--scope|--out|--limit|
      --dry-run|--model|--constructor]` — batch → distill → bundle → gate → **markers
      advance only after successful ingest** (failed pass retries the same batch;
      zero-record batches mark without a bundle)
- [x] `vsigma memory export` — read-side JSON-lines inspection (replaces raw sqlite3
      in the docs)
- [x] 9 tests (ingest+cursor, dry-run, failure/retry, zero-record, limit, scope parse,
      CLI export / no-LLM refusal / dry-run)
- Deferred to the proposal §D: tombstones/retraction records (bundle schema v0.2,
  strategy-library gate), per-record scope routing, evict-summarization (→ 4.5),
  internal scheduling (cron owns time, by the maintenance-without-the-agent decision)

## Phase 4.4 — Thinking models at the LLM seam `[x]`
*Prompted by bumblebee on local thinking models: reasoning invisible for debugging,
inline `<think>` breaking `distill`, consolidation overthinking. Design + accepted
decision in the [thinking-models proposal](../docs/proposals/thinking-models.md).*

- [x] `LLMResponse.reasoning` side channel — client captures the separate
      `reasoning_content`/`reasoning` field and strips inline `<think>` blocks out of
      `text` (truncated blocks strip to end); reasoning never re-enters the history
- [x] `LoggingLLM` surfaces it: clipped `[thought: …]` at INFO, full at DEBUG
- [x] `OpenAICompatibleLLM.extra_body` (provider-switch passthrough, merged last) +
      `max_tokens`; env `VSIGMA_LLM_EXTRA_BODY` (JSON object, malformed → ValueError)
      and `VSIGMA_LLM_MAX_TOKENS` via `default_llm()`
- [x] Run-end consolidation ladder documented (mechanical-only / **distiller with
      thinking off — accepted default for local thinking models** / model as-is);
      "distiller" naming settled ("constructor" stays a provenance identity)
- [x] 7 tests (`test_thinking_models.py`: split terminated/multiple/truncated, both
      reasoning serializations, payload merge, logging, env parse/reject)
- Deferred (proposal §C): reasoning-in-trajectory (opt-in observer if asked for),
  normalized `thinking=` flag (passthrough is the design), streaming
- [x] Follow-up (same day): the **durability bar** in `_DISTILL_SYSTEM` — record only
      facts useful in a future, unrelated session; run ephemera (execution times,
      timestamps, run/session ids, resolved transient errors) excluded by name.
      Drift-pinned by test.

## Phase 4.5 — Semantic recall & measured assembly `[ ]`
*The read-side loop, bundled deliberately: an index alone is a feature nobody calls;
index + assembly + measurement closes ingest → recall → prompt. Unblocks the §10 ✎ ring
in [`architecture_design.md`](../docs/architecture_design.md).*

**Overtaken, not stalled.** Accepted as the next phase after 4.4, then displaced twice by
consumer-driven work with a live trigger: 4.6 (bumblebee had hand-rolled the bridge) and 4.7
(the dogfooding review surfaced verified defects). 4.5 has no such trigger yet — nothing has
hit the retrieval wall, so building the index now would be measuring a problem no consumer
reports. **Pull-forward trigger:** bumblebee or the kata example hits retrieval limits, or
eval trajectories are wanted. Its `RecallContext` item is `[/]` — v0 shipped early.

- [ ] **Small embedded semantic index** — first-party, behind the `MemoryStore` seam;
      embeddings via an optional extra with a pluggable embedder (offline tests stay
      offline: deterministic fake embedder); external vector DBs remain opt-in adapters
- [/] **`RecallContext`** — **v0 landed early (2026-07-04):** gather (visible scopes,
      §5 rule) → rank by provenance confidence → bound (`max_facts`) → tagged/replaced
      injection (no stacking across steps); composable via `inner`; **opt-in** (default
      stays `PassthroughContext`). *Remaining for 4.5:* relevance × recency scoring,
      token-cap budget with deterministic eviction (designed in the
      [token-budget-context proposal](../docs/proposals/token-budget-context.md), DRAFT —
      budget enforced at the assembly seam over the whole window; app-set budget +
      `usage` on `LLMResponse` + char/usage-calibrated estimation, no tokenizer dep)
- [ ] **Minimal eval/metrics slice** (from §11 ✎): token/latency counters on trajectory
      events + a before/after harness, so the assembly policy is *measured* — the recorded
      bar ("innovation must be measured") for any default
- [ ] Feed-through: cross-store ranking/merge findings recorded back into the
      [memory proposal](../docs/proposals/memory-construction.md) outer ring (strategy
      library stays behind this gate)

## Phase 4.6 — AsyncBridge: the loop-thread bridge as public API `[x]`
*Landed 2026-07-19. Second-consumer trigger: bumblebee hand-rolled the same dedicated
loop-thread pattern for Playwright that `_AsyncBridge` already ran for MCP. Design +
accepted decision in the [async-bridge proposal](../docs/proposals/async-bridge.md).*

- [x] `bridge.py` (stdlib-only) — public `AsyncBridge`: one long-lived loop in a
      background thread; `run(timeout=)` **cancels the coroutine on expiry** (abandon,
      not just stop waiting); `wrap(fn, timeout=120.0)` returns a sync `Tool.func`-shaped
      callable with a finite tool-facing default; idempotent `shutdown`; context manager
- [x] `mcp.py` refactored onto the public class (`_AsyncBridge` deleted, behavior
      unchanged — proves the public surface is sufficient)
- [x] Exported; `getting_started` "async libraries behind sync tools" recipe (names the
      `asyncio.run()`-per-call trap); 6 bridge tests incl. the statefulness property
      per-call `asyncio.run()` breaks + cancel-on-timeout. **116 tests.**
- Live proof pending: bumblebee deletes its hand-rolled bridge for `from vsigma import
  AsyncBridge` (agy's move). Full async execution path (`acomplete`, async `react_node`)
  stays deferred — trigger is daemon concurrency, same cluster as the SQLiteStore WAL slice.

## Phase 4.7 — Memory scope safety: one destination rule `[/]`
*Slice 1 of the [memory-scope-promotion proposal](../docs/proposals/memory-scope-promotion.md)
(§B), separated out because it fixes **verified defects** rather than adding a capability —
it is not gated on the §F verdicts that hold Slice 2. Prompted by the bumblebee dogfooding
review (agy) plus a second review round. Reframe behind it: vsigma has no record→record
promotion at all, so the defect is that* construction *picks its own destination scope,
unconstrained and widest-first. No schema change; graduation criteria are already written as
testable assertions (proposal §E). **BUILDING** — board in
[`active_plan.md`](./active_plan.md).*

- [ ] **One destination rule for automatic construction** — narrowest durable scope, else no
      durable write. `_knowledge_scope` currently widens provisional 0.3-confidence knowledge
      to `persona(user_id)`, and sessionless to `AGENT` — which `visible_scopes` returns for
      *every* persona — while the mechanical summary ten lines above it
      (`consolidation.py:98-100`) narrows to session-else-episode. Making the two consistent
      is the fix; the widening rule has no recorded rationale. Escape hatch is explicit
      configuration, never an inferred fallback. Forward-only: existing records stay put.
- [ ] **Durable episode ownership** — `episodes/index` operational namespace
      (`run_id → session_id, user_id, created_at`), written by the seam, following the
      `maintenance/state` pattern. Needed because episodic context is keyed on `run_id`
      alone (`seam.py:42`) and ownership can't be reconstructed today. Tenant axis rejected
      for v0: persona and session are the boundaries.
- [ ] **Mandatory source selection** — `run_maintenance`'s `--scope` names the *destination*,
      so one pass currently distills every tenant's episodes into one persona. At least one
      `--source` required, `--all-sources` explicit, neither = error; unindexed episodes
      match only `--all-sources` (fail closed).
- [ ] **Auditable bundles** — pass `sources=` to `write_bundle` (already accepted and hashed
      into the manifest; `maintenance.py:99-101` just omits it).
- [ ] Tests per proposal §E (slice 1); docs; `checkpoint.md` + tag.

## Phase 5 — Multi-agent `[ ]`
- [ ] Subgraph composition helper (graph-as-node)
- [ ] `delegate` primitive — dynamic subagent spawning as nested runs (uses `parent_run_id`)

## Phase 6 — Orchestration Engine Mode (future product mode) `[ ]`
- [ ] Deferred: runtime that reads a goal and generates/compiles the graph automatically

---

## Candidates — DRAFT, awaiting a Decision Check Point

*Design records written and committed ahead of implementation (proposals-before-build).
Each awaits a verdict; none implemented. Listed here so the roadmap isn't silently behind.*

**Exit rule:** this section holds only what is still awaiting a verdict. An **ACCEPTED**
candidate graduates out of here into a numbered phase (as `async-bridge` → 4.6,
`thinking-models` → 4.4, `maintenance-pass` → 4.3 did), with its rationale consolidated into
`docs/`. A **REJECTED** one stays here with the verdict and the reason rather than being
deleted. Candidates are unnumbered on purpose: a number is an acceptance record, and
rejecting a numbered draft would leave a hole.

- **Capability extension (the autonomy ladder)** — [dynamic-tools](../docs/proposals/dynamic-tools.md)
  (foundation: `ToolSelector`, gated `ToolCatalog`, `load_skills` SKILL adapter),
  [procedure-extraction](../docs/proposals/procedure-extraction.md) (rung 2: instruction-only
  SKILL.md distilled from trajectories, mechanically gated), [agent-authored-tools](../docs/proposals/agent-authored-tools.md)
  (rung 3: the skill pipeline; author never operates the gate). Shared discipline:
  staging → gate → admit, mirroring the memory bundle.
- **Context economics** — [session-dialog-compaction](../docs/proposals/session-dialog-compaction.md)
  (bound what a Session stores/replays) + [token-budget-context](../docs/proposals/token-budget-context.md)
  (bound what we send per call, at the assembly seam). Both feed Phase 4.5's measured
  assembly; kept as two focused proposals by design.
- **Episode retention** ([episode-retention](../docs/proposals/episode-retention.md)) —
  nothing in `src/` deletes anything, so a long-running daemon grows without bound. Prune
  whole episodes, keyed by `task_id` (keep the N most recent per task), `run_id` staying the
  only storage key so nothing collides. Reuse of a `run_id` was the tempting non-answer: it
  bounds the namespace by *overwriting*, destroying trajectory the deep round never read, and
  inherits the episode's `deep:` marker so every later run is silently skipped — Phase 4.7
  rejects it, and this is what that rejection leaves unserved. Trajectory and marker go; the
  `episodes/index` entry survives as a tombstone stamped with a disposition, and knowledge
  records are never touched. Consolidated-only by default, unconsolidated behind an explicit
  flag (refusing outright would deadlock against an infrequent maintenance cron). Grouped by
  **(owner, `task_id`)**, taking the same mandatory source boundary as `consolidate` — a
  caller-supplied task name is not globally unique, and deleting across a tenant boundary is
  worse than reading across one. Two prereqs: `MemoryStore` has no delete, and a `deep:`
  marker does not currently mean the episode was *processed* — `episode_trajectory` caps at
  `steps[-REFLEXIVE_WINDOW:]` (20) and `run_maintenance` calls that same capped function, so
  the offline batch pass is silently bounded by a constant named for the reflexive round's
  per-run budget. That is a defect in the shipped deep round in its own right; retention is
  what would make it unrecoverable. Non-goals: LangGraph checkpoints (a store vsigma cannot
  reach — possibly the larger pile) and the superlinear within-run growth from `seam.py:66`
  persisting cumulative `messages`, which is bug-shaped and independently fixable.
- **Memory scope promotion — Slice 2 only** ([proposal](../docs/proposals/memory-scope-promotion.md)
  §C; Slice 1 graduated to Phase 4.7 above). The missing operation: an existing knowledge
  record derived into a wider scope, which does not exist in vsigma today. Trust and scope
  axes kept apart (widening visibility is not evidence of truth);
  `PromotionRequest(source_scopes, target_scope, policy)`; transformation is policy-driven
  (`verbatim`/`generalize`/`redact`), never mandatory generalization; a gate-policy seam
  extending the structural-only `validate_bundle`, which the constructor cannot select or
  bypass; structured lineage at bundle schema v0.2; idempotence via an operation marker
  (content addressing does not survive generative policies); per-policy recall dedup.
  Awaits the §F verdicts — CLI verb, `PromotionRequest` as public API, whether promotion may
  touch confidence, and the v0.2 schema collision with maintenance-pass §D tombstones.

## Deferred work
- **User manual** — *comprehensive* build guide. A lean consumer entry point now exists at
  [`docs/getting_started.md`](../docs/getting_started.md); the full manual stays deferred and
  will grow as the toolkit stabilizes (and as the co-designed agent surfaces real needs).
- **PyPI publish** — deferred until the API settles (post Phase 4 + the consolidation pass).
  Decision (2026-06-28): not reserving the name yet. `vsigma` confirmed **available** on PyPI
  (also `vector-sigma`, `vsigma-ai`); fallback is **`vector-sigma`** if `vsigma` is taken.
  Before any upload: readiness pass (LICENSE file, metadata/classifiers/urls, single-sourced
  version, CHANGELOG, optional `mcp` extra) + TestPyPI dry run.
