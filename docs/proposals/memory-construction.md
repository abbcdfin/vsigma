# Proposal: Memory Subsystem — Interface, First-Party Implementations & Construction

> **Status: PROPOSAL — awaiting validation.** A concrete design for the memory subsystem: a
> common interface, **first-party (batteries-included) implementations**, a strategy library
> as an innovation surface, LLM-driven construction, and a knowledge/context split. Not yet
> settled architecture. Targets Phase 4 (see [`vdesign/plan.md`](../../vdesign/plan.md)); must
> be validated there before graduating into [`architecture_design.md`](../architecture_design.md).
> Builds on the [Composition Model proposal](./composition-model.md). Do not treat as doctrine.

## Motivation

A generic toolkit becomes a *specialized* agent through its **application-specific memory** —
the domain context kept as *data*, not hardcoded into the engine (the *"Generality Before
Specialization / Prompt-as-Data"* principle). Users typically arrive with **raw,
unstructured documents**, so vsigma should provide a paved path from "pile of docs" → "memory
the agent can read," plus utilities to extend it later. Construction quality also directly
bounds retrieval quality, since the runtime's context-assembly step reads from this memory.

Memory is also one of the **most important and most innovatable** parts of an agent: small
improvements here compound into large performance gains. So vsigma treats memory as a
first-class, first-party subsystem — shipping built-in implementations (not only adapters to
external stores) so users unfamiliar with memory systems get a strong default, **and** as a
deliberate surface for novel, measured techniques that differentiate vsigma.

## The model

### 1. One symmetric memory interface (build + retrieve)

Construction is the **write** side and the runtime is the **read** side of the **same**
contract. There is never a second memory model (One Fact, One Place).

```
   construction utilities (offline) ──▶ build() / upsert() ─┐
                                                            ├─ one Memory interface
   context.assemble (runtime)       ──▶ retrieve() / query()┘
```

> **Asymmetry to respect:** *build* is offline/batch and heavy; *retrieve* is
> online/latency-sensitive. The same adapter implements both, but they are called by
> different callers at different times — build-time concerns must not leak into the retrieve
> path.

### 2. Two-axis adapter layer (avoid the N×M explosion)

"Different memory models" and "different backends" are **separable axes**. The adapter layer
keeps them apart so combinations are *compositions*, not bespoke classes:

| Axis | What varies | Examples | Decides |
|---|---|---|---|
| **Memory model (shape)** | *how* memory is organized | semantic chunks · hierarchical summaries · key-value facts · knowledge graph | the *logic* of build + retrieve |
| **Storage backend (where)** | *where* it persists | dict · vector DB · KV store · graph DB | persistence primitives |

Layering: **interface → memory-model adapter (shape) → storage-backend adapter
(persistence).** A model adapter expresses build/retrieve in terms of generic backend
primitives; a backend adapter just persists. Selecting a shape is a **data-driven config
choice** (construct-engine), not a code branch.

### 3. Construction utilities (thin, lean on existing tools)

vsigma's contribution is **orchestration + adapters**, not reinvented ingestion. Heavy
lifting (loading, chunking, embedding) leans on existing tooling (e.g. LangChain loaders,
`unstructured`, MCP, vector stores). The utilities:

* **Bootstrap:** raw docs → load → chunk/extract/summarize (per chosen shape) → `build()`
  through the memory interface into the long-term store.
* **Extend (incremental):** `add` / `update` / `prune` so users grow memory over time —
  re-runnable and incremental, so downstream memory updates when sources change (Auto
  Propagation).
* **Surface:** a small library + CLI (e.g. `vsigma memory build/extend …`), a *build-time*
  concern kept separate from the agent runtime loop.

### 4. First-party implementations (batteries-included)

vsigma **ships built-in implementations behind the interface** — not just adapters — so a
user with no memory expertise gets a strong default, and so memory becomes an innovation
surface vsigma owns. This *fills in* the two-axis adapter layer; it does not change it.

**Built-in stores** (defaults must stay dependency-light):

| Tier | First-party default | Dependency |
|---|---|---|
| Working | in-process (`InMemoryStore`, exists) | none |
| Session / long-term | file / **SQLite** store | stdlib |
| Semantic recall | small embedded index (optional embeddings) | light / optional extra |
| Production vector | **adapter** to pgvector / Chroma / … | external (opt-in) |

**Strategy library** (the memory-model/shape axis — the innovation surface): rolling /
hierarchical summarization & compaction; reflective consolidation (synthesize higher-order
memories from raw episodes); importance × recency × relevance weighted retrieval; episodic ↔
semantic separation with a promotion pipeline; learned forgetting / pruning. Strategies are
**opt-in** behind the interface so they cannot destabilize the core.

### Boundary (Minimal Mechanism, revised)

Ship **first-party defaults + a strategy library**, but keep the discipline:

* **Light defaults.** Built-in stores use stdlib/SQLite; heavy/production backends
  (vector DBs, graph DBs) stay **opt-in adapters**, not core dependencies.
* **Don't reinvent production search.** Adapt pgvector/Chroma/etc. rather than building a
  vector engine; lean on existing loaders/embedders for ingestion plumbing.
* **Innovation must be measured.** Novel strategies are wired to the observability/eval
  machinery so a claimed gain is demonstrated on trajectories, not assumed — and they remain
  opt-in.
* Users can always bring their own pipeline/backend and write through the interface.

## Design principles (recorded decisions)

### A. LLM-driven memory construction

Construction should **use state-of-the-art LLMs to analyze the support documents and generate
a well-constructed memory** — not merely mechanical chunk-and-embed. The LLM reads the
sources and produces an organized structure: distilled facts, entities/relations, hierarchical
summaries, and the appropriate shape for the material. This is the *deconstruct-then-structure*
analogue of the effort-estimation "deconstruct-then-rate" idea, and is itself a first-party
**construction strategy** and prime innovation surface. (Its cost lives entirely on the
offline *build* side, never the latency-sensitive retrieve path.) Quality is measured against
downstream retrieval/agent performance.

### B. Separate knowledge from context

The memory subsystem is **two subsystems handled differently**, not one undifferentiated store:

| | **Knowledge memory** | **Context memory** |
|---|---|---|
| What | durable, factual, domain knowledge from documents/experience | situational state of the current interaction |
| Examples | distilled facts, entities, summaries, references | conversation history, working scratchpad, recent events |
| Lifecycle | curated, slowly changing, shared across sessions/agents | volatile, recency-ordered, session-scoped |
| Write | **batch construction** (ideally LLM-driven, §A) | streaming append + compaction |
| Read | semantic search / grounding | recency × relevance |
| Maps to | semantic / long-term tier | working / session / episodic tiers |

They have different write patterns, retrieval, durability, and even storage shapes, so the
subsystem keeps them as distinct namespaces/adapters behind the common interface.

> **Terminology note:** "context memory" (a *kind of stored memory*) is distinct from
> **context management** (the runtime *assembly policy*, see the composition model). The
> context-assembly step *reads from both* knowledge and context memory to build each window.

## Relationship to the MVP

Nothing to build now. The only seam to protect in Phase 1 is that the memory interface is
**read+write symmetric and shape-agnostic**, so the first-party implementations, strategy
library, knowledge/context split, and LLM-driven construction are all pure extensions later —
no rewrite.

## Open questions (to settle during Phase 4)

* Which first-party stores/shapes ship first? (Lean: SQLite long-term + semantic chunks +
  a summary layer; graph/episodic later.)
* The exact `build` / `retrieve` method signatures and the shape-config schema.
* How knowledge vs. context surface in the interface — separate handles, or one interface with
  namespaces/policies?
* LLM-driven construction: prompt/rubric design, cost controls, and how its output shape is
  validated.
* CLI + library, or library only?

## Graduation criteria

Promote into [`architecture_design.md`](../architecture_design.md) only after:

* The real long-term memory tier exists (Phase 4) and the symmetric interface is proven by
  *both* a construction utility and the runtime retrieve path using it unchanged.
* At least one model adapter × one backend adapter compose without a bespoke class.
* The incremental `extend` path is demonstrated (bootstrap, then add/update) on real docs.
