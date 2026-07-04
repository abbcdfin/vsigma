# Checkpoint

> Volatile session-handoff memory (per `.vibe/vibe.md`). For the durable roadmap see
> [`plan.md`](./plan.md); for settled design see `docs/`. Last updated: **2026-07-04**.

## Where we are

- **Branch:** `dev`. Tags: `phase-0`, `phase-1-mvp`, `phase-2`, `pre-toolkit-layering`,
  `phase-2.5`, `templates-react-ladder`, `pre-phase-4`, `phase-4`.
- **Status:** **Phase 4 core complete** (2026-07-04) — real memory landed. `make check`
  green (**68 tests**); demo runs at `python -m examples.web_browsing.demo`.
  Since then: `RecallContext` v0 (read-side loop, plan 4.5 head-start);
  **Phase 4.1 complete** — `LoggingLLM` live tap at the LLM protocol seam with
  `enable_llm_logging()`/`disable_llm_logging()` (stdout/file, silent by default);
  **Phase 3 complete** (built after 4, deliberately) — `critic_node` + `Validator`
  (`critique.py`), `make_reflexion_agent` (both routers `break_on_attempts`; never-valid
  → escalate, not spin; backstop 2·limit), worked example `examples/code_kata/`
  (test-running subprocess validator; `python -m examples.code_kata.demo`). **78 tests.**
- **Phase 4 modules:** `scope.py` (ladder + visibility + `recall`), `records.py`
  (`MemoryRecord`/`Provenance`), `stores.py` (`SQLiteStore`), `construction.py` (`distill`),
  `bundle.py` (write/validate/ingest gate), `cli.py` (`vsigma memory validate|ingest`),
  `consolidation.py` (reflexive hook via `Services.run_end` + `deep_consolidate`),
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
- **Proposals (`docs/proposals/`):** consolidation pass done (2026-07-04) — the settled
  *how* now lives in `docs/architecture_design.md` (⚙ implemented / ✎ design intent).
  **GRADUATED:** `toolkit-layering` (§2), `composition-model` (§§3–4; GraphBuilder part
  superseded), `mcp-tool-adapter` (§7), `running-context-model` (§5),
  `memory-construction` **core** (§§8–9). Still live proposal material: the memory outer
  ring (semantic index, vector adapters, strategy library, source-hash prune, cross-store
  merge) inside `memory-construction.md`.

## Open / pending

- **Live smoke test** once LLM auth is provided.
- Pending validation demos noted in the design doc: multi-episode session rehydration;
  MCP HTTP transport.

## Next Steps

1. **Phase 4.5** — semantic recall + `RecallContext` scoring upgrade + minimal eval
   slice; trigger: a consumer (bumblebee or the kata example) hits the retrieval wall,
   or eval trajectories are wanted.
2. Respond to further bumblebee feedback as it lands (the memory system is new surface —
   expect signal; **tell agy the memory/bundle + reflexion API exists** — user-side nudge).
   Live smoke test once LLM auth is provided. (Messaging bus stays deferred: no reactive
   consumer yet. Phase 5 multi-agent is the next unbuilt phase after 4.5.)
