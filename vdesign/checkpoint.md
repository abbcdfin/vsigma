# Checkpoint

> Volatile session-handoff memory (per `.vibe/vibe.md`). For the durable roadmap see
> [`plan.md`](./plan.md); for settled design see `docs/`. Last updated: **2026-07-04**.

## Where we are

- **Branch:** `dev`. Tags: `phase-0`, `phase-1-mvp`, `phase-2`, `pre-toolkit-layering`, `phase-2.5`.
- **Status:** **Phase 2.5 complete** — the augment-not-replace refactor landed. `make check`
  green (26 tests); demo runs at `python -m examples.web_browsing.demo`.

## Architecture as of now (important — changed in 2.5)

- **vsigma augments LangGraph, doesn't replace it.** `GraphBuilder` was **removed**.
- **Two levels:**
  - *Templates* (fast path; return an `Agent` whose `.graph` is a real compiled graph) —
    a graceful-degradation ladder: `make_react_agent` (flat baseline) →
    `make_guarded_react_agent` (+ human-interrupt) → `make_plan_execute_agent` (+ planning).
  - *Primitives* — `wrap`/`Services` (seam: node→node) on a raw `StateGraph`; `add_wrapped`/
    `add_router` smooth LangGraph's strict typing; run via `Agent` → `Run`.
- **Modules:** `seam.py` (Services/wrap/add_wrapped/add_router), `run.py` (Agent + Run),
  `agents/` (templates). `builder.py` deleted.
- Tools: `Tool` is the spine; sources adapt in (`tool()`, `from_langchain`, `make_mcp_tools`).
- Offline testing: `FakeLLM` + `call`/`say`; `StaticLLM` always-finish stub.

## Key context

- **LLM is OpenAI-compatible, auth still pending** (`default_llm()` from `VSIGMA_LLM_*`;
  offline default `FakeLLM`). Not yet smoke-tested against a live model/web.
- **Dogfooding:** bumblebee (sibling project `../agents/bumblebee`, feedback in its
  `docs/vsigma_feedback.md`) is the consumer. Its 2 logged items are resolved (LangChain tool
  adapter + `react_node` fail-fast; offline `FakeLLM` discoverability/helpers).
  - **3rd item (code-edit signal, not logged):** bumblebee patched the templates so
    `recursion_limit` scales with `limit` (was hardcoded 25 → loop-breaker couldn't escalate at
    high `limit`, raising `GraphRecursionError`). Resolved upstream the right way: all three
    templates now take an explicit `recursion_limit` override; guarded/plan default to a
    documented `_backstop(limit)` (`limit + 20`, per-`invoke` sizing since resume resets the
    counter); flat exposes the knob too (its only bound). +3 tests (30 total green).
- **Proposals (`docs/proposals/`):** `toolkit-layering` **VALIDATED**, `mcp-tool-adapter`
  **VALIDATED**, `composition-model` (construction part superseded by toolkit-layering),
  `memory-construction` (Phase 4; expanded 2026-07-04 with §C three axes scope×kind×impl,
  §D one construction engine/three feeds + record provenance + **constructor seam &
  file-bundle contract** (external agents build memory offline; validate/ingest gate;
  schema-second), §E two-round consolidation
  reflexive+deep — not started), `running-context-model` (**new** 2026-07-04: persona /
  transport-agnostic multi-channel session / episode; multi-persona + visibility; defines the
  memory scope axis).

## Open / pending

- **Graduate** the VALIDATED proposals (toolkit-layering, mcp-tool-adapter; composition-model
  execution model) into `architecture_design.md` (consolidation pass).
- **Live smoke test** once LLM auth is provided.

## Next Steps

1. **Phase 4 (memory) is next by user decision (2026-07-04)** — prioritized over Phase 3 and
   over a messaging-bus idea (deferred: no reactive consumer yet; `interrupt`/`resume` covers
   minimal external input). Design vocabulary is settled in the two proposals above; start
   with the scope ladder + interface signatures. Open `vdesign/active_plan.md` for the JIT
   breakdown.
2. Respond to further bumblebee feedback as it lands.
3. Consolidate validated proposals into `architecture_design.md` at a lull.
   (Phase 3 — critique block + worked example — slots in when convenient.)
