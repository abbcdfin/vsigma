# Proposal: Memory Scope Promotion — Widening Visibility as a Deliberate Act

**Status:** DRAFT 2026-09-05, revision 2 — awaiting review.
**Prompted by:** the bumblebee dogfooding review (agy) and a second review pass on
revision 1. Both are accepted in substance; revision 2 folds in every verified finding and
records the decisions taken (§G). The originating thesis stands: bumblebee identifies the
session/persona, but *visibility, consolidation, provenance and promotion are reusable
memory semantics vsigma should own*.
**Reconciles (does not replace):** the scope ladder and its "consolidation flows up"
sentence in [running-context-model](./running-context-model.md); the promotion/consolidation
pipeline in [memory-construction](./memory-construction.md) §E; the operator-triggered
promotion framing in [maintenance-pass](./maintenance-pass.md); the ✎ "promotion policies"
outer-ring line in [architecture_design](../architecture_design.md) §8. Where those four use
the word *promotion*, §A.1 and §C.1 here are the authority on which operation and which axis
they mean.
**Siblings:** [session-dialog-compaction](./session-dialog-compaction.md) and
[token-budget-context](./token-budget-context.md); see §B.5.
**Shape:** two slices. **Slice 1 (§B) is a safety fix** — small, self-contained, no new
concepts, landable immediately. **Slice 2 (§C) is the promotion operation** — phase-sized,
adds a bundle schema revision, a gate-policy seam and a new CLI verb. Slice 1 does not
depend on slice 2.

## A. Problem

### A.1 Construction and promotion are different operations — and only one exists

The word *promotion* in the existing documents covers two operations that are not the same
thing:

- **Construction** — read raw context, produce a **new** knowledge record at a *chosen*
  scope. This is `distill` (`construction.py:49`). Both consolidation rounds do this: the
  reflexive hook and `deep_consolidate` each read `episode_trajectory` — raw episodic
  context — and mint new records.
- **Scope promotion** — take an **existing** knowledge record at scope A and derive a record
  at a wider scope B. **This does not exist anywhere in vsigma today.** There is no
  record→record scope movement in the codebase.

This matters because it renames the defect. The problem is not that promotion is too eager;
there is no promotion. The problem is that **construction chooses its own destination scope
with no constraint, and the widest available option wins by default** (§A.3, §A.4). Slice 1
constrains construction's destination. Slice 2 adds the missing operation.

### A.2 One word, two axes

Independently of the above, "promotion" is also used for two orthogonal movements:

- **Trust promotion** — provisional → curated. Implemented as confidence: `0.3` reflexive
  (`consolidation.py:131`), `0.9` deep (`consolidation.py:142-160`).
- **Scope promotion** — narrow scope → wider scope. Not implemented (§A.1).

§C.1 keeps these apart and states the rule that connects them.

### A.3 Defect: two contradictory destination rules, ten lines apart

`reflexive_consolidator` performs **two** automatic writes, under **different** destination
rules:

| | mechanical episode summary | LLM distillation |
|---|---|---|
| Where | `consolidation.py:98-100` | `consolidation.py:126` |
| Kind | `context` | `knowledge` |
| When | every terminal status | `done` only, `llm` provided |
| Destination | `session(session_id)` **else** `episode(run_id)` | `_knowledge_scope`: `persona(user_id)` → `session` → `AGENT` |
| Direction | narrows | **widens** |

The first rule is safe: session, falling back to the narrowest scope there is. The second
widens to the persona whenever `user_id` is set — which is the common case for any consumer
that uses personas at all. So **unvetted, provisional, 0.3-confidence knowledge is written
directly into the persona's durable cross-session store on every successful run**, without
review and without any record of the decision.

No rationale is recorded anywhere for why these two rules differ. The narrowing rule appears
to be the considered one, and the widening rule the accident. Slice 1 therefore does not
introduce a new safety policy — **it makes `_knowledge_scope` consistent with the rule
already written directly above it.**

### A.4 Defect: the `AGENT` fallback is the widest exposure of all

`visible_scopes` (`scope.py:62-64`) returns `[GLOBAL, AGENT, ...]` **unconditionally** — so
AGENT-scoped knowledge is visible in every turn, of every session, serving every persona. A
run with neither `session_id` nor `user_id` currently lands its provisional facts there. That
is broader exposure than the persona case in §A.3, and it has gone unremarked. Any rule that
fixes §A.3 without also fixing this is incomplete.

### A.5 Defect: the maintenance pass has an unconstrained source (multi-tenant leak)

`run_maintenance` (`maintenance.py:75-94`) takes one `scope` — the *target* handed to
`deep_consolidate` — but derives its source from `unconsolidated_episodes(store)`, which is
**every** unprocessed episode in the store regardless of origin. `vsigma memory consolidate
--scope persona:jian` therefore distills *everyone's* trajectories into jian's persona.
maintenance-pass instructs multi-tenant operators to "run one pass per persona with
`--scope persona:<id>`"; that instruction does not work, because the flag names the
destination, not the source.

### A.6 Ownership is not durably recorded — which blocks the fix for §A.5

Filtering `unconsolidated_episodes` is not sufficient, because vsigma cannot currently
determine who owns an episode:

- Episodic context is keyed on `run_id` alone — `_namespace` (`seam.py:42`) returns
  `episode(run_id).namespace("context")`. The `session_id` is in scope at that moment and is
  passed to the `TrajectoryEvent` (`seam.py:69`), but never enters the memory key.
- `TrajectoryEvent` carries `session_id`, but the default observer is an in-memory
  `TrajectoryLog` — not durable.
- `Session` turn records do associate `run_id` with a session, but building a reverse
  `run_id → session_id` index would mean scanning every session namespace in the store. That
  is fragile and gets slower with every session.

Slice 1 adds the missing durable record (§B.2).

### A.7 Recall double-counts across scopes

`visible_scopes` returns persona **and** session together. A fact promoted verbatim from
session to persona is then recalled twice, competing for the window budget the sibling
proposals are trying to bound.

## B. Slice 1 — the safety contract

Self-contained, no new concepts, no schema change. Fixes §A.3–§A.6.

### B.1 One destination rule for automatic construction

Automatic run-end construction writes to the **narrowest durable scope available, and where
none exists it writes no durable knowledge at all.**

```python
def _knowledge_scope(state: State) -> Scope | None:
    if state.get("session_id"):
        return session(str(state["session_id"]))
    return None   # no durable automatic write; promotion (§C) is the only path wider
```

Returning `None` skips the distillation write entirely — the run is unaffected, matching the
existing "skippable, never breaks the run" contract of the reflexive hook.

**Why not fall back to episode.** Episode scope is visible only during the run that owns it
(`visible_scopes` includes `episode(run_id)` for the current run only), so an episode-scoped
*knowledge* record can never be read again — a write-only sink that looks like a feature. The
existing mechanical summary does fall back to episode, and its own comment gives the game
away: the summary is written "so a retry run can find prior attempts in session context,"
which only works when it lands at *session* scope. The episode fallback is already a dead
letter there; it should not be copied into the knowledge path.

**Why not persona, ever.** Per §A.3 and the settled decision in §G.1: automatic widening of
unvetted data is the defect, and a `user_id` is an identity claim, not a review.

**Escape hatch.** Consumers that genuinely want the old behavior configure it explicitly
(an argument on `reflexive_consolidator`, not an inferred default). Configuration is a
recorded choice; a fallback is not.

**Migration is forward-only and lossless.** Records already at persona or AGENT scope stay
where they are and stay visible. Only new writes change destination. No backfill. The
observable change is that a returning user's *provisional* facts are no longer visible in a
later session until promoted — which is the property being bought.

### B.2 A durable episode ownership index

A new operational namespace `episodes/index`, written by the seam at run start, following
the `maintenance/state` pattern exactly (operational, outside the scope ladder, never
recallable):

```
run_id → { session_id, user_id, created_at }
```

Four fields, no more. A "participant/tenant boundary" field was proposed and is **rejected
for v0**: persona and session *are* vsigma's boundaries, and introducing a third ownership
axis inside a safety fix is how safety fixes get delayed. If a real multi-tenant model is
needed later it can be added behind this same index.

This makes source selection deterministic and enforceable *before* any trajectory reaches a
consolidator.

### B.3 Source selection is mandatory

`unconsolidated_episodes` gains a source filter, and the CLI requires the operator to state
a source boundary:

- **At least one `--source session:<id>` / `--source persona:<id>` is required** (repeatable).
- **`--all-sources` is an explicit flag** for intentionally store-wide maintenance.
- Omitting both is an **error**, not an inferred single-tenant default.

Revision 1 said an omitted source "reads everything — acceptable for the single-tenant
default." That was wrong: it preserves the exact behavior §A.5 identifies as a leak. Absence
of a stated boundary is not evidence that no boundary exists.

An episode with no entry in the `episodes/index` (written before this slice landed) is
matched only by `--all-sources`, never by a specific `--source`. Unknown ownership fails
closed.

### B.4 Bundles record their sources

`run_maintenance` passes `sources=` to `write_bundle` (`bundle.py:32-38` already accepts it
and hashes each source into the manifest; `maintenance.py:99-101` simply omits it today).
One-line fix, and it makes every maintenance bundle auditable for what it read.

## C. Slice 2 — promotion proper

Phase-sized. Adds the missing operation from §A.1.

### C.1 The two axes, and the rule connecting them

| | **Trust promotion** | **Scope promotion** |
|---|---|---|
| Moves | provisional → curated | narrow scope → wider scope |
| Carrier | `Provenance.confidence` (+ `consolidator`) | the record's namespace |
| Trigger | a second, more capable distillation pass | an explicit operator act |
| Reversible by | supersession (higher confidence wins) | retraction (deferred, §D) |

**Scope promotion never implies trust promotion.** Widening where a fact is visible is not
evidence that it is more true. A promoted record carries the *minimum* confidence of its
contributing sources forward, unless the policy's review step justifies otherwise (§C.3).

### C.2 An explicit operation with two scope parameters

Never one ambiguous `scope`. Source and target are separate and both required:

```python
PromotionRequest(
    source_scopes=[session("wafer-simulation")],
    target_scope=persona("jian"),
    policy="reviewed_generalization",
)
```

### C.3 The policy decides the transformation

Generalization is a policy, not an invariant — mandatory LLM generalization would add a
failure mode (over-generalization, invented generalities) to every promotion, including the
many that need none.

- `verbatim` — copy unchanged. Correct for a fact already general ("factory 35's upload
  endpoint is X").
- `generalize` — an LLM pass derives a reusable statement from the specifics.
- `reviewed_generalization` — `generalize` plus a human review point (the `--dry-run`
  pattern maintenance-pass already establishes); the only policy permitted to raise
  confidence, per §C.1.
- `redact` — copy with named fields stripped.

Worked example of `reviewed_generalization` — note the session record **survives**:

> **session:** During wafer job X, factory 35 required dismissing the pending popup.
> **persona:** For CMP uploads, dismiss pending notifications before selecting a factory.

### C.4 The gate gains a policy seam — an extension, not a parallel mechanism

Reusing the bundle gate is right, but `validate_bundle` (`bundle.py:57`) is **structural
only**: schema version, constructor present, record JSON shape. Secret, PII, ownership and
confidence checks do not exist. Revision 1 said "no new checking mechanism," which hid that
the mechanism being reused has to be built.

So: a **pluggable gate-policy interface** inside the existing gate. Structural validation
stays exactly as it is and runs first; policies run after, on the parsed records, and can
only reject. Two invariants:

- **The constructor cannot select, configure, or bypass the applicable policies.** Policies
  are the operator's, resolved at the gate, never read from the bundle.
- A failed check admits nothing and leaves the target scope untouched.

This preserves "the agent proposes, the engine commits" and the standing rule that an author
never operates its own gate.

### C.5 Lineage and idempotence — stated honestly

Revision 1 claimed both were "free" from existing machinery. Neither is:

- **Lineage is not free.** `Provenance.source_ref` is a single string (`records.py:29`).
  Multi-source lineage requires structured source refs — a **bundle schema v0.2** change.
  Coordination note: maintenance-pass §D already reserves v0.2 for tombstones/retraction;
  these either land together or lineage takes v0.2 and retraction follows at v0.3.
- **Content-addressing does not give idempotence for generative policies.** `record_id` is
  `sha256(content)` (`construction.py:44`), so re-running a `generalize` promotion that
  words its output differently produces a *different id* and a duplicate record.

**The fix is an operation-level marker, not a new id scheme.** Promotion writes a marker
keyed by

```
hash(sorted(source_record_ids), target_scope, policy, policy_version)
```

into the operational `maintenance/state` namespace, exactly as the deep round marks
episodes. A repeated promotion is then recognized and skipped **before the LLM is called** —
idempotence at the operation grain, no wasted generation, and `record_id` stays purely
content-addressed with no exception carved out for one record class.

### C.6 Recall dedup depends on the transformation

Suppressing the wider record whenever its lineage names a visible narrower one is correct
for `verbatim` and wrong for `generalize`: a generalized lesson may carry a conclusion
present in none of its sources, and suppressing it would discard exactly what the promotion
produced. So dedup is per-policy:

- `verbatim` — suppress the wider duplicate when a source is visible.
- `redact` — policy-dependent; declared by the policy.
- `generalize` / `reviewed_generalization` — **retain both**; let ranking and the token
  budget decide (that is the siblings' job, §B.5 below).

All of this is a deterministic check over recorded lineage. Semantic dedup of *unrelated*
overlapping facts stays deferred (§D).

### C.7 Sequencing against the siblings

Slice 1 moves provisional knowledge to session scope, where — inside that same session — it
is largely redundant with the replayed raw dialog. It earns its keep across sessions once
promoted, and inside a long session once
[session-dialog-compaction](./session-dialog-compaction.md) truncates the replay. §C.6's
"retain both" makes the token-budget sibling load-bearing rather than optional. **None of
this blocks slice 1** — an unsafe default is not worth holding for an efficiency concern.

## D. Non-goals / hard lines

- **Automatic scope promotion — never.** Not as a side effect of a shared `user_id`, of a
  confidence threshold, or of a run ending. Widening is always an explicit act.
- **Retraction / supersession lineage propagation.** Deferred to bundle schema v0.2/v0.3 per
  maintenance-pass §D. Promotion being additive (§C.2, §C.3) is what makes deferring it
  survivable.
- **Per-record scope routing by the constructor.** Already deferred in maintenance-pass §D;
  `distill` keeps one scope per pass.
- **Cross-persona merge, and promotion to `GLOBAL`.** Not in v0; both need retraction and a
  sharper ownership model first.
- **Semantic deduplication.** §C.6 dedups by recorded lineage only.
- **A tenant axis.** Rejected for v0 (§B.2); persona and session are the boundaries.
- **The agent operating its own gate.** Unchanged house invariant, reinforced by §C.4.

## E. Graduation criteria

**Slice 1:**

- A `done` run with both `session_id` and `user_id` writes its distilled record to **session**
  scope; persona is untouched (the §A.3 regression test).
- A `done` run with `user_id` and no `session_id` writes **no** durable knowledge record; the
  run completes normally (the §A.4 test).
- `run_maintenance` with `--source session:a --scope persona:jian` reads and marks **only**
  session a's episodes; an episode owned by session b is neither read nor marked (the §A.5
  multi-tenant test).
- Invoking the CLI with neither `--source` nor `--all-sources` **errors**; an episode with no
  index entry is matched only by `--all-sources`.
- Every maintenance bundle manifest lists the sources it read.

**Slice 2:**

- A `verbatim` promotion run twice produces one record and one marker, and the second pass
  makes **no LLM call**.
- A `generalize` promotion whose second run words its output differently is still recognized
  as the same operation and skipped.
- After any promotion the source record still exists at source scope, and the target
  record's provenance names every contributing source.
- `recall` inside the source session suppresses a `verbatim` promoted record and **retains** a
  `generalize` one.
- A promotion whose gate policy fails admits nothing; the bundle is rejected and the target
  scope is unchanged.
- A bundle that tries to declare its own gate policies is rejected.

When validated, this graduates into architecture_design §8/§9 as the scope-promotion
section, retiring the ✎ "promotion policies" outer-ring line and disambiguating *promotion*
throughout §9.

## F. Open questions for review

1. **CLI surface for slice 2.** A new verb (`vsigma memory promote --from session:a --to
   persona:jian --policy verbatim`) or a mode of `consolidate`? Leaning a new verb: the
   two-scope signature does not fit `consolidate`'s one-scope shape, and conflating the two
   is what §A.5 already cost us.
2. **Is `PromotionRequest` public API**, or just the CLI's argument shape plus a `promote(...)`
   function? Leaning the latter for v0 — the smaller mechanism until a programmatic consumer
   asks.
3. **Should promotion touch confidence at all** (§C.1), or should trust stay strictly the deep
   round's business? The stricter rule is cleaner; it costs an extra pass to curate a
   reviewed generalization.
4. **Schema coordination:** does lineage take v0.2 alongside retraction, or v0.2 alone with
   retraction at v0.3? (§C.5)

## G. Decision log

- **G.1 — Automatic knowledge destination: session, else nothing durable.** `[SETTLED
  2026-09-05, by the maintainer]`, directed as "safe-by-default." Revision 1 settled only the
  session-present case and closed the sessionless case with an incorrect claim (that persona
  was the narrowest remaining option — episode is narrower). Revision 2 settles the
  sessionless case too, on the uniform rule in §B.1 and the in-codebase precedent in §A.3;
  the second reviewer independently favored the same outcome ("require a Session for durable
  automatic consolidation").
- **G.2 — Generalization is a policy, not an invariant.** `[SETTLED]` §C.3.
- **G.3 — Promotion commits through the existing gate, extended with a policy seam.**
  `[SETTLED]` §C.4. Revision 1's "no new mechanism" framing is withdrawn as misleading.
- **G.4 — Idempotence via operation marker, not a new id scheme.** `[SETTLED]` §C.5.
- **G.5 — Retraction stays deferred.** `[SETTLED]` §D, conditional on promotion being
  additive.
- **G.6 — Tenant axis rejected for v0.** `[SETTLED]` §B.2.
