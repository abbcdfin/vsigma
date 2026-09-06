# Proposal: Memory Subsystem — Interface, First-Party Implementations & Construction

> **Status: CORE GRADUATED (2026-07-04)** into
> [`architecture_design.md`](../architecture_design.md) §§8–9 — kept as the historical
> record. Graduated (implemented + tested): the symmetric interface, scope×kind namespaces,
> `SQLiteStore`, the `distill` engine with mandatory provenance, the bundle contract +
> `validate`/`ingest` gate (schema v0.1, schema-second), two-round consolidation (deep
> supersedes reflexive). **Still proposal (the outer ring):** semantic index/embeddings,
> external vector adapters, the strategy library, source-hash prune, and the cross-store
> assembly merge — those sections below remain live design, not doctrine.
> Builds on the [Composition Model proposal](./composition-model.md); the scope axis is defined
> by the [Running-Context Model proposal](./running-context-model.md). Do not treat as doctrine.
>
> **2026-07-04 additions (recorded decisions §C–§E):** three orthogonal axes
> (scope × kind × implementation); one construction engine with three feeds + record-level
> provenance (injected vs consolidated); two-round consolidation (reflexive vs deep);
> the **constructor seam + file-bundle contract** (external agents build memory fully
> offline; validate/ingest gate; schema-second sequencing).

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

**Refinements / risks (the value lives here, not in the raw "LLM reads the docs" step):**

* **Provenance is mandatory.** Every distilled fact links back to its source span, so it is
  auditable and the agent can cite/verify. An LLM-built base that is *confidently wrong* is
  worse than raw docs — the error is baked in and poisons every downstream answer.
* **Constrain the shape.** The LLM *fills a defined target schema* (construct-engine), it does
  not invent structure freely — otherwise the memory is inconsistent, hard to query, and
  hard to test/compare.
* **Cost, scale & incrementality.** Full-LLM passes over large corpora are slow/expensive and
  hard to update. Expect a **hybrid** (cheap index + LLM-distilled layer) and a real **diff /
  incremental rebuild** story when sources change.
* **Reproducibility.** Construction is non-deterministic (same docs → different memory), so
  built memory is **versioned/snapshotted**; otherwise eval and regression testing break.

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

**Refinements / risks:**

* **Not sealed silos — there is flow between them.** A fact learned mid-interaction (context)
  often *should* become durable knowledge. The **promotion / consolidation pipeline** across
  the boundary is central machinery, not a footnote (now specified in §E: two rounds,
  reflexive at run-end + deep at maintenance).
* **Two tiers may not be enough.** This binary lumps working+episodic into "context" and maps
  "knowledge" to semantic. **Procedural / skill memory** (learned how-to) fits neither and will
  likely want its own home later — don't let the binary calcify.
* **The hard problem moves to the merge.** Even stored separately, context-assembly must blend
  knowledge + context into one budgeted window coherently. The split is necessary; the
  **cross-store ranking/merge** at assembly time is where answer quality is won or lost.
* **Don't under-invest the context side.** Knowledge is the shinier target, but many real agent
  failures come from the context side (context rot, bad compaction, stale working memory).

> **Terminology note:** "context memory" (a *kind of stored memory*) is distinct from
> **context management** (the runtime *assembly policy*, see the composition model). The
> context-assembly step *reads from both* knowledge and context memory to build each window.

### C. Three orthogonal axes over one interface

There is no single "split" — memory is **one symmetric interface over three orthogonal
axes**, and conflating them is what makes memory designs muddy:

| Axis | Values | Decides | Nature |
|---|---|---|---|
| **1. Scope** (whose/which) | `global · agent · persona · session · episode · step` | namespace + **visibility rule** + lifespan | conceptual |
| **2. Kind** (what nature) | `knowledge` vs `context` (§B) | write pattern (batch vs streaming) + read pattern (semantic vs recency) | conceptual |
| **3. Implementation** (how) | shape × backend (§2) | storage + build/retrieve logic | config, not concept |

The **scope axis is the
[running-context model](./running-context-model.md)** (persona / transport-agnostic session /
episode, multi-persona with per-scope visibility) — defined there, consumed here. The §2
two-axis adapter layer *is* axis 3. Scope × kind maps what memory exists:

| Scope | **Knowledge** (durable, semantic read) | **Context** (situational, recency read) |
|---|---|---|
| **Global** | world / org facts | — |
| **Agent** | learned skills, procedures, semantic base | — |
| **Persona** | durable user facts (prefs, identity) — *private* | that user's cross-session summary — *private* |
| **Session** | facts established in this dialog (shared) | conversation history, all participants |
| **Episode** | — | this run's trajectory + working state |
| **Step** | — | scratchpad (in-flight reasoning) |

The namespace key is **`(scope-path, kind)`** — e.g. `persona:alice/knowledge`,
`session:S1/context`. The existing `MemoryStore(namespace, key, value)` already carries this:
`namespace` becomes structured; **no interface change**. Consolidation (§E) is the one piece
of machinery spanning the axes: it flows *up* scope and *across* kind (context → knowledge).

### D. One construction engine, three feeds — and record-level provenance

Bootstrap, maintenance, and experience-consolidation are **the same engine with different
source material**, not three subsystems:

```
bootstrap:        raw docs         ──▶ LLM distill (§A) ──▶ build()  ──▶ knowledge
maintenance:      new/changed docs ──▶ (same engine)     ──▶ upsert()
consolidation:    run trajectories ──▶ (same engine, §E) ──▶ upsert()
```

Same target schema, same provenance rules, same versioning — only the feed and the invoking
context differ.

**Injected vs consolidated is provenance, not a scope split.** Knowledge derived from curated
documents ("injected") and knowledge derived from the agent's own experience ("consolidated")
must stay distinguishable — but as a **mandatory record-level field**, not separate
namespaces:

```
record = (scope, kind, content,
          provenance{ origin: injected | consolidated,
                      consolidator: reflexive | deep,      # §E, when consolidated
                      source_ref,                          # doc span | trajectory ref
                      confidence, version })
```

* **Why not a scope split:** retrieval must not fork on origin (the agent wants the best
  relevant knowledge, period — two namespaces tax the hot path with a fan-out/merge), and the
  duality repeats at *every* scope (agent skills, persona facts) — a sure sign it's an
  attribute, not a level.
* **Why the field is mandatory:** *lifecycle* — re-running bootstrap on changed docs must
  replace doc-derived records without touching experience-derived ones; *trust* — consolidated
  knowledge is agent-inferred and can be confidently wrong, so retrieval can weight by
  origin/confidence and bad consolidations can be pruned without disturbing the curated base;
  *audit* — extends §A's provenance mandate (source is a doc span *or* a trajectory ref).
* **Graduation path:** consolidated knowledge that proves out (or passes review) is promoted
  to curated status by flipping provenance — no data migration.

**The constructor seam — the engine's distiller is not necessarily an LLM call.** Because
build is offline/batch (§1 asymmetry), the engine depends on a **constructor** seam — anything
that takes (sources, target schema, scope) and produces candidate records:

1. **LLM-backed** — the §A picture; fits small corpora and the reflexive round (§E).
2. **External agent CLI** — a full coding agent (e.g. Claude Code, Antigravity) invoked
   headless. For corpus distillation an agent beats a bare completion: it navigates files
   itself, iterates, and can run the validator in its own loop (self-verification).
3. **A vsigma-built construction agent** — the deep pass (§E) assembled from vsigma's own
   templates with a capable model; the toolkit eats its own dogfood.

**The constructor contract is a portable file bundle, not an API.** An external constructor
must be able to build memory **completely offline, with no vsigma installed**. So the
contract is an interchange artifact:

```
bundle/
  manifest.json    # schema version, target scope, constructor identity
                   # (model/agent + version), source inventory w/ content hashes
  records.jsonl    # one record per line, in the §D record shape
  sources/         # optional source snapshots so provenance refs resolve
```

* **The agent proposes; the engine commits.** The bundle is a staging area (a directory).
  Live memory is only ever touched by `vsigma memory ingest <bundle>`, after the
  deterministic gate `vsigma memory validate <bundle>` (schema conformance, provenance refs
  resolve, scope legal) passes. A constructor never writes to the store directly — §A's
  "confidently wrong" risk applies with more force to agents, which compound their own errors.
* **Constructor-agnostic by construction.** Claude Code, Antigravity, a vsigma agent, a
  script, or a human editing JSONL are equal citizens; the standard, not the tool, is the
  contract. `provenance.consolidator` generalizes to record *which* constructor
  (model/agent + version) produced each record.
* **Hard requirements become conventions.** Versioning/reproducibility (§A) = snapshot the
  bundle; audit = read the file; incremental maintenance = a new bundle whose manifest source
  hashes drive the replace-injected/keep-consolidated lifecycle above.
* **Offline feeds only.** Bootstrap, maintenance, and the deep round use bundles; the
  reflexive round (§E) stays a bounded in-process LLM call writing through the interface.

**Schema-second sequencing (guardrail).** Commit *now* to the bundle contract + validate/
ingest gate + a schema version from the first byte. Do **not** spec the full JSON Schema up
front: the `content` serialization across shapes (kv facts vs semantic chunks vs graph
triples) is unsolved, and freezing a standard ahead of a working concrete case is the
`GraphBuilder` mistake. The schema is **extracted from the first end-to-end Phase-4 slice**
(real docs → bundle → validate → ingest → agent retrieves), then versioned and treated as a
public commitment (`ingest` refuses unknown versions; changes are migrations).

### E. Two-round consolidation (reflexive vs deep)

Run-end consolidation must be cheap/local (it runs on the agent's clock, with whatever model
the field agent has); good distillation wants the most capable model available (offline). One
round cannot be both, so there are **two**:

| | **Round 1 — reflexive** (agent, autonomous) | **Round 2 — deep** (maintenance, external LLM) |
|---|---|---|
| When | run-end hook, online | batch, offline, operator-triggered |
| LLM | the agent's own (possibly small / air-gapped) | most capable model available |
| Input | this episode's trajectory | raw trajectories *across episodes* + round-1 output |
| Output | episode summary, provisional facts → session/persona | high-quality distillation, cross-episode patterns |
| Trust | **provisional** (low confidence) | may **promote, revise, merge, prune** round-1 records |

Rules that keep this sound:

* **Still one engine (§D).** Reflexive vs deep is *configuration* of the same construction
  procedure (which constructor, input window, depth budget). Round 1 = engine invoked by the
  run-end hook with the agent's own LLM (in-process); round 2 = engine invoked by the
  maintenance CLI with any constructor from the §D seam — including a full agent producing a
  bundle.
* **Raw trajectories are the source of truth.** Round-1 output is provisional and
  *re-derivable*: the deep pass reads raw trajectories, not just round-1 summaries, so a bad
  reflexive consolidation is superseded, never compounded. ⇒ **Retention requirement:**
  trajectories are kept durably per episode at least until the deep pass has processed them.
* **Round 2 is the trust boundary.** `provenance.consolidator` records which round produced a
  record; reflexive records sit at low confidence (quarantinable from high-stakes retrieval)
  until the deep pass promotes them (§D graduation path). Cross-episode synthesis ("the agent
  keeps failing at X") is *only* possible in round 2.
* **Round 1 is bounded and skippable.** Strict budget on the agent's clock; with no LLM at
  all it degrades to mechanical trajectory capture and round 2 does everything. Quality
  degrades gracefully; nothing breaks.

## Relationship to the MVP

Nothing to build now. The only seam to protect in Phase 1 is that the memory interface is
**read+write symmetric and shape-agnostic**, so the first-party implementations, strategy
library, knowledge/context split, and LLM-driven construction are all pure extensions later —
no rewrite.

## Open questions (to settle during Phase 4)

* Which first-party stores/shapes ship first? (Lean: SQLite long-term + semantic chunks +
  a summary layer; graph/episodic later.)
* The exact `build` / `retrieve` method signatures and the shape-config schema (now including
  the structured `(scope-path, kind)` namespace and the mandatory provenance record of §D).
* How knowledge vs. context surface in the interface — separate handles, or one interface with
  namespaces/policies?
* LLM-driven construction: prompt/rubric design, cost controls, and how its output shape is
  validated.
* ~~CLI + library, or library only?~~ **Settled by §D:** at minimum
  `vsigma memory validate` + `vsigma memory ingest` (the bundle gate) ship as CLI.
* The bundle record schema itself — deliberately deferred (schema-second, §D): extracted from
  the first end-to-end Phase-4 slice, then versioned.
* Reflexive-round budget: what bound (tokens/latency) is acceptable at run-end, and the exact
  degraded behavior when the agent has no LLM.
* Trajectory retention policy: how long raw episodes are kept after the deep pass has
  processed them. Two invariants constrain whatever policy lands (recorded 2026-09-06,
  during Phase 4.7):
  * **Payload may be pruned; identifiers may never be recycled.** A `run_id` is one episode
    for its whole lifetime — LangGraph thread, episodic namespace, maintenance cursor entry,
    and the `source_ref` of every record derived from it. The deep pass *adds* a permanent
    `deep:<run_id>` marker; nothing releases an id. Reclaiming bytes must not reclaim names.
  * **A pruned episode needs a tombstone, and the `episodes/index` entry is not one.**
    The index carries ownership and creation time, so `trajectory:<run_id>` still resolves to
    *who and when* after the payload is gone — but that is attribution, not auditability. To
    keep a pruned source auditable a tombstone must also carry the content hash of what was
    pruned and its consolidation disposition, so a record's lineage can still be *checked*
    rather than merely named. Cost note: what grows is the trajectory, not the bookkeeping —
    the seam persists each node update including the cumulative `messages`
    (`seam.py`), so a single episode's stored bytes grow superlinearly in its step count
    while the per-episode index and marker rows stay flat.

## Graduation criteria

Promote into [`architecture_design.md`](../architecture_design.md) only after:

* The real long-term memory tier exists (Phase 4) and the symmetric interface is proven by
  *both* a construction utility and the runtime retrieve path using it unchanged.
* At least one model adapter × one backend adapter compose without a bespoke class.
* The incremental `extend` path is demonstrated (bootstrap, then add/update) on real docs.
* Provenance round-trips: a doc-source rebuild replaces injected records while leaving
  consolidated ones untouched (§D), proven by a test.
* Both consolidation rounds run through the *same* engine (§E): the run-end hook (reflexive)
  and an offline pass (deep) differ only in configuration, and the deep pass supersedes a
  round-1 record.
* **The independent-constructor test (§D):** an external agent (e.g. Claude Code), given only
  real documents + the published bundle schema — **no vsigma installed** — produces a bundle
  that `vsigma memory validate` + `ingest` accept and the runtime retrieves from. If an
  independent agent can't produce a valid bundle from the schema alone, the standard has
  failed its one job.
