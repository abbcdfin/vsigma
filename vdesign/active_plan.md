# Active Plan — Phase 4.7: Memory scope safety

> JIT breakdown for the phase under construction. Durable roadmap:
> [`plan.md`](./plan.md); volatile session handoff: [`checkpoint.md`](./checkpoint.md).
> Design: [memory-scope-promotion](../docs/proposals/memory-scope-promotion.md) §B (Slice 1);
> graduation criteria: §E. Opened 2026-09-06.

**Contract for the whole phase:** automatic construction writes to the **narrowest durable
scope available, and where none exists it writes nothing durable**. No schema change, no new
concepts, forward-only (existing records stay where they are).

**Order matters.** B.2 lands first — B.3 cannot enforce a source boundary before ownership is
recorded. B.1 and B.4 are independent and can go in any order.

---

## 1. Episode ownership index (§B.2) `[x]`

- [x] `episodes.py` — `EPISODES_NS = "episodes/index"`, `EpisodeOwner`, `record_episode`
      (first write wins), `episode_owner`, `episode_owners`. Operational, modelled on
      `MAINTENANCE_NS`; a test asserts `visible_scopes` never yields it
- [x] `run_id → {session_id, user_id, created_at}`; tenant axis rejected for v0 (§B.2)
- [x] **Written by `Agent.run`, not by `wrap`.** §B.2 said "the seam", but the seam is
      per-*node* — it has no run-start grain, and writing there would cost one store write
      per node with a drifting `created_at`. `Agent.run` is the actual run boundary: it mints
      the `run_id` and holds both ids. Written *before* `graph.invoke`, so a run that crashes
      part-way is still attributable. `resume` does not rewrite (same run, same owner), and
      `Session.send` inherits this by routing through `Agent.run`
- [x] Consequence, already specified: a graph invoked directly without `Agent` gets trajectory
      but no entry. That is the **unindexed** case §B.3 handles — ownership unknown, matched
      only by `--all-sources`. Distinct from an indexed entry with null ids, which means
      ownership is known to be absent
- [x] **A `run_id` is never recycled** — registering a used id raises, whatever the owner.
      Two review rounds got this to the right rule. Round 1: `record_episode` returned the
      stored owner but `Agent.run` ignored it, so the seam wrote tenant B's trajectory into
      tenant A's episode and the index attributed it to A — a *false* attribution, worse than
      the unknown one §B.3 fails closed on. Round 2: same-owner reuse is equally unsafe and
      for reasons independent of tenancy — the id is also the LangGraph thread, the
      maintenance cursor entry (a surviving `deep:` marker means the episode is born marked
      consolidated and is silently never distilled), and `source_ref`. `run_id` is permanent
      episode identity, `task_id` correlates related episodes, `resume()` continues one; all
      three are now documented on `Agent.run`, which is public API even though no caller in
      this tree passes `run_id=`
- [x] 8 tests (`test_episodes.py`); `make check` green, **135 tests** (was 127)
- Recorded, not fixed: `record_episode`'s read-then-write is **not atomic** (`SQLiteStore`
  locks each op separately), so two concurrent registrations of the same *caller-supplied* id
  can both see it free. Negligible for generated uuid4 ids. The real fix is a create-if-absent
  store operation — a widening of the deliberately-minimal `MemoryStore` protocol — so it is
  a `TODO(concurrency)` in `episodes.py`, not Phase 4.7 work (§B.2)
- Retention invariants recorded in `memory-construction.md`'s open-questions list: payload may
  be pruned, identifiers never recycled; and a pruned episode needs a tombstone carrying a
  content hash and disposition — the ownership index gives attribution, **not** auditability
- Deferred to step 3: nothing from `episodes.py` is exported in `__init__.py` yet — the
  public surface waits until the CLI actually needs it

## 2. One destination rule (§B.1) `[x]`

- [x] `_knowledge_scope` returns `Scope | None`: session if `session_id`, else `None`. Never
      persona, never the `AGENT` fallback, and no `episode` fallback either — the mechanical
      summary's is a dead letter and was not copied into the knowledge path
- [x] `None` skips the distillation write; the run is otherwise unaffected, holding the hook's
      existing "skippable, never breaks the run" contract
- [x] Escape hatch: `reflexive_consolidator(knowledge_scope=)`, a `Callable[[State], Scope |
      None]`. Explicit configuration, never an inferred fallback; the docstring shows the
      pre-4.7 widening rule as a copy-pasteable opt-in and names what it opts into
- [x] Tests (§E): session+user → **session**, persona untouched; user and no session → **no**
      durable knowledge at AGENT/persona/episode, run completes normally, mechanical summary
      still written; the escape hatch restores widening
- [x] `architecture_design.md` §9 gained the **Destination row** it never had — the scope a
      record lands in was nowhere stated, which is how the widening survived four phases
- [x] Four existing tests encoded the old behaviour and were updated, not deleted
- [x] **New consequence, recorded in the proposal §B.1:** ids are content-addressed but
      namespaces are not, so a deep pass run with `--scope persona:alice` now *adds* a record
      instead of superseding round 1's session-scoped one. The §A.7 double-count by a new
      route; Slice 1 does not fix it (aim the deep pass at the same scope), §C's per-policy
      dedup does. **145 tests.**
- [x] **Doc sweep (review round, 2026-09-07)** — I updated the architecture table but never
      swept for claims the change invalidates. Three found: (a) `getting_started`'s flagship
      "agent that learns from experience" walkthrough passed only `user_id`, so after this
      change it wrote nothing and the advertised second-run recall silently never happened —
      the headline example had become a no-op; (b) the supersession claim in
      `architecture_design` §9 and twice in `getting_started` was unconditional, and is now
      scope-conditional; (c) `getting_started`'s deep-pass example used
      `scope=persona("alice")`, which after this change demonstrates the *duplicate* case
      while claiming supersession. All corrected, with the sessionless case explained rather
      than papered over.
- Found while sweeping, **deferred to step 3**: `cli.py:50` defaults `--scope` to `agent`, so
  a bare `vsigma memory consolidate --db …` writes to the widest scope in the system and
  duplicates rather than supersedes. Same argument as §B.3's — a default is not a decision —
  so the destination should become mandatory alongside the source boundary.

## 3. Mandatory source selection (§B.3) `[x]`

- [x] `unconsolidated_episodes(store, *, sources)` filters on the step-1 index. `sources` is
      **required, not defaulted** — `None` means store-wide, but the caller has to say so;
      that is the library-level equivalent of `--all-sources`. Same for `run_maintenance`
- [x] `parse_source` accepts only `persona:`/`session:` — the two ownership rungs. `agent`
      and `global` are destinations, not owners, and are rejected with that wording
- [x] CLI: repeatable `--source`, explicit `--all-sources`, **neither *or both* is an error**
      (specifying both is contradictory, so it is rejected too). `--scope` is now
      `required=True` — the deferred finding from step 2: it defaulted to `agent`, which
      `visible_scopes` returns for every persona, so the widest destination in the system was
      what you got for not typing anything
- [x] Unindexed episodes match **only** `--all-sources`. Unknown ownership fails closed
- [x] Tests (§E): `sources=[session("a")] --scope persona:jian` reads and marks only session
      a's episodes; session b neither read nor marked; a persona source matches its owner; an
      unindexed episode is invisible to every specific source; the CLI errors on neither and
      on both; `parse_source` rejects non-ownership scopes. **150 tests**
- [x] **Doc sweep, done this time rather than in review:** `architecture_design` §9 gained
      the two-boundaries paragraph; `getting_started`'s CLI block now shows `--source` and
      `--scope` together with a comment on which is which; `maintenance-pass.md` carries a
      superseded-in-part banner, since its examples predate the boundary
- mypy caught the one real hazard: the new `sources` parameter shadowed a local of the same
  name holding trajectories. Renamed to `trajectories`, with a comment on the two senses

## 4. Auditable bundles (§B.4) `[x]`

- [x] `run_maintenance` passes `sources=trajectories` to `write_bundle`, which already
      accepted the argument and hashed each entry into the manifest — it was simply omitted
- [x] Test: the manifest lists `trajectory:<run_id>` for exactly the episodes the source
      boundary allowed, each with a sha256. Without this, a bundle could not be checked
      against the boundary that produced it — step 3 constrains the pass, step 4 makes the
      constraint *evidenced*. **151 tests**

## 5. Close out `[ ]`

- [ ] `make check` green; test count updated in `checkpoint.md`
- [ ] Docs: `architecture_design.md` §9's round-1/round-2 table has **no Destination row** —
      the scope a record lands in is nowhere stated, which is how the widening survived
      unexamined. Add it. `getting_started` if the CLI change is user-visible
- [ ] Mark the proposal's Slice 1 VALIDATED; §C (Slice 2) stays a candidate
- [ ] Tag `phase-4.7`; collapse this board into a `plan.md` summary
