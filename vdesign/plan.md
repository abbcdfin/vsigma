# vsigma — Plan & Roadmap

Design board and high-level roadmap. Status: `[ ]` todo · `[/]` in progress · `[x]` done.

**Guiding rule:** start with the simplest MVP that proves the composition seam, then grow
complexity gradually — each phase an *extension*, not a rewrite.

**Design reference:** the model these phases build on is the
[Composition Model proposal](../docs/proposals/composition-model.md) (Status: PROPOSAL —
graduates into `docs/architecture_design.md` once Phase 1 validates it). Do not restate the
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

## Phase 4 — Real memory & context `[ ]`
*Memory work follows the [memory subsystem proposal](../docs/proposals/memory-construction.md);*
*its scope axis is the [running-context model](../docs/proposals/running-context-model.md).*

- [ ] **Scope ladder in code** — `user_id` (persona) on `BaseState`; structured
      `(scope-path, kind)` namespaces; persona-visibility rule on the retrieve path
- [ ] Symmetric memory interface + two-axis adapter layer (memory model/shape × storage backend)
- [ ] **First-party (batteries-included) stores** behind the interface — light-dep defaults
      (in-process working + SQLite long-term + small semantic index); external vector DBs opt-in
- [ ] **Knowledge vs. context split** — distinct subsystems handled differently (see proposal §B)
- [ ] **Strategy library** (innovation surface, opt-in, measured): summarization/compaction,
      reflective consolidation, weighted retrieval, episodic↔semantic promotion, forgetting
- [ ] **One construction engine, three feeds** (proposal §D): bootstrap from raw docs
      (LLM-driven, build-side only) + incremental `add`/`update`/`prune` + trajectory
      consolidation — with mandatory record-level **provenance** (injected vs consolidated)
- [ ] **Two-round consolidation** (proposal §E): reflexive run-end hook (agent's own LLM,
      bounded/skippable) + deep offline maintenance pass (capable LLM; may promote/revise/prune
      round-1 records); durable trajectory retention until the deep pass runs
- [ ] Context management: real assembly + compaction, slotted into the existing seam

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
