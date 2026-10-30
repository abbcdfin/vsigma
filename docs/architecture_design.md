# vsigma — Architecture Design (How)

> **Maturity:** Developer preview; see the authoritative
> [project status](../README.md#project-status) and current
> [implementation roadmap](https://github.com/abbcdfin/vsigma/blob/master/vdesign/plan.md). This document records the validated
> architecture: sections marked ⚙ are implemented and tested; sections marked ✎ remain design
> intent. The **code is the source of truth**; graduated proposals remain in
> [`docs/proposals/`](./proposals/) as historical records with their rationale and refinement
> history. The *what & why* lives in [`product_specification.md`](./product_specification.md).

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
   `estimate_effort`, …) plus interception points (`wrap`/`Interceptor`, sugar `add_wrapped`/`add_router`)
   applied to a **raw `StateGraph` you own**. Full control, full LangGraph power.
2. **Templates** — one-line prebuilt topologies (`vsigma/agents/`), a
   **graceful-degradation ladder**: `make_react_agent` (flat baseline — "less structure,
   more intelligence", bounded only by a tunable `recursion_limit`) →
   `make_guarded_react_agent` (+ human-interrupt loop-breaker; backstop coupled to the
   attempt limit) → `make_plan_execute_agent` (+ up-front planning). A template returns an
   `Agent` whose `.graph` is a real, inspectable compiled graph — never a sealed box — and is
   built only on public blocks.
3. **Orchestration engine** *(future product mode)* — generates/picks graphs from a
   goal; templates are its curated precursor.

Model-awareness rule: explicit declared-capability hints are acceptable (transparent,
opt-in); inferring topology from a model string is rejected for v1.

## 3. Interception Points & Interceptor ⚙

*(graduated from the [composition-model proposal](./proposals/composition-model.md) and refined in [thread-native-runtime](./proposals/thread-native-runtime.md))*

Memory, context management, and observability are **not embedded in nodes**. `wrap` is a
composable primitive (`node → node`) that weaves them around any node; the node author
writes only reasoning + action:

```
        ┌─ wrap(node, interceptor) ───────────────────────────────┐
state ─▶│ 1. context.assemble(state, memory)     (pre-call)        │
        │ 2. node(assembled_window)              (your logic)      │
        │ 3. memory.write(episode-scope capture) (post-call, if mem)│
        └─────────────────────────────────────────────────────────┘
```

`Interceptor` bundles the injected implementations (`observer`, optional `memory`, `context`, optional
`run_end` hook — see §9). Note that trajectory events (§11) are stream-driven: `Agent.run` and `Run.resume`
consume LangGraph's execution stream (`stream_mode=["updates", "values"], subgraphs=True`) and emit
`TrajectoryEvent`s, guaranteeing events for unwrapped nodes and nested subgraphs without node wrapping.
Memory is optional (`memory=None` by default in templates): an agent can run
with thread-native checkpointer history and zero external memory store writes. When memory is provided,
interception points write episode trajectories and recall context reads from it. Strategy changes happen
in one place; interception points are proven on raw `StateGraph`s and inside every template.
(`interceptor.py`)

## 4. Execution Model ⚙

*(graduated from the composition-model proposal; "episode" naming from the
[running-context model](./proposals/running-context-model.md))*

| Concept | What it is | Maps to | Lifespan |
|---|---|---|---|
| **Loop** | the compiled graph (`Agent`) — reusable definition | code | reused |
| **Run / Episode** | one bounded execution for one task-arc, incl. resumes | a run on a LangGraph thread | one task |
| **Session** | continuity envelope grouping runs | a LangGraph thread (`thread_id = session_id`) | many runs |
| **Task** | the goal a run pursues | the run's state | one run |
| **Step** | one node firing | a trajectory event | one node exec |

* **Lean state.** `BaseState` carries identifiers + control fields only (`task_id`,
  `run_id`, `session_id`, `user_id`, `parent_run_id`, `attempt_count`, `status`,
  `memory_ref`) — never payloads; those live behind memory. (`state.py`)
* **LangGraph `messages` with id-based merging** (Phase 4.9). `ReActState.messages` is
  reduced by LangGraph's `add_messages` (⚙): messages are LangChain objects carrying ids, and
  a returned message whose id matches an existing one merges instead of duplicating.
  `wrap` removes from a node's update any message that was in the window it received,
  preventing echoed messages (such as `RecallContext` injections) from entering thread
  history. Wherever messages leave graph state (the `LLM` protocol, memory capture rows,
  `Session.dialog`), they are converted to plain OpenAI dicts. The system prompt is its own
  `system` field, set every turn, never a message. Subgraphs sharing `messages` nest
  cleanly, paused or not ([langgraph-messages](./proposals/langgraph-messages.md)).
* **Session = Thread; Episode = a tagged run on it.** A session is a LangGraph thread
  (`thread_id = session_id`), and each turn is an episode tagged in checkpoint metadata
  (`run_id`, `task_id`, `user_id`, `channel`). This reverses the earlier rejection of
  "session = one long thread" (Phase 4.8; see `docs/proposals/thread-native-runtime.md` §A.1):
  process pinning disappears under persistent checkpointers with stateless handles.
  `durability="exit"` bounds the checkpoint *count* to one per turn, but each checkpoint
  holds the whole conversation, so a session's storage still grows quadratically, and only
  Redis's checkpointer prunes. ⚙ Built as Phase 4.10
  ([thread-retention](./proposals/thread-retention.md)): opt-in keep-latest checkpoints
  (`checkpoints="latest"`), an opt-in stored message window (`retain_messages=N`), and
  `Session.end()`/`Run.end()` to delete finished threads. ⚙ Phase 4.11
  ([thread-concurrency](./proposals/thread-concurrency.md)): one operation per thread at a
  time (a per-`(checkpointer, thread_id)` lock shared across `Agent`s in one process; a second
  operation raises `SessionBusy`), and optional `interrupt_id` verification on `resume`
  (raising `PauseChanged` if the answered pause is no longer pending).
* **Checkpointer thread expiry** ⚙ Phase 4.12 ([thread-expiry](./proposals/thread-expiry.md)):
  declarative thread expiry (`ThreadExpiry`) across `InMemorySaver` and `SqliteSaver`
  (`idle_ttl`, `keep_last` per user, `paused_idle_ttl`, `run_ttl`), with race-free deletion
  under `thread_lock` (`LockedThread` proof-of-lock) and recovery sweep of unfinished
  episodes in `episodes/status`.
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
* ⚙ **Multi-round dialog: the `Session` handle** (`sessions.py`, Phase 4.8). `Session` is
  the spine transports adapt into. It is a stateless projection over the thread's
  checkpointer snapshot: dialog, pause status, and interrupt payloads are read directly
  via `graph.get_state()`, with no process-local state. `session.send(payload)` starts
  a new episode turn on the thread; if paused at an interrupt, `send` raises `SessionPaused`
  (unless `abandon=True`), avoiding LangGraph's silent drop of pending interrupts.
  Resuming a paused turn is `session.resume(value)` (or `run.resume()`), which reuses the
  paused checkpoint's `run_id`.
  Dialog compaction/summarization is deliberately
  deferred to 4.5's measured assembly; the bus boundary is fixed in the proposal §C.
* **Multi-persona sessions; visibility is a property of scope.** ⚙ `visible_scopes`/`recall`
  enforce the persona rule on the retrieve path: **persona → that persona only** (tested:
  private memory never crosses personas). Global and agent are visible to all, by design.
  ✎ **Session visibility is *not* enforced, and this line used to claim it was.** A session
  scope is admitted to a turn whenever the caller supplies its `session_id`
  (`scope.py:68-69`), and `Session` rehydrates a dialog on that id alone
  (`sessions.py:60`) — there is no participant list and no admission check anywhere in
  `src/`. A `session_id` is therefore a **bearer capability**: whoever holds it reads that
  session's dialog, turn envelopes, episode summaries and (since Phase 4.7) its distilled
  knowledge, whatever persona they serve. Each turn *does* record its author in the envelope
  (`sessions.py`), so the data an admission check needs is already durable — nothing consults
  it. Note also that `visible_scopes(state)` takes no store, so it could never have enforced a
  durable participant list; only `recall`, which holds the `MemoryStore`, is positioned to.
  Until this is closed, **a host must treat `session_id` as a secret and scope it per
  principal** (e.g. a composite `(user_id, session_id)`). Design:
  [session-admission](./proposals/session-admission.md) (DRAFT).
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
  ⚙ Phase 4.12: **Lifespan model and episode status** ([scope-lifespans](./proposals/scope-lifespans.md),
  [thread-expiry](./proposals/thread-expiry.md)). Each rung on the scope ladder has its own lifespan:
  - *Turn/Thread*: Checkpointer threads live until ended or expired by `ThreadExpiry`.
  - *Episode status*: `episodes/status` (`"running" | "escalated" | "done" | "failed" | "abandoned"`)
    tracks turn execution under proof-of-lock. A process crash leaves `running` or `escalated`;
    `ThreadExpiry(store=store)` sweeps unfinished episodes and recovers dead runs as `abandoned`.
  - ✎ *Session memory*: `session:<id>/context` and `session:<id>/knowledge` outlive checkpointer threads
    until forgotten through the forget record ([scope-lifespans](./proposals/scope-lifespans.md) §C.2, awaiting `BaseStore` adapter).
  - ✎ *Episodic memory*: transcripts persist until consolidated and pruned by the memory maintenance pass ([episode-retention](./proposals/episode-retention.md)).
* **Axis 2 — Kind**: `knowledge` (durable, batch-written, semantically read) vs `context`
  (situational, streamed, recency-read). `wrap`'s per-node capture writes episodic
  context; construction/consolidation write knowledge. ⚙ Capture rows are keyed
  `<node>:<uuid4>` — each row stores only that node's newly added messages (linear
  capture, $O(N)$), every firing keeps its own row, including after a resume, and the key
  needs no coordination, so a fan-out cannot collide. **Sequence therefore comes from
  `MemoryStore.query`'s first-write ordering**, which the protocol states as a contract
  (`episode_trajectory` depends on it). An input row (`input:<uuid4>`) is written at run start
  so the task begins the transcript. A capture row is *not* a record: it has no id and no
  provenance; it is the node's update minus the window's messages, with messages stored as
  OpenAI dicts.
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
  pass supersedes rather than duplicates — within one namespace. Ids come from content,
  namespaces from the caller's chosen scope, so the same fact written to a wider scope is an
  added row, not a replacement.
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
| When | run-end hook (`Interceptor.run_end`), online | offline, operator-triggered |
| LLM | the agent's own — or none | most capable constructor available |
| Bound | ≤ 1 call over a capped trajectory window (`REFLEXIVE_WINDOW`); malformed pass skipped, never fatal | batch over the entire concatenated transcript |
| Output | provisional (confidence 0.3, `consolidator="reflexive"`) | authoritative (0.9, `"deep"`), committed via the bundle gate |
| **Destination** | ⚙ the **narrowest durable scope**: the session, or — with no session — no durable write at all. Never persona, never `AGENT`; construction may not widen (`knowledge_scope=` is the explicit escape hatch) | ⚙ the operator's `--scope`, an explicit decision at a human-reviewed moment |

Raw trajectories are the source of truth (`wrap`'s episodic capture *is* the retention);
round-1 output is re-derivable and **superseded** by the deep pass — but only where the deep
pass targets the *same scope*. Record ids are content-addressed; namespaces are not
(`<scope>/<kind>`), so supersession happens by key within one namespace. A deep pass aimed
wider than round 1's session adds a second copy that `recall` returns alongside the first —
a *promotion*, with a cost, not a free upgrade. With no LLM, round 1 degrades to mechanical episode capture; nothing breaks. Deep
is the trust boundary and the only place cross-episode synthesis happens. The reflexive round
bounds its input to the initial task row plus the last `REFLEXIVE_WINDOW` rows, while the deep
pass processes the full trajectory.

The hook fires at **every terminal edge**, not only on success (2026-07-14): the mechanical
episode summary is written for escalated attempts too — so a retry run can find "a previous
attempt failed" in session context — and its fixed id (`episode:<run_id>`) makes
escalate-then-resume an idempotent overwrite ending at the final status. Distillation stays
gated on `done`: a cheap reflexive pass over a failure trajectory can't reliably invert
"what happened" into "what to avoid" and risks storing the mistake as knowledge (and the
distilling LLM is the same one that just failed — correlated). Failure-learning is the deep
round's job. Consequence for hook authors: `Interceptor.run_end` now sees escalated states
(possibly repeatedly across resumes) with the *derived* status in `state["status"]`.

⚙ The deep round is runnable as the **maintenance pass** (2026-07-15, per the
[maintenance-pass proposal](./proposals/maintenance-pass.md), not yet graduated):
`vsigma memory consolidate` batches episodes the pass hasn't seen (markers in the
operational `maintenance/state` namespace — one of two namespaces outside the scope ladder,
alongside `episodes/index`; neither is a rung, `visible_scopes` yields neither, so nothing
in them can reach a prompt)
through distill → bundle → gate; markers advance only after successful ingest, so failures
retry. `--dry-run` is the review point.

⚙ **Two boundaries, both mandatory, easy to confuse.** `--source persona:<id>|session:<id>`
(repeatable, or `--all-sources`) bounds whose episodes may be *read*; `--scope` is where the
distilled knowledge *lands* — the operator's explicit promotion claim. Neither has a default:
one pass over every tenant's episodes, distilled into the single scope the operator happened
to name, is a cross-tenant leak, and `agent` — the former `--scope` default — is returned by
`visible_scopes` for every persona. Absence of a stated boundary is not evidence that none
exists. Ownership comes from the `episodes/index` (`run_id → task/session/persona`, written
at run start); an episode with no entry has *unknown* ownership and matches only
`--all-sources`, never a specific `--source`. `vsigma memory export` is the read-side inspection command. Scheduling is external
(cron) by the §D decision that maintenance runs without the agent. Deferred there:
tombstones/retraction records, per-record scope routing, evict-summarization.

## 10. Context Management ⚙ / ✎

`wrap`'s pre-call assembly slot (§3) is the *only* place context policy lives.
Implemented: `PassthroughContext` (default), `SlidingWindowContext` (compaction — keeps the
recent tail, drops orphaned tool turns; the system prompt is `state["system"]`, never in the
window), and **`RecallContext` v0**
(the read-side loop: gather via `recall()` under the §5 visibility rule → rank by
provenance confidence → bound by `max_facts` → one *tagged* system message, replaced fresh
each step, with view-only context ensuring injections never persist into saved state;
composable via `inner`; **opt-in** — the default never silently injects). ✎ Design intent (Phase 4.5, behind the
same protocol): semantic relevance scoring, token-budgeted cross-store ranking/merge,
progressive multi-layer summarization. The cross-store merge is where answer quality is won
or lost — it lands with the semantic-recall tier and its eval slice.

## 11. Observability ⚙ / ✎

Append-only trajectory storage, separate from lean graph state: stream-driven execution emits a structured
`TrajectoryEvent` per step (run/task/session ids, node, attempt, summary) into an `Observer`
when configured (opt-in; default is `None` to prevent unbounded memory growth; `TrajectoryLog`
is the in-memory sink for tests and debugging); `run.trajectory` filters per run (raising if
no TrajectoryLog observer was configured). ⚙ The trajectory observes at
*node* grain; the live tap at *LLM-call* grain is `LoggingLLM` — augment-not-replace applied
to the LLM protocol seam itself (wrap any `LLM`, zero engine change). It logs replies/tool
calls + latency at INFO and the full prompt at DEBUG through stdlib logging (`vsigma.llm`),
toggled by `enable_llm_logging()` / `disable_llm_logging()` and silent by default.
⚙ *Thinking models are absorbed at this seam* (2026-07-16, not yet graduated — see the
[thinking-models proposal](./proposals/thinking-models.md)): the client lands reasoning
(separate `reasoning_content`/`reasoning` field, or inline `<think>` blocks stripped out of
content) on `LLMResponse.reasoning` — `text` is always clean, reasoning never re-enters the
history — and `LoggingLLM` shows it (`[thought: …]` at INFO, full at DEBUG). Provider
thinking switches pass through verbatim via `extra_body` / `VSIGMA_LLM_EXTRA_BODY`
(deliberately unnormalized) with `max_tokens` as the hard bound; the accepted run-end
pairing for local thinking models is a *distiller* (same endpoint, thinking off) on
`reflexive_consolidator`. ✎ Design
intent: *structured* resource metrics (tokens/latency) on trajectory events, an externalized
sink, and the eval machinery that "innovation must be measured" (memory strategies,
estimation) depends on — Phase 4.5.
