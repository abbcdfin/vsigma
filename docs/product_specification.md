# vsigma — Product Specification (What & Why)

> **Maturity:** Developer preview; see the authoritative
> [project status](../README.md#project-status) and current
> [implementation roadmap](../vdesign/plan.md). This document defines the problem vsigma
> addresses, the building blocks it offers, and how they compose into a real agent. The
> *how* (tech stack and engineering decisions) lives in
> [`architecture_design.md`](./architecture_design.md).

## 1. Vision

**vsigma** is a *universal* toolkit for architecting, implementing, and orchestrating
cognitive networks — the foundational plumbing for building autonomous agents. It does not
ship one opinionated agent; it ships modular building blocks you compose into the agent your
problem needs. The same blocks assemble a deterministic data pipeline, a latency-sensitive
chat agent, or a long-horizon research system — they differ only in *which* blocks are used
and *how* they are wired.

* **Toolkit Mode (current):** modular, stateful building blocks you *explicitly
  assemble* into a task-specific architecture.
* **Orchestration Engine Mode (future):** a runtime that reads an open-ended goal,
  generates agent definitions, compiles a tailored graph, and runs it.

## 2. The Building Blocks

A single paradigm like **ReAct** is not enough for a generic system. vsigma instead offers a
small set of composable blocks operating at different layers of abstraction. You pick the
ones a given agent needs:

| Block | What it gives you | Reach for it when… |
|---|---|---|
| **Loop primitives** | Pure ReAct (Thought→Action→Observation), Plan-and-Solve, Critique/Reflexion, and Search (Tree/Graph of Thoughts) loops | The task ranges from a single tool call to a multi-step plan, a retry-on-failure cycle, or branch-and-backtrack exploration |
| **Effort estimation** | A fast pre-step that rates task complexity, so a router can branch to the right machinery | You want trivial work to stay cheap and only escalate complex work |
| **Memory systems** | Constraint-aware, tiered, pluggable memory (ephemeral working memory ↔ durable long-term recall) | Agents differ in latency budget, context size, and how much they must remember |
| **Context management** | The active layer that assembles each prompt: selection/retrieval, compaction, **tool scoping**, prioritization | The window is finite and you must decide what the model sees (and which tools it can call) each step |
| **Resilience** | Loop-breakers (`attempt_count`) that escalate to a human-interrupt node | Any cyclic relationship could otherwise run forever or act unsafely |
| **Observability** | Append-only trajectory capture (thoughts, tool calls, observations) + resource metrics | You need to evaluate, debug, or tune behavior |

> vsigma leans on standard protocols (e.g. **MCP**) for tool *transport and extensibility*;
> its own concern is *which* tools are exposed and gated in a given context, which is why
> tool scoping lives inside context management rather than being a separate subsystem.

The mechanics of each block are in [`architecture_design.md`](./architecture_design.md).

## 3. Building an Agent with vsigma — A Worked Example

To make the composition concrete, here is how a user would assemble **one** agent. The same
method applies to any archetype; this is just one point in the toolkit's space.

### Goal & constraints

> *Build a software-engineering agent that takes a feature request, writes code, and keeps
> fixing it until the tests pass — escalating to a human if it gets stuck.*

* **Loops needed:** a retry cycle (Critique/Reflexion) wrapped around tool-calling (ReAct).
* **Constraints:** must remember repo conventions across runs (durable memory); must not
  loop forever (resilience); should not waste a heavy plan on a one-line fix (estimation).

### How the blocks compose

1. **Entry router + effort estimation.** A macro router runs the effort estimator on its
   entry edge. Trivial changes route straight to a lightweight ReAct loop; substantial
   features route into the full implementation subgraph. *(Blocks: effort estimation,
   routing.)*
2. **Implementation subgraph — the Critique loop.** `plan → implement → validate → (repeat)`.
   The *implement* node is a ReAct loop with code-editing tools; the *validate* node is a
   deterministic critic that compiles and runs the test suite. Failures are appended as a
   structural critique and fed back to the next attempt. *(Blocks: Plan-and-Solve + ReAct +
   Critique loops.)*
3. **Memory.** A durable long-term store holds repo conventions and prior decisions; the
   active graph state holds only pointers and the current `attempt_count`. *(Block: tiered
   memory.)*
4. **Context management.** Code-editing tools are scoped to *only* the implementation
   subgraph; the growing critique history is compacted so the window stays lean. *(Block:
   context management + tool scoping.)*
5. **Resilience.** If `attempt_count` crosses its threshold, the loop-breaker routes to a
   `HUMAN_INTERRUPT_NODE` for review instead of retrying forever. *(Block: resilience.)*
6. **Observability.** Every attempt — plan, edits, test results — is appended to the
   trajectory log for later debugging and tuning. *(Block: observability.)*

### What changes for a different agent

Swap blocks, keep the method. A **latency-sensitive chat agent** drops the Critique loop and
durable store, leaning on lean working memory and a tight context budget. A **research
agent** swaps the Critique loop for Search loops and adds aggressive compaction for a
long horizon. *Same toolkit, different composition* — which is the whole point.

## 4. Beyond This Example

This document shows the *shape* of building with vsigma. The runnable consumer path now lives
in [`getting_started.md`](./getting_started.md), backed by offline examples and tests. A
comprehensive reference manual and expanded archetype walkthroughs remain deferred until the
public API stabilizes; current implementation status belongs in the roadmap rather than being
duplicated here.
