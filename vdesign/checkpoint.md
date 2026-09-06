# Checkpoint

> Volatile session-handoff memory (per `.vibe/vibe.md`). For the durable roadmap see
> [`plan.md`](./plan.md); for settled design see `docs/`. Last updated: **2026-09-06**.

## Where we are

- **Branch:** `dev`, HEAD `0ddd9f8`. Tags: `phase-0`, `phase-1-mvp`, `phase-2`,
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
    retention, and worth fixing on its own. Non-goals worth remembering: LangGraph checkpoints are in a store vsigma
    cannot reach and may be the bigger pile, and `seam.py:66` persists cumulative `messages`
    so one long run grows superlinearly on its own — bug-shaped, fixable without this design.
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
  references, three in shipped source. Phase 4.7 is `[/]` BUILDING with a board in
  `active_plan.md` (the board had rotted on completed Phase 3 until 2026-09-05).
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
- **Fix:** the seam owns the key, so it owns the counter — `episodes.next_step`, a per-episode
  ordinal in the operational `episodes/steps` namespace. Key is `f"{label}:{step}"`; `wrap`
  returns the node's update exactly as produced. Chosen over a resume-generation counter
  bumped by `human_interrupt_node`, which would have needed `resilience.py` and `seam.py` to
  cooperate — and `resilience.py` is public, so a consumer's own escalation node would
  silently reintroduce the bug.
- **A first attempt put the counter in graph state (`step_count` on `BaseState`) and caused
  two regressions**, both caught in review and confirmed by repro. (1) A fan-out to two
  wrapped nodes raised `InvalidUpdateError` — LangGraph rejects two writes to an ordinary
  channel in one superstep *even when the values are equal*; the same graph worked unwrapped.
  (2) A consumer's own state schema that doesn't declare the field has it filtered out of
  every update, so the counter read zero forever: 3 firings → 1 row, worse than the bug being
  fixed. **The lesson, now in `seam.py`'s docstring: graph state belongs to the consumer; the
  seam's bookkeeping goes where the seam already has authority.** Reasoning about store-key
  collisions had missed LangGraph's channel semantics entirely.
- **Still open:** `architecture_design.md:246`'s "raw trajectories are the source of truth"
  is *still* not fully true — `episode_trajectory` truncates to `steps[-REFLEXIVE_WINDOW:]`
  (20) and `run_maintenance` calls that same capped function, so the deep pass never sees a
  long episode's beginning. Recorded as a prerequisite in `episode-retention` §B.3 with two
  candidate fixes in §F.1. **141 tests.**

## Open / pending

- **Live smoke test** once LLM auth is provided.
- Pending validation demos noted in the design doc: MCP HTTP transport
  (multi-episode session rehydration retired by Phase 4.2's SQLite test).

## Next Steps

1. **Build Phase 4.7 (memory scope safety)** — BUILDING; board open in `active_plan.md`
   with five steps in dependency order (ownership index first — source selection can't
   enforce a boundary before ownership is recorded). Fixes verified defects (unvetted
   provisional knowledge auto-widened to persona/`AGENT`; `run_maintenance`'s unconstrained
   source = a multi-tenant leak), no schema change, §E already reads as testable assertions.
   Everything else below waits on review.
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
