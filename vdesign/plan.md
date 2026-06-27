# vsigma — Plan & Roadmap

Design board and high-level roadmap. Status: `[ ]` todo · `[/]` in progress · `[x]` done.

**Guiding rule:** start with the simplest MVP that proves the composition seam, then grow
complexity gradually — each phase an *extension*, not a rewrite.

**Design reference:** the model these phases build on is the
[Composition Model proposal](../docs/proposals/composition-model.md) (Status: PROPOSAL —
graduates into `docs/architecture_design.md` once Phase 1 validates it). Do not restate the
model here; this file tracks *what gets built when*.

---

## Phase 0 — Project setup `[ ]`
*Operationalize the environment before writing logic ("building is part of architecture").*

- [ ] `git init`; add `.vibe/` to root `.gitignore`
- [ ] Python project scaffold with `uv` (`pyproject.toml`, package layout, `Makefile`)
- [ ] Test harness + lint/type-check wired so a new dev runs everything with one command

## Phase 1 — Minimal seam slice (the MVP) `[ ]`
*Goal: a runnable agent composed from blocks, proving the wrapper seam + loop/run model.*
*This is the slice that validates the [Composition Model proposal](../docs/proposals/composition-model.md).*

- [ ] `BaseState` (ids + control fields, lean — no payloads)
- [ ] `GraphBuilder` — `add`, `route_conditional`, `compile`
- [ ] `react_node` — the ReAct loop primitive (LLM behind an injectable interface; default Claude)
- [ ] Resilience: `break_on_attempts` + `HUMAN_INTERRUPT`
- [ ] The node wrapper seam, with **memory + observability live** and **context as pass-through**
- [ ] Service interfaces + trivial defaults: `InMemoryStore`, `TrajectoryLog`
- [ ] `Run` handle: `agent.run()` / `run.resume()`, backed by an in-memory checkpointer
- [ ] One runnable demo graph + tests (composes; loop-breaker escalates and resumes)
- [ ] **Decision Check Point:** does the seam hold with no structural rewrite? If yes, graduate the proposal.

## Phase 2 — Routing & planning `[ ]`
- [ ] Effort-estimation block as a conditional edge (cheap path vs. heavy path)
- [ ] Plan-and-Solve loop primitive

## Phase 3 — Critique loop & worked example `[ ]`
- [ ] Critique / Reflexion block (`critic_node` with a deterministic validator)
- [ ] Assemble the software-engineering worked example end-to-end (from the product spec)

## Phase 4 — Real memory & context `[ ]`
- [ ] Memory tiers (working / session / long-term) + an external durable backend
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
