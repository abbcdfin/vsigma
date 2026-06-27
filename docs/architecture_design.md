# vsigma — Architecture Design (How)

> **Status: Design intent, Phase 1.** This document captures the intended technical
> approach and engineering guidelines. It precedes implementation and will be revised as
> the toolkit is built. The *what & why* lives in
> [`product_specification.md`](./product_specification.md).

## 1. Tech Stack

* **Runtime:** **[LangGraph](https://langchain-ai.github.io/langgraph/)** as the underlying
  state-machine runtime. vsigma deliberately does *not* reimplement async race-condition
  handling, state checkpoints, or token-memory buffers from scratch.
* **Language / toolchain:** Python, managed with `uv`.

## 2. Layered Loop Architecture

vsigma maps its core loop taxonomy (see product specification) onto three nested layers:

```
+-----------------------------------------------------------------+
| Macro-Loop: Multi-Agent State Machine / Router                  |
| (Orchestrates high-level system states and handoffs)            |
+-----------------------------------------------------------------+
                                |
                                v
+-----------------------------------------------------------------+
| Meso-Loop: Plan-Execute-Refine (Critique / Self-Correct)       |
| (Compiles sub-tasks, evaluates outputs, injects feedback)       |
+-----------------------------------------------------------------+
                                |
                                v
+-----------------------------------------------------------------+
| Micro-Loop: Pure ReAct / Tool-Calling Loop                      |
| (Linear, rapid token-by-token reasoning and tool execution)     |
+-----------------------------------------------------------------+
```

## 3. Production Design Guidelines

1. **Routing via conditional edges.** Do not make the router or estimator a heavy node
   inside the execution flow. Implement complexity estimation in LangGraph's
   `add_conditional_edges`, using fast, structured-output LLMs to branch the state to the
   optimal subgraph topology.
2. **Lean state philosophy.** Keep the shared graph state schema lightweight. Store heavy
   payloads, raw files, and large text dumps in an external store; keep only pointers
   (IDs), metadata, task statuses, and error counts in LangGraph state for fast
   checkpointing and easy debugging.
3. **Infinite-loop breakers (escalation path).** For every cyclic relationship (e.g. Code →
   Critique → Code), keep a strict `attempt_count` in the state schema. When the counter
   hits a threshold, the conditional edge must break the cycle and route to a
   `HUMAN_INTERRUPT_NODE` for manual approval.
4. **Modular subgraphs.** Build self-contained graphs for specialized roles (e.g. a
   software-engineering subgraph, a data-parsing subgraph) and drop them as modular nodes
   into a high-level parent orchestrator graph.

## 4. Effort-Estimation Pipeline

The estimation model (see product specification) is realized as a **hybrid pipeline**:

* **Deconstruct-Then-Rate.** An initial LLM call breaks the request into an atomic
  hierarchical breakdown; a structured classification prompt then maps each item against a
  rigid complexity rubric (e.g. a Fibonacci scale).
* **Few-Shot Anchor Calibration.** Historical reference anchors are provided in the system
  context to give the model a standard metric for evaluation.
* **Look-Ahead Scratchpads.** The agent drafts a hidden, low-cost skeleton solution,
  turning an abstract estimation problem into a concrete text-analysis task ("how many
  steps / lines did I just draft?").
* **Deterministic Math Offloading.** The model extracts strict qualitative variables (e.g.
  `num_external_apis`, `database_tables_altered`, `complexity_multiplier`); effort is then
  computed programmatically with a deterministic formula rather than asked of the model.

## 5. Memory Systems

Memory infrastructure (see [product specification](./product_specification.md)) is built
around a single principle: **decouple the memory interface from the storage backend** so an
agent can swap stores as its runtime constraints change without altering its logic.

Intended shape (not yet finalized):

* **A uniform memory interface** — read/write/recall operations the agent depends on,
  independent of where data physically lives.
* **Tiered backends behind that interface** — e.g. in-process working memory for
  latency-sensitive loops, and an external durable store (vector / key-value / relational)
  for long-horizon recall. This mirrors the lean-state philosophy: the agent's active
  context holds pointers and metadata; bulk content lives in the external tier.
* **Constraint-driven selection** — the chosen tier is a configuration concern, consistent
  with the data-driven / construct-engine approach, not hardcoded per agent.

> Concrete backend choices and the interface contract are open design items to be settled
> during implementation.

## 6. Context Management

Context management (see [product specification](./product_specification.md)) is the active
assembly layer that decides what enters the model's window each step. Architecturally it is
realized as a **pipeline applied at the boundary of each LLM call**, not as logic scattered
through individual nodes:

* **A context-assembly step** that gathers candidate material (retrieved memory, tool
  schemas, recent loop outputs) and emits the final prompt payload under a token budget.
* **A compaction stage** implementing progressive, multi-layered summarization — recent
  turns kept verbatim, older history rolled into summaries, consistent with the lean-state
  philosophy (bulk content in external stores, pointers in active state).
* **Tool exposure as configuration.** The set of tools visible in a given subgraph is a
  data-driven decision (construct-engine approach), with the transport handled by standard
  protocols such as MCP rather than bespoke integration code.
* **Budget enforcement** with deterministic prioritization/eviction so a single step cannot
  silently overflow the window.

> The compaction strategy and the assembly interface are open design items to be settled
> during implementation.

## 7. Observability

Observability (see [product specification](./product_specification.md)) is built on
**append-only trajectory storage**, kept separate from the lean graph state so that
checkpointing stays fast:

* **Trajectory log.** An append-only record of thoughts, tool calls, and observations,
  emitted as structured events from the harness — never reconstructed after the fact.
* **Resource metrics.** Token counts, latency, and error/attempt counts captured per step,
  reusing the same counters that drive loop-breakers and routing.
* **Externalized sink.** Trajectories and metrics live in an external store; the LangGraph
  state holds only the lightweight counters needed for control flow.

> The event schema and the choice of tracing/telemetry backend are open design items to be
> settled during implementation.
