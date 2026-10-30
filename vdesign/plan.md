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
      (same engine, 0.9, via the bundle gate); deep supersedes round-1 at the same scope
      (tested; since Phase 4.7 a wider deep destination adds a copy instead); trajectory
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

## Phase 4.7 — Memory scope safety: one destination rule `[x]`
*Landed 2026-09-09. Slice 1 of the
[memory-scope-promotion proposal](../docs/proposals/memory-scope-promotion.md) (§B, now
VALIDATED), separated out because it fixed **verified defects** rather than adding a
capability. Prompted by the bumblebee dogfooding review (agy) plus two further review rounds.
The reframe behind it: vsigma has no record→record promotion at all, so the defect was that*
construction *picked its own destination scope, unconstrained and widest-first. No schema
change; forward-only (existing records stayed put). 151 tests.*

- [x] **One destination rule** — `_knowledge_scope` returns `Scope | None`: the session, or
      nothing durable. It used to widen provisional 0.3-confidence knowledge to
      `persona(user_id)`, and sessionless runs to `AGENT`, which `visible_scopes` returns for
      *every* persona. Ten lines above, the mechanical summary already narrowed; making the
      two consistent was the fix, and the widening rule had no recorded rationale. No
      `episode` fallback either — episode-scoped knowledge is a write-only sink. Escape hatch
      is `reflexive_consolidator(knowledge_scope=)`: explicit configuration, never an
      inferred fallback.
- [x] **Durable episode ownership** — `episodes.py`, the `episodes/index` operational
      namespace (`run_id → task_id, session_id, user_id, created_at`), written by `Agent.run`
      before `graph.invoke`. Ownership is immutable and **a `run_id` is never recycled**: it
      is simultaneously the LangGraph thread, the episodic namespace, the maintenance cursor
      entry and every derived record's `source_ref`, and nothing releases it — the deep pass
      *adds* a permanent marker. Tenant axis rejected for v0.
- [x] **Mandatory source selection** — `--scope` named the destination and nothing named the
      source, so one pass distilled every tenant's episodes into the single scope named.
      `sources` is now required on `unconsolidated_episodes` and `run_maintenance` (**a
      breaking signature change**); CLI takes repeatable `--source persona:|session:` or an
      explicit `--all-sources`, with neither *or both* an error. Unknown ownership fails
      closed. `--scope` also lost its `agent` default — the same argument applied to the
      destination.
- [x] **Auditable bundles** — `run_maintenance` passes `sources=` to `write_bundle`, so a
      manifest records which trajectories it read and the boundary can be *checked*.
- [x] Tests, docs (`architecture_design` §9 gained the **Destination row** it never had, plus
      a two-boundaries paragraph; `getting_started`'s learning walkthrough and CLI block
      corrected; `maintenance-pass.md` banner), tag `phase-4.7`.

**Found and fixed along the way** (shipped defects, not part of §B): the seam was silently
losing trajectory rows across a human interrupt — keys were `f"{label}:{attempt}"` and
`human_interrupt_node` must reset `attempt_count`, so post-resume firings overwrote
pre-escalation ones. Rows are now keyed `f"{label}:{uuid4().hex}"`, needing no coordination;
sequence comes from `MemoryStore.query`'s insertion order, now stated in the protocol. Four
designs were tried before that one — see `seam.py`'s docstring for the three rejected and why.

**Still open in this area:** `episode_trajectory` truncates to `steps[-REFLEXIVE_WINDOW:]`
(20) and `run_maintenance` calls that same capped function, so the deep pass never sees a
long episode's beginning — a Phase 4.3 defect, recorded as a hard prerequisite in
[episode-retention](../docs/proposals/episode-retention.md) §B.3 with two candidate fixes in
§F.1.

## Phase 4.8 — Thread-native runtime `[x]`
*Session = Thread, Append-Only Messages, Optional Memory. Landed 2026-10-25 (all 3 slices + review passes).
Follows `docs/proposals/thread-native-runtime.md`. 181 tests green.
Thesis: keep the convenience layer, but store nothing LangGraph already stores.*

Verdicts (its §G):
- `send` while paused raises `SessionPaused` unless `abandon=True`.
- Continuity lives on LangGraph threads (`history=` dropped).
- Context assembly is view-only ($O(N)$ episodic storage).
- `run_id` identifies the run; continuity across turns uses `session_id`.
- System prompt is in `state["system"]`, not `messages[0]`.
- Durability is configurable (`durability="exit"` default in templates).
- No compatibility aliases (§G.8): `Services`, `seam.py`, `Agent.services`, and `services=` dropped.
- Stream-driven trajectory observability (§G.9): events emitted from LangGraph's execution stream.
- Opt-in observer (§G.10): `Interceptor.observer` defaults to `None`, preventing unbounded memory growth.
- What the `messages` guard compares (§G.11): object identity catches echoed message dicts while allowing legitimate LLM tool-call retries.
- Historical run snapshot isolation: `Run.snapshot` targets the run's own checkpoint, refusing resume on abandoned runs.
- Distillation session context ordering & sizing: strictly ordered by insertion index, filtering tool noise.
- The web app is **not migrated** in this phase. Each slice writes its consumer changes to
  `docs/upgrading.md`, for the web app and bumblebee.

Reverses `architecture_design.md:82` ("session = one long thread" rejected), argued in its
§A.1.

- [x] **Slice 1 — append-only `messages`.**
  - `append_messages` reducer with fail-loud guard against cumulative returns.
  - Four nodes (`react_node`, `critic_node`, `human_interrupt_node`, `plan_node`) return message deltas only.
  - Initial `input:<uuid4>` row; `episode_trajectory` concatenates rows without cumulative masking; `REFLEXIVE_WINDOW` cap lives in `reflexive_consolidator`.
  - Ended quadratic capture, destructive context trim, and deep pass 20-step truncation.
- [x] **Slice 2 — session = thread.**
  - `thread_id = session_id or run_id`, metadata tagged (`run_id`, `task_id`, `user_id`, `channel`).
  - Stateless `Session` projection reading checkpointer snapshot via `graph.get_state()`.
  - `SessionPaused` raised on `send()` if paused at an interrupt unless `abandon=True`; `session.resume(value)` continues run.
  - `Run` exposes `interrupted`, `interrupt` with payload `kind` (e.g. `"escalation"`).
  - Durability default `"exit"` and `system` prompt in `state["system"]`.
  - Multi-turn prior context prepended in `episode_trajectory`.
  - Retired dialog key, `turn:` records, `Session.runs`, and `Agent.run(history=)`.
- [x] **Slice 3 — optional memory and `Interceptor` rename.**
  - `Interceptor` with `kw_only=True` bundles observer, context, optional memory, and run_end hook.
  - `memory` defaults to `None`, producing zero external store writes on pure thread checkpointing.
  - Fail-loud store guards on `RecallContext.assemble` and episode capture functions.
  - Renamed `Services` -> `Interceptor` (`seam.py` -> `interceptor.py`), with no compatibility aliases (§G.8).
- [x] **Review fixes.**
  - Dropped compatibility aliases; `durability` typed to `Durability`; turn result reset between turns.
  - Paused check in `Agent.run`; tags preserved on resume; run snapshots isolated from thread head.
  - Distillation session ordering & sizing fixed; stream-driven trajectory observability (§G.9).
  - Context module cleanup; design rationale restored; opt-in observer with no default log (§G.10).
  - Reducer guard switched to object identity (§G.11); `Session.resume` carries `recursion_limit`.
  - `Run.snapshot` raises on un-checkpointed runs; `Run.trajectory` enforces `TrajectoryLog`.
  - `dialog_turns` settled in `critique.py`; filled `upgrading.md` doc gaps.
- [x] **Close.**
  - Graduated proposal to `docs/proposals/thread-native-runtime.md`.
  - Updated `docs/architecture_design.md` §§3, 4, 5, 11, `README.md`, `docs/getting_started.md`, and `docs/upgrading.md`.
  - Marked `session-dialog-compaction` superseded, updated `episode-retention` prerequisites.
  - Collapsed board and tagged `phase-4.8`.

**Follow-ons, not in this phase:** a persistent checkpointer for the web app (its slice 4);
whether the SQLite saver implements `prune` (the network-blocked spike; answered 2026-10-26:
no, see the thread-retention candidate); and the storage engine under governed memory (§G.7).

## Phase 4.9 — LangGraph messages: `add_messages` replaces the append reducer `[x]`
*Landed 2026-10-25. Follows `docs/proposals/langgraph-messages.md`. 186 tests green.
Thesis: use LangGraph's native message representation and id-based merging.*

Verdicts (its §G):
- Adopt `add_messages`: messages carry ids and merge by id. Rejected: keeping the identity
  guard, and vsigma's own ids on dicts (§E).
- Hold the 4.8 consumer notice until 4.9 lands, so the web app and bumblebee migrate once.
- The `LLM` protocol, capture rows and `Session.dialog` stay OpenAI dicts.
- Accepted cost: +168 B per message in checkpoints.

Outcome summary:
- `ReActState.messages` uses LangGraph's `add_messages` (`langchain_core.messages.AnyMessage`).
- Messages carry ids and merge by id; returning cumulative history updates in place without duplicating.
- Subgraphs sharing `messages` now work cleanly as nodes, whether running straight through or paused at `interrupt()` (the reason for the phase).
- Boundaries stay plain OpenAI dicts: the `LLM` protocol, memory capture rows, and `Session.dialog`.
- `wrap` strips received window messages, preventing context injections from persisting to the thread.
- `append_messages` and its fail-loud guard are completely removed.
- Consumers (the web app and bumblebee) have clear single-step migration notes in `docs/upgrading.md`.

- [x] **Slice 1 — the switch** (atomic: state cannot hold both kinds of message).
  - `ReActState.messages` uses `add_messages`; `append_messages`, its guard and its tests go.
  - `react_node` and `plan_node` convert with `convert_to_openai_messages` before the LLM.
  - Readers use LangChain messages: `SlidingWindowContext`, `RecallContext`, `estimation`.
  - `wrap` captures OpenAI dicts and drops the message objects it handed the node; `Session.dialog` converts.
- [x] **Slice 2 — docs.** `upgrading.md` (replaces the 4.8 guard and subgraph notes, adds the
      consumer lines of proposal §C), `architecture_design.md` §4, and thread-native-runtime
      §B.3 and §G.11 marked superseded.
- [x] **Review fixes.**
  - Locked `langchain-core>=1.4` dependency in `uv.lock`.
  - Added two-call `make_react_agent` tool-conversion test and `plan_node` conversion test.
  - Corrected `test_messages.py` paused subgraph content extraction using `convert_to_openai_messages`.
  - Updated `test_services.py` sliding window tests with session and two sends; removed dead dict checks.
  - Fixed `wrap` to pass non-dict updates (e.g. `None` or `Command`) through unchanged.
  - Aligned `docs/architecture_design.md` capture row wording and `docs/upgrading.md` `Run.state["messages"]` classification.
- [x] **Close.** Proposal status, board collapsed, tag `phase-4.9`.

## Phase 4.10 — Thread retention: bounding what the checkpointer keeps `[x]`
*Landed 2026-10-26. Follows the [thread-retention proposal](../docs/proposals/thread-retention.md).
Prompted by the SQLite checkpointer spike: every checkpoint holds the whole conversation, so
under `"exit"` a session's storage grows quadratically (75.8 KB for ten short turns, about
574 MB at 1,000), and of the five LangGraph checkpointer backends only Redis implements `prune`.
226 tests green.*

Verdicts (its §G):
- Ship `keep_latest` in vsigma, applied after each turn when opted in with
  `checkpoints="latest"`; default `"all"`. When the thread isn't paused it keeps only the main
  graph's latest checkpoint; while paused, the latest in every namespace.
- Earlier `Run` objects raise after pruning; this is documented.
- An opt-in message window, `retain_messages`, removed with `RemoveMessage`.
- `Session.end()` and `Run.end()` delete a finished thread.
- `langgraph-checkpoint-sqlite` becomes a dev dependency, locked through the Tsinghua mirror
  declared as the project's default index.
- The 4.8/4.9 consumer notice is held until 4.10 lands, so one notice covers all three phases.

- [x] **Slice 1 — `keep_latest` for `InMemorySaver` and `SqliteSaver`.** Keeps root latest checkpoint when unpaused and all namespace latest checkpoints when paused; cleans unreferenced blobs on `InMemorySaver`; atomic SQLite transaction under `cp.lock` via `BEGIN IMMEDIATE` for `SqliteSaver`; delegates to `prune` where implemented (Redis); unsupported savers or graphs with `DeltaChannel` raise.
- [x] **Slice 2 — `checkpoints="latest"` on `Agent` and templates.** Opt-in on `Agent` and the four prebuilt templates (`make_react_agent`, `make_guarded_react_agent`, `make_plan_execute_agent`, `make_reflexion_agent`); prunes old checkpoints after each turn and resume (after run-end hooks); validates support at construction.
- [x] **Slice 3 — `Session.end()` and `Run.end()`.** Deletes thread from checkpointer (`delete_thread`), preserves vsigma memory, refuses paused threads unless `abandon=True`, and rejects `Run.end()` on a session run.
- [x] **Slice 4 — `retain_messages` and `retain_window`.** Plain `"retain"` node emitting `RemoveMessage(id=...)` on `done` turns; construction check enforces `retain_messages >= SlidingWindowContext.max_messages`.
- [x] **Slice 5 — docs and upgrading notes.** Documented in `docs/upgrading.md` (with the web app's and bumblebee's migration notes), `docs/getting_started.md`, and `docs/architecture_design.md` §4.
- [x] **Review fixes.** Input validation for `retain_window(max_messages < 1)` and `Agent(checkpoints=...)`; unified `_pruner` checkpointer dispatch and `isinstance(ch, DeltaChannel)`; dropped dead dict branches in `window` and `retain_window`; direct `delete_thread` calls; strengthened subgraph namespace test under default durability.
- [x] **Close.** Proposal graduated, board collapsed, tag `phase-4.10`.

## Phase 4.11 — Thread concurrency: one operation per thread at a time `[x]`
*Landed 2026-10-29. Follows the [thread-concurrency proposal](../docs/proposals/thread-concurrency.md).
A bug fix for released behaviour, found 2026-10-27 by tracing an audit finding (expiry deleting
a running thread) back to the same check-then-act pattern in shipped code. Released alone as
`v0.2.1`, with the `mcp<2` cap; the web app's migration targets it. 234 tests green.*

Verdicts (its §F):
- Whatever checks and then changes a thread holds that thread's lock for the whole operation,
  shared by every `Agent` over one checkpointer.
- A second operation on a busy thread raises `SessionBusy` at once; the caller decides whether
  to retry.
- One process per checkpointer is the documented boundary. A lease in the database waits for a
  consumer that scales out.
- `v0.2.1` is this fix alone, with the `mcp<2` cap; thread expiry moves to `v0.2.2`.
- A resume can name the pause it answers: an optional `interrupt_id`, compared under the lock,
  and `PauseChanged` on a mismatch.

- [x] **The lock.** Per-`(checkpointer, thread_id)` busy-thread registry keyed by checkpointer instance (`thread_lock` in `retention.py`) and `SessionBusy(thread_id)` (`RuntimeError` subclass); `Agent.run`, `Session.resume`, `Run.resume`, `Session.end()`, `Run.end()`, and direct `keep_latest` take it; the `SessionPaused` check runs inside the lock.
- [x] **The pause id.** `interrupt_id` exposed on `Session`, `Run`, and `SessionPaused`; `interrupt_id: str | None = None` on `Session.resume` and `Run.resume`, compared under the lock; `PauseChanged(interrupt, interrupt_id)` raised on mismatch or when no pause is pending.
- [x] **Docs.** `upgrading.md`: `SessionBusy`, the one-process-per-checkpointer boundary, `interrupt_id` and `PauseChanged`, and the web app's note (target `v0.2.1`; its answer message names the question it answers); updated `getting_started.md` and `architecture_design.md` §4.
- [x] **Review fixes.** Busy-thread registry shrinks to empty when operations finish and skips locking when `checkpointer is None`; `getting_started.md` saves `qid = session.interrupt_id` when showing the pause; `upgrading.md` restores the `v0.2.0` warning under `Releases` and fixes cross-references.
- [x] **Close.** Proposal graduated, board collapsed, `mcp>=1.28.1,<2` cap locked, tag `phase-4.11`.

## Phase 4.12 — Thread expiry: ending idle sessions and runs, and episode status recovery `[/]`
*Accepted 2026-10-30 per [`docs/proposals/thread-expiry.md`](../docs/proposals/thread-expiry.md) as Phase 4.12 (target release `v0.2.3`), under the [`docs/proposals/scope-lifespans.md`](../docs/proposals/scope-lifespans.md) umbrella. Active board in `vdesign/active_plan.md`.*
- [ ] **Step 1 — Module layout cleanup & proof-of-lock.** Extract `SessionBusy`, `LockedThread`, and `thread_lock` into `src/vsigma/threads.py`, replace `_pruner` with `prune_checkpoints(graph, locked: LockedThread)` taking proof of lock, and maintain backward-compatible re-exports.
- [ ] **Step 2 — `ThreadInfo` and `ThreadExpiry.threads()`.** Read root (`""`) checkpoint metadata and pending writes without deserializing checkpoint state; extract `last_active` from uuid6 checkpoint id; support `InMemorySaver` and `SqliteSaver`.
- [ ] **Step 3 — `ExpiryPolicy` and race-free checkpointer deletion.** `idle_ttl`, `keep_last` per user, `paused_idle_ttl`, `run_ttl` (defaulting to `idle_ttl`); deletion holds `thread_lock` and aborts if checkpoint moved; atomic delete across all namespaces, writes, and unreferenced blobs.
- [ ] **Step 4 — Episode status lifecycle & recovery sweep.** Operational namespace `episodes/status` records `running`, `escalated`, `done`, `failed`, `abandoned` under `thread_lock`; recovery sweep marks abandoned orphaned or dead episodes.
- [ ] **Step 5 — Documentation, roadmap updates, and release tag.** Update docs, graduate `thread-expiry.md`, collapse active plan, update checkpoint, tag `phase-4.12`.

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
- **Context economics** — **[session-dialog-compaction](../docs/proposals/session-dialog-compaction.md) (SUPERSEDED by Phase 4.8)**
  (Phase 4.8 stores session history directly in thread checkpoints with `durability="exit"`, retiring the session dialog key;
  in-thread token trimming is handled at context assembly) + [token-budget-context](../docs/proposals/token-budget-context.md)
  (bound what we send per call, at the assembly seam). Both feed Phase 4.5's measured
  assembly; kept as two focused proposals by design.
- **Session admission** ([session-admission](../docs/proposals/session-admission.md)) — a
  `session_id` is currently a **bearer capability**: `visible_scopes` admits a session scope on
  presence of the id (`scope.py:68-69`) and `Session` rehydrates a dialog on it alone
  (`sessions.py:60`), with no participant list anywhere in `src/`. So a second persona naming
  someone else's session reads its dialog, turn envelopes, episode summaries and — since Phase
  4.7 — its distilled knowledge. Reproduced. `architecture_design` §5 claimed
  `visible_scopes`/`recall` enforced "session → its participants"; that claim has been
  corrected (2026-09-10) and this proposal is how it becomes true. The finding that makes it
  cheap: the turn envelope **already records the author**, so the data an admission check needs
  is durable and simply unread. Design: a `sessions/index` operational namespace
  (`session_id → owner, visibility, participants, created_at`), private by default, sharing an
  explicit act. The owner declares a session `private` or `open` — policy, kept separate from
  `participants`, which is state; it also removes the last inferred rule in the design, since
  an unowned (`user_id=None`) session is now *recorded* open rather than deduced from a null
  owner. Prompted by a bumblebee dogfooding review of Phase 4.7. Not a 4.7 regression — the
  shared channel predates it (dialog since 4.2, episode summaries since before 4.6) — but 4.7's
  own §A.4 argument about `AGENT` applies one rung down, which is how it was missed.
- **Scope lifespans** ([scope-lifespans](../docs/proposals/scope-lifespans.md), OPEN;
  DRAFT revision 2, 2026-10-27; Decision Check Point 2026-10-30): the umbrella for bounding
  what an agent keeps. Implements architecture §8's "scope: namespace + visibility + lifespan",
  of which lifespan was never built. Each rung of the scope ladder gets a lifespan the app
  declares as data; vsigma enforces it through two entry points with different cost and cadence:
  thread expiry (minutes, in process, no LLM) and the memory pass (daily, by cron or a daemon's
  timer, with an LLM). The hand-off between them: a status lifecycle in `episodes/status` covering
  every way a run ends, one writer per operational record, a retried forget record for session
  memory, and consolidation of ended episodes only. Every §H verdict is accepted (2026-10-30);
  §C.1 is built in Phase 4.12 (`v0.2.3`), while §C.2–§C.3 wait for the `BaseStore` adapter.
  - **Thread concurrency** ([thread-concurrency](../docs/proposals/thread-concurrency.md)):
    **graduated 2026-10-29 as Phase 4.11** (above), released alone as `v0.2.1`.
  - **Thread expiry** ([thread-expiry](../docs/proposals/thread-expiry.md)):
    **graduated 2026-10-30 as Phase 4.12** (above; target release `v0.2.3`).
  - **Episode retention** ([episode-retention](../docs/proposals/episode-retention.md),
    revision 5): the episode rung's transcripts, pruned in the memory pass after consolidation.
    `run_maintenance(..., retention=RetentionPolicy(keep_last, max_age))` and
    `vsigma memory maintain`. Only ended episodes are consolidated; a marker's row count must
    match the transcript, for selection, pruning and prior-context protection alike; capture
    rows and markers go, summaries and knowledge stay, and a tombstone with a content hash is
    written first, under its own key, as the prune's crash-safe intent. Awaits the §H
    verdicts.
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
- **Open-source groundwork** ([open-source-groundwork](../docs/proposals/open-source-groundwork.md),
  DRAFT 2026-10-30): what a contributor needs before the first pull request. Lock against
  pypi.org instead of the maintainers' mirror; CI for Python 3.11–3.13; the version in
  `__init__.py` only; publishing from a tag with trusted publishing; `CONTRIBUTING.md` and
  `SECURITY.md`. Deferred by the owner: no external contributors yet. Build on the first
  external pull request or issue, or at the start of `v0.3.0`. The optional `mcp` extra is
  breaking, so it is left to `v0.3.0` as its own candidate. Awaits the §G verdicts.

## Deferred work
- **User manual** — *comprehensive* build guide. A lean consumer entry point now exists at
  [`docs/getting_started.md`](../docs/getting_started.md); the full manual stays deferred and
  will grow as the toolkit stabilizes (and as the co-designed agent surfaces real needs).
- **PyPI publish** — no longer deferred: decided 2026-10-29, first public release `v0.2.2`,
  tracked in checkpoint Next Steps. Of the 2026-06-28 readiness list, the metadata, classifiers,
  URLs, CHANGELOG and `LICENSE` are done. The rest (a single-sourced version, the publishing
  workflow, the optional `mcp` extra) is in the open-source-groundwork candidate above.
  `vsigma` was still free on PyPI and TestPyPI (2026-10-30).
