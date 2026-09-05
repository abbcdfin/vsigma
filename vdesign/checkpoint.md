# Checkpoint

> Volatile session-handoff memory (per `.vibe/vibe.md`). For the durable roadmap see
> [`plan.md`](./plan.md); for settled design see `docs/`. Last updated: **2026-09-05**.

## Where we are

- **Branch:** `dev`, HEAD `6942c4b`. Tags: `phase-0`, `phase-1-mvp`, `phase-2`,
  `pre-toolkit-layering`, `phase-2.5`, `templates-react-ladder`, `pre-phase-4`, `phase-4`,
  `phase-4.2`, `phase-4.3`, `phase-4.4`, `phase-4.6`.
- **Status:** **Phase 4 core complete** (2026-07-04) — real memory landed. `make check`
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
- **Coordinator Dogfooding Pass complete** (2026-07-23) — resolved 6 high-friction items from live-model consumer (`VSIGMA_FEEDBACK.md`):
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
  - *Memory scope promotion* (`memory-scope-promotion`, revision 2 — bumblebee dogfooding
    review + a second review round). Key reframe: there is **no record→record promotion in
    vsigma at all**; both consolidation rounds are *construction* from raw trajectory, so
    the defect is that construction picks its own destination scope, widest-first.
    **Slice 1 = verified safety fix, landable ahead of a full verdict:** one destination
    rule (narrowest durable scope, else no durable write — `_knowledge_scope` currently
    widens to persona, and sessionless to `AGENT`, which `visible_scopes` returns for every
    persona, while the mechanical summary ten lines above narrows to session); durable
    `episodes/index` for ownership (episodic context is keyed on `run_id` alone today);
    mandatory `--source` + explicit `--all-sources` (`run_maintenance`'s `--scope` names the
    destination, not the source — one pass currently distills every tenant's episodes into
    one persona); pass `sources=` to `write_bundle`. **Slice 2 = promotion proper**
    (phase-sized): trust vs. scope axes, `PromotionRequest(source_scopes, target_scope,
    policy)`, policy-driven transformation (`verbatim`/`generalize`/`redact`), gate-policy
    seam over the structural-only `validate_bundle`, structured lineage at bundle schema
    v0.2, operation-marker idempotence (content addressing doesn't survive generative
    policies), per-policy recall dedup. Open Qs in §F; decisions logged in §G.

## Open / pending

- **Live smoke test** once LLM auth is provided.
- Pending validation demos noted in the design doc: MCP HTTP transport
  (multi-episode session rehydration retired by Phase 4.2's SQLite test).

## Next Steps

1. **`memory-scope-promotion` Slice 1 — the one actionable item that isn't verdict-gated.**
   It fixes verified defects (unvetted provisional knowledge auto-widened to persona/`AGENT`;
   `run_maintenance`'s unconstrained source = a multi-tenant leak), needs no schema change,
   and its §E graduation criteria are already written as testable assertions. Everything
   else below waits on review.
2. **Review the open DRAFT proposals** (above). `async-bridge` is accepted + shipped;
   the capability-extension set, the two context-economics proposals, and
   `memory-scope-promotion` Slice 2 await verdicts. On acceptance, natural first slices:
   `load_skills(path)` (dynamic-tools; also the standing commitment to agy) and, for
   context economics, either the `Session` `dialog_policy` or `TokenBudgetContext`.
3. **Phase 4.5** — semantic recall + `RecallContext` scoring upgrade + minimal eval
   slice; trigger: a consumer (bumblebee or the kata example) hits the retrieval wall,
   or eval trajectories are wanted. (The token-budget + scoring design now exists on paper.)
4. Respond to further bumblebee feedback as it lands (the memory system is new surface —
   expect signal; **tell agy the memory/bundle + reflexion API exists** — user-side nudge).
   Live smoke test once LLM auth is provided. (Messaging bus stays deferred: no reactive
   consumer yet. Phase 5 multi-agent is the next unbuilt phase after 4.5.)
