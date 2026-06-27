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

## Phase 2 — Routing, planning & tool extensibility `[/]`
- [x] Effort-estimation block as a conditional edge (cheap vs. heavy path) — `estimation.py`
- [x] Plan-and-Solve loop primitive — `planning.py`
      (also: extracted `Router` into `routing.py`, shared by resilience + estimation)
- [ ] MCP tool adapter: `make_mcp_tools(server) → list[Tool]` — source MCP tools through the
      `Tool` contract with **no engine change** (framework infra, e.g. `vsigma/mcp.py`).
      See [MCP tool adapter proposal](../docs/proposals/mcp-tool-adapter.md).
      **Awaiting confirmation on approach (async/in-sync, client lib, offline testing).**

## Phase 3 — Critique loop & worked example `[ ]`
- [ ] Critique / Reflexion block (`critic_node` with a deterministic validator)
- [ ] Assemble the software-engineering worked example end-to-end (from the product spec)

## Phase 4 — Real memory & context `[ ]`
*Memory work follows the [memory adapter layer proposal](../docs/proposals/memory-construction.md).*

- [ ] Memory tiers (working / session / long-term) + an external durable backend
- [ ] Symmetric memory interface + two-axis adapter layer (memory model/shape × storage backend)
- [ ] Construction utilities: bootstrap (raw docs → memory) + incremental `add`/`update`/`prune`
      (library + CLI, leaning on existing loaders/embedders — not a reinvented ingestion engine)
- [ ] Run-end **consolidation** hook (promote working → session/long-term memory)
- [ ] Context management: real assembly + compaction, slotted into the existing seam

## Phase 5 — Multi-agent `[ ]`
- [ ] Subgraph composition helper (graph-as-node)
- [ ] `delegate` primitive — dynamic subagent spawning as nested runs (uses `parent_run_id`)

## Phase 6 — Orchestration Engine Mode (Phase 2 product) `[ ]`
- [ ] Deferred: runtime that reads a goal and generates/compiles the graph automatically

---

## Deferred work
- **User manual** — comprehensive build guide. Deferred until a working MVP exists
  (writing it ahead of the implementation would risk contradicting it).

## Next steps
1. Get the green light on this roadmap.
2. Start Phase 0; when Phase 1 begins, open `vdesign/active_plan.md` for the JIT breakdown.
