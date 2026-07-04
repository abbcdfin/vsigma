# vsigma — Plan & Roadmap

Design board and high-level roadmap. Status: `[ ]` todo · `[/]` in progress · `[x]` done.

**Guiding rule:** start with the simplest MVP that proves the composition seam, then grow
complexity gradually — each phase an *extension*, not a rewrite.

**Design reference:** the settled architecture lives in
[`docs/architecture_design.md`](../docs/architecture_design.md) (consolidated 2026-07-04 from
the graduated proposals; ⚙ = implemented, ✎ = design intent). Proposals in
`docs/proposals/` are historical records + the still-open outer ring. Do not restate the
model here; this file tracks *what gets built when*.

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

## Phase 3 — Critique loop & worked example `[ ]`
- [ ] Critique / Reflexion block (`critic_node` with a deterministic validator)
- [ ] Assemble the software-engineering worked example end-to-end (from the product spec)

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

## Phase 4.5 — Semantic recall & measured assembly `[ ]`
*The read-side loop, bundled deliberately: an index alone is a feature nobody calls;
index + assembly + measurement closes ingest → recall → prompt. Unblocks the §10 ✎ ring
in [`architecture_design.md`](../docs/architecture_design.md). Sequenced after Phase 3;
may be pulled forward if a consumer (bumblebee) hits the retrieval wall first.*

- [ ] **Small embedded semantic index** — first-party, behind the `MemoryStore` seam;
      embeddings via an optional extra with a pluggable embedder (offline tests stay
      offline: deterministic fake embedder); external vector DBs remain opt-in adapters
- [/] **`RecallContext`** — **v0 landed early (2026-07-04):** gather (visible scopes,
      §5 rule) → rank by provenance confidence → bound (`max_facts`) → tagged/replaced
      injection (no stacking across steps); composable via `inner`; **opt-in** (default
      stays `PassthroughContext`). *Remaining for 4.5:* relevance × recency scoring,
      token-cap budget with deterministic eviction
- [ ] **Minimal eval/metrics slice** (from §11 ✎): token/latency counters on trajectory
      events + a before/after harness, so the assembly policy is *measured* — the recorded
      bar ("innovation must be measured") for any default
- [ ] Feed-through: cross-store ranking/merge findings recorded back into the
      [memory proposal](../docs/proposals/memory-construction.md) outer ring (strategy
      library stays behind this gate)

## Phase 5 — Multi-agent `[ ]`
- [ ] Subgraph composition helper (graph-as-node)
- [ ] `delegate` primitive — dynamic subagent spawning as nested runs (uses `parent_run_id`)

## Phase 6 — Orchestration Engine Mode (Phase 2 product) `[ ]`
- [ ] Deferred: runtime that reads a goal and generates/compiles the graph automatically

---

## Deferred work
- **User manual** — *comprehensive* build guide. A lean consumer entry point now exists at
  [`docs/getting_started.md`](../docs/getting_started.md); the full manual stays deferred and
  will grow as the toolkit stabilizes (and as the co-designed agent surfaces real needs).
- **PyPI publish** — deferred until the API settles (post Phase 4 + the consolidation pass).
  Decision (2026-06-28): not reserving the name yet. `vsigma` confirmed **available** on PyPI
  (also `vector-sigma`, `vsigma-ai`); fallback is **`vector-sigma`** if `vsigma` is taken.
  Before any upload: readiness pass (LICENSE file, metadata/classifiers/urls, single-sourced
  version, CHANGELOG, optional `mcp` extra) + TestPyPI dry run.

## Next steps
1. Get the green light on this roadmap.
2. Start Phase 0; when Phase 1 begins, open `vdesign/active_plan.md` for the JIT breakdown.
