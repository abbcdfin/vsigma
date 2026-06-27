# Proposal: vsigma Composition Model

> **Status: PROPOSAL — awaiting validation.** This is a concrete design reasoned out in
> design discussion, **not** yet settled architecture. It must be validated by the minimal
> MVP (see [`vdesign/plan.md`](../../vdesign/plan.md)) before it graduates into
> [`architecture_design.md`](../architecture_design.md). Do not treat it as doctrine.

## Motivation

vsigma's product spec describes *what* the building blocks are; this proposal pins down the
missing concreteness — **how the blocks snap together** — so the toolkit can be built and
the worked example becomes literal code. Everything else hangs off this model.

## The model

### 1. One tool: the `GraphBuilder`

vsigma ships a single assembler. There is **no separate "orchestrator" component** — an
orchestrator is just an agent whose nodes are other agents (composition is recursive).

* **Blocks are node factories** — `react_node(...)`, `critic_node(...)` each return a node
  (or subgraph).
* **Wiring is edges** — estimation and resilience are *conditional* edges.
* **Cross-cutting services are injected at `compile()`** — memory, context, observability.
* **`compile()` returns the loop; `run()` executes it.**

```python
# vsigma provides: GraphBuilder, BaseState, block factories, service interfaces+defaults
# you provide:     the AgentState fields, the llm, the tools, your domain logic
g = GraphBuilder(AgentState)
g.add("act", react_node(llm=llm, tools=tools))            # loop primitive (block)
g.route_conditional("act", break_on_attempts(             # resilience (block)
    done="__end__", retry="act", escalate=HUMAN_INTERRUPT, limit=3))
agent = g.compile(memory=InMemoryStore(), observer=TrajectoryLog())   # the LOOP

run = agent.run(task="…", session_id="sess-42")           # a RUN
run.status; run.result; run.trajectory; run.resume(...)
```

### 2. Execution hierarchy: loop / run / session / task / step

| Concept | What it is | Maps to | Lifespan |
|---|---|---|---|
| **Loop** | The compiled graph — reusable definition (`agent`) | code | reused |
| **Run** | One bounded execution of the loop for one task | a LangGraph thread (+ checkpointer) | one task |
| **Session** | Continuity envelope grouping a series of runs | a namespace in the memory store | many runs |
| **Task** | The goal a run pursues | the run's `AgentState` | one run |
| **Step** | One loop iteration (a node firing) | a trajectory event | one node exec |

The **task is the fundamental unit**; the **session is optional** (a one-shot pipeline is
just a run). `run_id` and `task_id` are kept distinct even when 1:1, so a task could be
re-run later (new run, same task) without a rewrite.

### 3. Lean state: `BaseState`

Identifiers + control fields only — **never payloads** (those live behind `memory_ref`).

```python
class BaseState(TypedDict):
    session_id: str | None     # optional continuity envelope
    task_id: str               # the fundamental unit of work
    run_id: str                # this execution (≈ task_id for now; kept distinct)
    parent_run_id: str | None  # set when this run is a spawned subagent (nesting)
    attempt_count: int         # resilience / loop-breakers
    status: Literal["planning", "implementing", "done", "escalated"]
    memory_ref: str            # pointer into memory, scoped by session/task ids
```

### 4. The node wrapper seam (cross-cutting services)

Memory, context management, and observability are **not embedded in nodes.** vsigma wraps
every node it adds, so the node author writes only reasoning + action:

```
        ┌─ vsigma wraps every node ───────────────────────────────┐
state ─▶│ 1. context.assemble(state, policy, memory)               │─▶ pointers→payloads,
        │ 2. node.run(assembled_window)   ← your reasoning lives   │   compact, scope tools
        │ 3. memory.persist(results)                               │─▶ write back, update refs
        │ 4. observer.emit(trajectory)                             │
        └─────────────────────────────────────────────────────────┘
```

* **Memory** — injected once at `compile`, reached via a handle; state holds only pointers.
* **Context management** — the *pre-call* step. The node declares a *policy* (what to
  retrieve, which tools, how to compact); the engine executes it (construct-engine: node
  says *what*, engine does *how*). Strategy changes in **one place**.
* **Observability** — the *post-call* step, append-only, keyed by `(session_id, task_id)`.

### 5. Sessions: ephemeral runs + a durable memory namespace

* A run is **born fresh and dies** — the loop holds no state; each task gets a clean
  `AgentState`. There is **no idle process between tasks.**
* The session persists as **externalized state in the memory store**. The next task spins up
  a new run that **rehydrates** session memory on the context-assembly step.
* **`run` ↔ a LangGraph thread** (small, checkpointed only to resume at `HUMAN_INTERRUPT`).
  **`session` ↔ a memory namespace**, *not* a thread. (We reject "session = one long thread"
  because the checkpoint would grow with full history and tie the session to one process.)
* **Consolidation at run-end:** a post-run hook promotes salient working memory into session
  (and possibly long-term) memory — the seam that lets task N+1 see task N.

### 6. Multi-agent: composition vs. delegation

Two distinct mechanisms; the leaf LLM never spawns freely (harness owns the spawn).

| | **Subgraph composition** | **Subagent delegation** |
|---|---|---|
| What | A child graph wired in as a *node* | A child *run* of another loop |
| Execution | Same run/state/thread | Separate run — fresh state, own thread/trajectory |
| Topology | Static, at assembly | Dynamic, at runtime |
| Owned by | the parent/orchestrator graph | a node opting into a `delegate` primitive (a block) |
| Use when | deterministic decomposition | isolated/parallel/curated sub-tasks |

A subagent **is a nested run** — `child.run(task=briefing, parent_run_id=…)` — reusing fresh
state, blast-radius (its own loop-breaker), nested observability, and a result contract for
free. A curated briefing is just *scoped input* to that run.

### 7. Who provides what

* **vsigma:** `GraphBuilder`, `BaseState`, block factories, the service *interfaces* + simple
  defaults, the wrapper seam, the `Run` handle, the `delegate` primitive.
* **You:** the `AgentState` fields, the `llm`, the `tools`, your domain validators.
* **LangGraph (hidden):** `StateGraph`, checkpointing, async, threads.

## Open questions (to settle during the MVP)

* The exact `GraphBuilder` surface (`add` / `route_conditional` / `compile` signatures).
* The memory and context-policy interface contracts.
* Whether the node wrapper is true middleware or an explicit decorator on each factory.
* Trajectory event schema.

## Graduation criteria

Promote into [`architecture_design.md`](../architecture_design.md) only after:

* The minimal MVP (see [`vdesign/plan.md`](../../vdesign/plan.md)) is built on this model and
  the seam holds — i.e. a runnable agent composes from blocks, a run executes a task, and the
  loop-breaker escalates and resumes.
* The wrapper seam is demonstrated to keep memory/observability out of node logic.
* No structural rewrite was required to make the model real.
