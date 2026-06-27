# Proposal: Memory Adapter Layer & Construction Utilities

> **Status: PROPOSAL — awaiting validation.** A concrete design for how users prepare and
> grow application-specific memory. Not yet settled architecture. Depends on the real
> long-term memory tier (see [`vdesign/plan.md`](../../vdesign/plan.md), Phase 4) and must be
> validated there before graduating into [`architecture_design.md`](../architecture_design.md).
> Builds on the [Composition Model proposal](./composition-model.md). Do not treat as doctrine.

## Motivation

A generic toolkit becomes a *specialized* agent through its **application-specific memory** —
the domain context kept as *data*, not hardcoded into the engine (the *"Generality Before
Specialization / Prompt-as-Data"* principle). Users typically arrive with **raw,
unstructured documents**, so vsigma should provide a paved path from "pile of docs" → "memory
the agent can read," plus utilities to extend it later. Construction quality also directly
bounds retrieval quality, since the runtime's context-assembly step reads from this memory.

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

### Boundary (Minimal Mechanism)

Do **not** build a full RAG/knowledge-management product (parsers, embedders, vector DBs) —
that space is well-served and drifts toward Phase 2. vsigma supplies the **contract + thin
utilities + adapters**; everything heavy is delegated. Users can always bring their own
pipeline and write through the interface.

## Relationship to the MVP

Nothing to build now. The only seam to protect in Phase 1 is that the memory interface is
**read+write symmetric and shape-agnostic**, so the adapter layer and construction utilities
are pure extensions later — no rewrite.

## Open questions (to settle during Phase 4)

* How opinionated is the paved path vs. "bring your own pipeline"? (Lean: paved-but-thin.)
* Which memory shapes ship first? (Lean: semantic chunks + a summary layer; graph later.)
* CLI + library, or library only?
* The exact `build` / `retrieve` method signatures and the shape-config schema.

## Graduation criteria

Promote into [`architecture_design.md`](../architecture_design.md) only after:

* The real long-term memory tier exists (Phase 4) and the symmetric interface is proven by
  *both* a construction utility and the runtime retrieve path using it unchanged.
* At least one model adapter × one backend adapter compose without a bespoke class.
* The incremental `extend` path is demonstrated (bootstrap, then add/update) on real docs.
