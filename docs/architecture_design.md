# vsigma — Architecture Design (How)

> **Status: settled design, current through Phase 4.** This document records the *validated*
> architecture — every section marked ⚙ is implemented and tested; sections marked ✎ are
> design intent still ahead of code. The **code is the source of truth**; graduated proposals
> remain in [`docs/proposals/`](./proposals/) as historical records with their rationale and
> refinement history. The *what & why* lives in
> [`product_specification.md`](./product_specification.md).

## 1. Tech Stack ⚙

* **Runtime:** [LangGraph](https://langchain-ai.github.io/langgraph/) as the state-machine
  runtime. vsigma deliberately does *not* reimplement checkpointing, threads, or async.
* **Language / toolchain:** Python ≥ 3.11, managed with `uv`; ruff + mypy-strict + pytest
  (`make check`). First-party defaults are dependency-light (stdlib SQLite, no vector DB in
  core).

## 2. Toolkit Layering — Augment, don't Replace ⚙

*(graduated from the [toolkit-layering proposal](./proposals/toolkit-layering.md))*

vsigma is an **opinionated layer that augments LangGraph**, never a wrapper that hides it.
The one construction facade we built (`GraphBuilder`) was removed when the first consumer
proved it subtracted capability. Three tiers:

1. **Primitives** — building blocks (`react_node`, `plan_node`, `break_on_attempts`,
   `estimate_effort`, …) plus the seam (`wrap`/`Services`, sugar `add_wrapped`/`add_router`)
   applied to a **raw `StateGraph` you own**. Full control, full LangGraph power.
2. **Templates** — one-line prebuilt topologies (`vsigma/agents/`), a
   **graceful-degradation ladder**: `make_react_agent` (flat baseline — "less structure,
   more intelligence", bounded only by a tunable `recursion_limit`) →
   `make_guarded_react_agent` (+ human-interrupt loop-breaker; backstop coupled to the
   attempt limit) → `make_plan_execute_agent` (+ up-front planning). A template returns an
   `Agent` whose `.graph` is a real, inspectable compiled graph — never a sealed box — and is
   built only on public blocks.
3. **Orchestration engine** *(Phase 2 product, future)* — generates/picks graphs from a
   goal; templates are its curated precursor.

Model-awareness rule: explicit declared-capability hints are acceptable (transparent,
opt-in); inferring topology from a model string is rejected for v1.

## 3. The Cross-Cutting Seam ⚙

*(graduated from the [composition-model proposal](./proposals/composition-model.md))*

Memory, context management, and observability are **not embedded in nodes**. `wrap` is a
composable primitive (`node → node`) that weaves them around any node; the node author
writes only reasoning + action:

```
        ┌─ wrap(node, services) ──────────────────────────────────┐
state ─▶│ 1. context.assemble(state, memory)     (pre-call)        │
        │ 2. node(assembled_window)              (your logic)      │
        │ 3. memory.write(episode-scope capture) (post-call)       │
        │ 4. observer.emit(trajectory event)     (post-call)       │
        └─────────────────────────────────────────────────────────┘
```

`Services` bundles the injected implementations (`memory`, `observer`, `context`, optional
`run_end` hook — see §9). Strategy changes happen in one place; the seam is proven on raw
`StateGraph`s and inside every template. (`seam.py`)

## 4. Execution Model ⚙

*(graduated from the composition-model proposal; "episode" naming from the
[running-context model](./proposals/running-context-model.md))*

| Concept | What it is | Maps to | Lifespan |
|---|---|---|---|
| **Loop** | the compiled graph (`Agent`) — reusable definition | code | reused |
| **Run / Episode** | one bounded execution for one task-arc, incl. resumes | a LangGraph thread | one task |
| **Session** | continuity envelope grouping runs | a memory namespace, *not* a thread | many runs |
| **Task** | the goal a run pursues | the run's state | one run |
| **Step** | one node firing | a trajectory event | one node exec |

* **Lean state.** `BaseState` carries identifiers + control fields only (`task_id`,
  `run_id`, `session_id`, `user_id`, `parent_run_id`, `attempt_count`, `status`,
  `memory_ref`) — never payloads; those live behind memory. (`state.py`)
* **Runs are ephemeral; sessions are durable namespaces.** A run is born fresh and dies; no
  idle process between tasks. Session continuity is externalized memory that the next run
  rehydrates. We reject "session = one long thread" (checkpoint growth, process pinning).
* **`Agent.run(task) → Run`** (`run.py`): status/result/trajectory, `resume()` after a
  human-interrupt (LangGraph `interrupt()`/`Command(resume=)`), a `recursion_limit`
  backstop, and the run-end consolidation hook.
* **Multi-agent** ✎ *(design settled, `delegate` not yet built — Phase 5)*: **subgraph
  composition** (child graph as a node; same run) vs **subagent delegation** (child *run*,
  fresh state, own thread/trajectory, `parent_run_id`). The harness owns the spawn; a
  subagent is a nested run.

## 5. Running-Context Model ⚙

*(graduated from the [running-context-model proposal](./proposals/running-context-model.md);
interaction grains 2–3 refined in the [interaction-model proposal](./proposals/interaction-model.md))*

The scope ladder — **who is talking, over what, inside which unit of work** — is the memory
scoping backbone (`scope.py`):

```
global ─ agent ─ persona ─ session ─ episode ─ step
```

* **Namespaces are structured scope paths**: `(scope-path, kind)` →
  `persona:alice/knowledge`, `session:s1/context`. The `MemoryStore` protocol is unchanged;
  scopes ride on the namespace string.
* **Sessions are transport-agnostic.** Channel (Slack/email/voice) is **event metadata,
  never a scope**; one session may span channels; correlation/stitching is the channel
  adapter's job. ✎ The inbound event envelope `(author_persona, session, channel, payload)`
  is defined but built only when a real reactive consumer pulls a messaging seam.
* ⚙ **Multi-round dialog: the `Session` handle** (`session.py`, Phase 4.2). `Session` is
  the spine transports adapt into — every delivery path (direct call, HTTP adapter, the
  future bus) terminates in `session.send(payload, channel=...)`, same pattern as `Tool`
  (spine in core, sources adapt in; the engine never learns about transports). One `send`
  = one episode seeded with the dialog so far (user/assistant turns only — tool traffic,
  recall injections and critique feedback are episode-local noise); the dialog persists
  to `session:<id>/context`, so handles **rehydrate across process restarts** (tested on
  SQLite — retires the pending rehydration demo). Envelope compatibility: each turn is
  persisted as `(payload, user_id, channel, run_id)` metadata the model never sees.
  Grain unification: `send` on an escalated run routes to `resume()` — one verb for
  human input, mid-run or between runs. Dialog compaction/summarization is deliberately
  deferred to 4.5's measured assembly; the bus boundary is fixed in the proposal §C.
* **Multi-persona sessions; visibility is a property of scope.** A session has participants;
  each turn records its author. `visible_scopes`/`recall` enforce the rule on the retrieve
  path: global/agent → all; session → its participants; **persona → that persona only**
  (tested: private memory never crosses personas).
* **Task is correlation metadata, not a rung.** A ladder level must earn its place with
  distinct *visibility* or *lifespan* semantics (the same test that put channel on events
  and origin in provenance). Task has session's visibility, and cross-attempt continuity
  ("what did prior attempts at this task learn?") flows through **consolidation to session
  scope**, whose records carry the `task_id` (tested: a retry correlates prior attempts).
  If a consumer ever pulls a real task scope, adding `"task"` to `ScopeLevel` + the bundle
  schema is purely additive — old bundles stay valid.

## 6. Loop Architecture & Resilience ⚙

The loop taxonomy (see product spec) maps onto nested layers — micro (pure ReAct
tool-calling), meso (plan-execute-refine / critique), macro (multi-agent state machine) —
realized concretely as the template ladder (§2) plus:

* **Routing via conditional edges** — routers (`Router`, `loop_until_done`,
  `break_on_attempts`, `estimate_effort`) are cheap conditional edges, never heavy nodes.
* **Loop-breakers + escalation** — every cycle carries `attempt_count`; at the limit the
  edge routes to `HUMAN_INTERRUPT` (pause via `interrupt()`, continue via `run.resume()`,
  attempt budget reset on resume). The `recursion_limit` backstop is coupled to the attempt
  limit so clean escalation always beats the hard stop. (`resilience.py`, `routing.py`)
* **Critique / Reflexion** — `critic_node(validator)` closes generate → validate → revise
  with a *deterministic* validator (`str -> None | critique`): accepted candidates stay
  `done`; rejections are appended as `[critique]` feedback and the loop revises.
  `make_reflexion_agent` wires it so act's `done` is only a *candidate* (routed to the
  critic, not END) and **both** routers are `break_on_attempts` — a candidate that always
  finishes but never validates escalates instead of spinning. One attempt ≈ act + critic
  = 2 super-steps, so its backstop is sized `_backstop(2·limit)`. The worked example is
  the code kata (`examples/code_kata/`): validator = run the tests in a subprocess — the
  domain where an objective critic exists for free. Recorded limitation: the loop
  converges to "passes the tests", not "is correct". Core ships only contract algebra and
  seam adapters — `all_of` (chain clauses, first critique wins) and `llm_judge` (adapter
  from the `LLM` protocol to the `Validator` contract, like `plan_node`/`distill`: rubric'd
  second model, PASS protocol, fails closed to the attempt budget; keep it last — the one
  deliberately non-deterministic clause). Concrete clauses (format contracts, test
  runners) are consumer/example code, same rule as tools (decided 2026-07-04: `expect_json`
  demoted from core to a getting_started snippet). The gate bar: a validator needn't prove
  correctness, only reject the demonstrably broken; if no clause is writable, use the
  guarded template — a gate that can't reject is ceremony. (`critique.py`)
* **Effort estimation** — `estimate_effort` branches cheap-vs-heavy paths as a conditional
  edge. ✎ The richer hybrid pipeline (deconstruct-then-rate, few-shot anchors, look-ahead
  scratchpads, deterministic math offloading) remains design intent for when estimation
  quality is measured.

## 7. Tools ⚙

*(graduated from the [mcp-tool-adapter proposal](./proposals/mcp-tool-adapter.md))*

**`Tool` is the spine; everything else is a source.** The loop depends only on the
in-process `Tool` contract (name, description, JSON-schema parameters, sync `func` →
observation string); sources adapt in, and the engine never learns about them:

```
plain Python fn ──── tool(fn) ─────────┐
LangChain tool ───── from_langchain ───┼─▶ Tool ─▶ react_node / LLM
MCP server ───────── make_mcp_tools ───┘
```

* **MCP adapter** (`mcp.py`): `make_mcp_tools(connect) → MCPToolset`; `ConnectionFactory`
  abstracts transport (stdio, in-memory for tests); async→sync bridged on a background
  event-loop thread so `Tool.func` stays sync. Wrapped tools scope and trace identically to
  native ones. ✎ Still open: HTTP transport, pooling, MCP resources/prompts.
* **Fail-fast contract**: `react_node` accepts vsigma `Tool`s only and says exactly how to
  adapt anything else.
* Domain tools (e.g. web browsing) are **examples**, never core.

## 8. Memory ⚙

*(graduated — core — from the [memory subsystem proposal](./proposals/memory-construction.md)
§C–§E; the outer ring below stays proposal)*

**One symmetric interface over three orthogonal axes, plus a consolidation pipeline.**

* **The interface** (`memory.py`): `write / read / query` — construction is the write side,
  the runtime the read side of the *same* contract (build is offline/heavy; retrieve is
  online/latency-sensitive; build-time concerns never leak into the retrieve path).
* **Axis 1 — Scope**: the running-context ladder (§5) — namespace + visibility + lifespan.
* **Axis 2 — Kind**: `knowledge` (durable, batch-written, semantically read) vs `context`
  (situational, streamed, recency-read). The seam's per-node capture writes episodic
  context; construction/consolidation write knowledge.
* **Axis 3 — Implementation**: shape × backend adapters. First-party backends:
  `InMemoryStore` (working) and `SQLiteStore` (stdlib, single file — session/long-term),
  proven interchangeable behind the protocol. ✎ Semantic index (optional embeddings extra)
  and external vector adapters (pgvector/Chroma) are opt-in, not core.
* **Records carry mandatory provenance** (`records.py`):
  `(scope, kind, content, provenance{origin: injected|consolidated, consolidator,
  source_ref, confidence, constructor, version})`. Injected-vs-consolidated is a *field*,
  not a scope split: retrieval never forks on origin; lifecycle (doc rebuilds replace
  injected, spare consolidated), trust weighting, and audit all key off it. Promotion =
  flipping provenance, no migration.
* **One construction engine, three feeds** (`construction.py`): `distill(sources, llm, …)`
  serves bootstrap (raw docs), maintenance (changed docs), and consolidation
  (trajectories). Output is **constrained** — the constructor fills the record schema, one
  JSON fact per line, or the pass is rejected. Record ids are content-addressed, so a later
  pass supersedes rather than duplicates.
* **The constructor contract is a portable file bundle, not an API** (`bundle.py`, `cli.py`):
  `manifest.json` + `records.jsonl`; published schema
  [`docs/schemas/memory-bundle.schema.json`](./schemas/memory-bundle.schema.json) (v0.1,
  extracted schema-second from the working slice, drift-pinned to code by test). External
  agents (Claude Code, Antigravity, scripts, humans) build memory **fully offline with no
  vsigma installed**; live memory is only touched by `vsigma memory ingest` after the
  deterministic `validate` gate — *the agent proposes, the engine commits*. Proven by the
  independent-constructor test (a handmade bundle passes the gate).
* ✎ Outer ring (still proposal): the strategy library (weighted retrieval, promotion
  policies, forgetting — opt-in and measured), source-hash-driven prune, shape variety
  beyond kv facts.

## 9. Consolidation — Two Rounds, One Engine ⚙

*(graduated from memory proposal §E)*

Run-end consolidation must be cheap/local; good distillation wants the best model available
offline. So two rounds of the *same* engine (`consolidation.py`):

| | **Reflexive** (round 1) | **Deep** (round 2) |
|---|---|---|
| When | run-end hook (`Services.run_end`), online | offline, operator-triggered |
| LLM | the agent's own — or none | most capable constructor available |
| Bound | ≤ 1 call over a capped trajectory window; malformed pass skipped, never fatal | batch |
| Output | provisional (confidence 0.3, `consolidator="reflexive"`) | authoritative (0.9, `"deep"`), committed via the bundle gate |

Raw trajectories are the source of truth (the seam's episodic capture *is* the retention);
round-1 output is re-derivable and **superseded** by the deep pass (content-addressed ids —
tested). With no LLM, round 1 degrades to mechanical episode capture; nothing breaks. Deep
is the trust boundary and the only place cross-episode synthesis happens.

The hook fires at **every terminal edge**, not only on success (2026-07-14): the mechanical
episode summary is written for escalated attempts too — so a retry run can find "a previous
attempt failed" in session context — and its fixed id (`episode:<run_id>`) makes
escalate-then-resume an idempotent overwrite ending at the final status. Distillation stays
gated on `done`: a cheap reflexive pass over a failure trajectory can't reliably invert
"what happened" into "what to avoid" and risks storing the mistake as knowledge (and the
distilling LLM is the same one that just failed — correlated). Failure-learning is the deep
round's job. Consequence for hook authors: `Services.run_end` now sees escalated states
(possibly repeatedly across resumes) with the *derived* status in `state["status"]`.

## 10. Context Management ⚙ / ✎

The pre-call assembly slot in the seam (§3) is the *only* place context policy lives.
Implemented: `PassthroughContext` (default), `SlidingWindowContext` (compaction — pins the
system prompt, keeps the recent tail, drops orphaned tool turns), and **`RecallContext` v0**
(the read-side loop: gather via `recall()` under the §5 visibility rule → rank by
provenance confidence → bound by `max_facts` → one *tagged* system message, replaced fresh
each step so the persisted window never stacks injections; composable via `inner`;
**opt-in** — the default never silently injects). ✎ Design intent (Phase 4.5, behind the
same protocol): semantic relevance scoring, token-budgeted cross-store ranking/merge,
progressive multi-layer summarization. The cross-store merge is where answer quality is won
or lost — it lands with the semantic-recall tier and its eval slice.

## 11. Observability ⚙ / ✎

Append-only trajectory storage, separate from lean graph state: the seam emits a structured
`TrajectoryEvent` per step (run/task/session ids, node, attempt, summary) into an `Observer`
(`TrajectoryLog` default); `run.trajectory` filters per run. ⚙ The trajectory observes at
*node* grain; the live tap at *LLM-call* grain is `LoggingLLM` — augment-not-replace applied
to the LLM protocol seam itself (wrap any `LLM`, zero engine change). It logs replies/tool
calls + latency at INFO and the full prompt at DEBUG through stdlib logging (`vsigma.llm`),
toggled by `enable_llm_logging()` / `disable_llm_logging()` and silent by default. ✎ Design
intent: *structured* resource metrics (tokens/latency) on trajectory events, an externalized
sink, and the eval machinery that "innovation must be measured" (memory strategies,
estimation) depends on — Phase 4.5.
