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

## 2. One destination rule (§B.1) `[ ]`

- [ ] `_knowledge_scope` (`consolidation.py:41`) returns `Scope | None`: session if
      `session_id`, else `None`. Never persona, never the `AGENT` fallback
- [ ] `None` skips the distillation write; the run is otherwise unaffected (holds the
      existing "skippable, never breaks the run" contract of the reflexive hook)
- [ ] Do **not** copy the mechanical summary's `episode` fallback (`consolidation.py:98-100`)
      into the knowledge path — episode-scoped knowledge is a write-only sink (§B.1)
- [ ] Escape hatch: an explicit argument on `reflexive_consolidator`, not an inferred default
- [ ] Tests (§E): done-run with session+user → **session**, persona untouched; done-run with
      user and no session → **no** durable record, run completes normally

## 3. Mandatory source selection (§B.3) `[ ]`

- [ ] `unconsolidated_episodes` (`maintenance.py:58`) gains a source filter, reading the
      index from step 1
- [ ] CLI: repeatable `--source session:<id>|persona:<id>`; `--all-sources` explicit;
      **neither is an error** (`cli.py:43-66`). `--scope` keeps naming the *destination* —
      the two are now visibly different things
- [ ] Unindexed episodes (written before this phase) match **only** `--all-sources` — unknown
      ownership fails closed
- [ ] Tests (§E): `--source session:a --scope persona:jian` reads and marks only session a's
      episodes, session b's neither read nor marked; bare invocation errors

## 4. Auditable bundles (§B.4) `[ ]`

- [ ] `maintenance.py:99-101` passes `sources=` to `write_bundle` — already accepted and
      hashed into the manifest (`bundle.py:32-38`), just omitted today
- [ ] Test: every maintenance bundle manifest lists the sources it read

## 5. Close out `[ ]`

- [ ] `make check` green; test count updated in `checkpoint.md`
- [ ] Docs: `architecture_design.md` §9's round-1/round-2 table has **no Destination row** —
      the scope a record lands in is nowhere stated, which is how the widening survived
      unexamined. Add it. `getting_started` if the CLI change is user-visible
- [ ] Mark the proposal's Slice 1 VALIDATED; §C (Slice 2) stays a candidate
- [ ] Tag `phase-4.7`; collapse this board into a `plan.md` summary
