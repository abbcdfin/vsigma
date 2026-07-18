# Proposal: The Procedure-Extraction Pass — Rung 2 of the Autonomy Ladder

**Status:** DRAFT 2026-07-18 — awaiting review.
**Depends on:** dynamic-tools (DRAFT) for the consumption side (`load_skills`, §B.6
there); shares the staging→gate→admit discipline with agent-authored-tools (DRAFT),
of which this is deliberately the *instruction-only tier* — the rung that can run
fully autonomously because its gate is mechanically decidable.
**Prompted by:** the operator's standing goal of an agent that extends its own
capability without intervention. Fact distillation already does this at sentence
granularity; this proposal gives *multi-step procedures* the same autonomous loop.

## A. Problem

vsigma's learning loop is closed today only for atomic facts: trajectory → distill →
knowledge record → `RecallContext`. Procedures — ordered, conditional, multi-step
"how to do X" knowledge — cannot ride that loop, for three structural reasons:

1. **Atomicity.** The distill contract demands one self-contained fact per line. A
   procedure forced through it either shatters into one-liners that lose their
   ordering, or is rejected as a shape violation.
2. **Always-on economics.** `RecallContext` pushes recalled facts into every window.
   Right for one-liners; wrong for 40-line procedure bodies, which would tax every
   run whether relevant or not.
3. **Push-only recall.** The model cannot *open* a procedure on recognizing the
   situation that calls for it; there is no pull.

Yet trajectories demonstrably contain procedures: a run that succeeded at a
multi-step task *is* a worked example. The knowledge is being generated and thrown
away — the same waste the memory system was built to stop, one level of structure up.

## B. Design

### 1. Where it runs: a sibling of the deep round, not of the reflexive one

The pass lives in maintenance, next to `run_maintenance` — same batching machinery,
a different question over the same retained trajectories: not "what facts emerged?"
but **"did these episodes demonstrate a reusable multi-step procedure not already in
the library?"** Run-end is the wrong home: procedure recognition is deliberative,
wants the capable model, and has zero same-day urgency.

Mechanics mirror `run_maintenance` exactly:

- own markers — `proc:<run_id>` in `MAINTENANCE_NS` — so a procedure-pass failure
  never blocks fact-consolidation retries and the two passes can run on different
  cadences;
- markers advance **only after successful admission** (the maintenance invariant);
  zero-procedure batches are marked, or they retry forever;
- own CLI verb — `vsigma memory procedures --db … --skills-dir …` (naming open to
  review) — with `--dry-run` as the review point, cron owning cadence as always.

### 2. The artifact: constrained JSON in, mechanical SKILL.md out

The model never emits markdown. Like `distill`, the extraction prompt demands
constrained output — one JSON object per procedure:

```json
{"name": "renew-tls-cert", "description": "Renew a TLS certificate when expiry is near",
 "steps": ["...", "..."], "source_refs": ["episode:abc123"]}
```

vsigma *renders* the SKILL.md package from that mechanically — frontmatter from
`name`/`description`, body from `steps`, provenance block from `source_refs` plus
`authored-by: agent`, extraction date, and the constructor identity. Rendering being
mechanical is what makes the safety property structural: **the pass is physically
incapable of emitting an executable file** — it only ever writes one markdown
document per procedure. Shape violations raise, the batch retries, nothing partial
lands (the `distill` discipline, unchanged).

The prompt carries this pass's analog of the durability bar, stated as concrete
exclusions small models can follow:

- extract only procedures that are **reusable and generalized** — parameterize away
  run-specific values (ids, hostnames, dates); a procedure containing a literal from
  one run is a rejected extraction;
- **multi-step or don't bother** — anything expressible as one sentence belongs to
  fact distillation, which already runs on these episodes;
- extract nothing that merely restates an existing library entry (the model is shown
  the current library's names + descriptions as context).

### 3. The gate: mechanical, therefore honestly automatic

This is the rung where "no intervention" is *earned*, not waived. The admission gate
checks only properties a machine can decide with certainty:

| check | rule |
|---|---|
| shape | valid frontmatter: kebab-case `name`, non-empty `description` |
| payload | the package contains exactly one `SKILL.md` — text, no scripts, no other files |
| identity | no name collision with any existing package (see §B.4) |
| bounds | body under a size ceiling; batch under a per-pass procedure cap |

No LLM judges the gate. The worst artifact that can pass is a *bad instruction* —
the same risk class as a wrong memory fact, which the system already auto-admits at
0.3 confidence. That equivalence, not optimism, is the justification for automation.

### 4. Admission: the agent's shelf is not the operator's shelf

Admitted packages land in `<skills-dir>/authored/<name>/` — a subtree the pass owns.
Two rules make coexistence safe:

- **operator space is inviolate**: the pass never writes outside `authored/`, and a
  name collision with *any* existing package — operator- or agent-authored —
  refuses admission (logged, not overwritten). Operator packages always win.
- **provenance is the directory**: everything under `authored/` is by construction
  agent-written, so audits, exports, and bulk revocation (`rm -r`, or
  `catalog.disable`) need no bookkeeping beyond the tree itself.

Default behavior is auto-admit — that is the point of the rung — with
`--stage-only` for operators who want the review workflow anyway. Consumption is
dynamic-tools §B.6 unchanged: `load_skills` picks the package up at the next
construction (or next step, via the catalog's hot-load path); name + description
ride in the spec; the body enters context only when invoked.

### 5. The trust tier (deferred hook, designed now)

Authored packages start *provisional* in their provenance block. Promotion and
retirement — invoked-and-followed-by-success raises standing, never-invoked decays
it — is the usage-based aging already parked for Phase 4.5, applied to procedures
and facts under one mechanism. Nothing in this proposal blocks on it; the
provenance field just leaves the slot.

## C. Non-goals / hard lines

- **Executable output — never.** The renderer writes markdown only; the gate
  refuses any package that is not exactly one text file. If a procedure needs a
  script, the procedure can *say so* and the operator can grant one — through the
  agent-authored-tools pipeline, with its heavier gate.
- **The pass never touches gate code, admission rules, or operator packages** — the
  self-extension loop must not reach the mechanism that bounds it.
- **Semantic deduplication** ("this new procedure is 90% of an existing one") —
  needs Phase 4.5 embeddings; until then, name-collision refusal plus the
  library-context prompt line are the dedup story.
- **Run-end procedure extraction** — deliberateness is the design; no reflexive
  variant.

## D. Graduation criteria

- Live loop, no human in it: bumblebee completes a multi-step task; the pass
  extracts a procedure; a *later* run — facing the same class of task — invokes the
  skill and follows its steps to success. Transcript as evidence.
- A generalization check: the admitted body contains none of the source run's
  literals (ids, hosts, dates).
- The collision test: an authored candidate matching an operator package's name is
  refused and logged; the operator package is byte-identical after the pass.
- The payload test: no invocation of the pass can produce a non-markdown file under
  `authored/` (property held across the whole test suite, not one case).
- A zero-procedure batch advances markers and is never re-examined.

When validated, this graduates into architecture_design beside the maintenance pass,
as the second question the deep round asks — and rung 2 of the autonomy ladder
recorded in agent-authored-tools becomes operational fact rather than roadmap.
