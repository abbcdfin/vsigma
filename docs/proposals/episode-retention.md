# Proposal: Episode Retention — Pruning Trajectory Without Losing Episodes

**Status:** DRAFT 2026-09-06 — awaiting review.
**Prompted by:** the question "will run_ids accumulate and eventually run out?", asked
during Phase 4.7. They will not — `uuid4` — but the storage behind them accumulates without
bound, and nothing in `src/` deletes anything. The follow-up question, "can a caller reuse a
`run_id` to cap trajectory growth?", is the one this proposal answers: no, but the intent
behind it is legitimate and deserves a real mechanism.
**Closes:** the open question recorded at
[memory-construction](./memory-construction.md) — *"Trajectory retention policy: how long raw
episodes are kept after the deep pass has processed them"* — and honours the two invariants
added there on 2026-09-06.
**Sibling, not dependency:** [session-dialog-compaction](./session-dialog-compaction.md)
bounds what a `Session` stores and replays; this bounds what the *store* keeps per episode.

## A. Problem

**Nothing is ever deleted.** `MemoryStore` is `write` / `read` / `query` (`memory.py:14-17`).
There is no delete, no eviction, no TTL anywhere in `src/`. `architecture_design.md:246`
states the position deliberately — *"Raw trajectories are the source of truth (the seam's
episodic capture is the retention)"* — but "retention" there means "we keep it", not "we have
a policy". For a long-running daemon that is unbounded growth.

**What grows is the trajectory, not the bookkeeping.** Per episode the store holds one
`episodes/index` row (~106 B) and, after the deep pass, one `deep:<run_id>` marker — both
flat. The trajectory is the pile: `wrap` persists each node update including the cumulative
`messages` (`seam.py:66`), so a single episode's stored bytes grow *superlinearly in its own
step count*. A long tool loop is expensive before any question of how many episodes are kept.

**Reuse is not the answer, though it looks like one.** Re-running under an existing `run_id`
does bound the namespace — measured, three runs through one id leave one key — but by
*overwriting*: the earlier runs' trajectory is destroyed before the deep round can read it.
It also inherits that episode's `deep:` marker, so the slot is permanently marked
consolidated and every later run through it is silently skipped by maintenance. And it does
not bound the thing that actually accumulates alongside: LangGraph's checkpoint history grew
to 6 entries across 2 invokes on one thread. Phase 4.7 accordingly rejects any recycled
`run_id` (`episodes.py`); this proposal supplies what that rejection leaves unserved.

## B. Design

### B.1 The key is `task_id`, the unit is the episode

`run_id` remains the only storage key — trajectory namespace, checkpoint thread, maintenance
cursor entry, and `source_ref` all keep pointing at exactly one episode, and nothing
collides. Retention groups episodes *by* `task_id`, read from the `episodes/index` entry
(Phase 4.7 §B.2), and deletes whole episodes:

    keep the N most recent episodes per task_id; prune the rest

`task_id` is already the correlation identity — caller-supplied, defaulting to the `run_id`
(`run.py:60`), carried on every `TrajectoryEvent`. A recurring job names itself
(`task_id="nightly-report"`) and gets a bounded window of history without any two of its runs
ever sharing storage.

Grouping by `task_id` beats a global TTL because it is workload-shaped: a nightly job keeps
its recent runs, a one-off keeps its single episode, and neither needs a tuned age threshold.
Episodes whose `task_id` defaulted to their `run_id` form singleton groups and are therefore
never pruned by this rule — a global age bound is a possible second policy, not this one.

### B.2 What is deleted, and what survives

| | Fate | Why |
|---|---|---|
| `episode:<run_id>/context` | **deleted** | the payload; the point of the exercise |
| `maintenance/state` `deep:<run_id>` | **deleted** | dead weight once the namespace is gone — `unconsolidated_episodes` iterates existing namespaces, so a pruned episode drops out of its input set regardless |
| `episodes/index` `<run_id>` | **kept**, stamped with a disposition | the tombstone: `source_ref="trajectory:<run_id>"` on already-ingested records must still resolve to *who and when* |
| knowledge records | **kept** | prune the source, never the conclusions |
| LangGraph checkpoints | **not reachable** — see §D | a different store, behind the injected checkpointer |

### B.3 Consolidated by default; unconsolidated by explicit opt-in

```python
prune_episodes(store, keep_last=3)                        # consolidated episodes only
prune_episodes(store, keep_last=3, unconsolidated=True)   # explicit opt-in
```

Pruning an episode the deep round has not yet processed destroys learning that was never
extracted. Refusing outright is *also* wrong: maintenance is operator-triggered (cron owns
time, by the maintenance-without-the-agent decision), so a nightly job under weekly
maintenance would find almost nothing prunable and the disk fills anyway. A safety rule that
disables the feature is not a safety rule.

So it is a choice, with the safe default — the same posture already settled in
[memory-scope-promotion](./memory-scope-promotion.md) §B.1: *escape hatch is explicit
configuration, never an inferred fallback*. The consumer who sets `keep_last=3` intends
bounded storage; whether that also costs them learning depends on the cadence of a cron job
they may not control, so the two must not be silently coupled.

### B.4 The loss is counted, not silent

Every pruned episode's surviving index entry records what happened:

```
run_id -> {session_id, user_id, created_at, disposition, pruned_at}
```

with `disposition` one of `pruned` / `pruned_unconsolidated`. A pass reports both counts, so
"12 episodes pruned, 2 before consolidation" is visible and alertable rather than discovered
months later as a week with no knowledge in it. This is also the disposition half of the
tombstone the Phase 4.7 review asked for; the content-hash half is §F.

### B.5 Shape: a CLI verb

`vsigma memory prune --db … --keep-last N [--task <id>] [--unconsolidated] [--dry-run]`,
beside `consolidate` and `export`. Cron keeps owning time; nothing runs inside the agent.
`--dry-run` reports what would go, matching `consolidate`.

## C. Prerequisite: `MemoryStore` cannot delete

`write` / `read` / `query`, and nothing else. Three options:

1. **Add `delete(namespace, key)` and `drop(namespace)` to the protocol.** Honest, but every
   backend must implement them, and the protocol is deliberately minimal.
2. **A `SQLiteStore` backend extra**, exactly as `namespaces()` already is — `run_maintenance`
   already takes a concrete `SQLiteStore` for this reason. Cheapest, and consistent.
3. **A separate `PrunableStore` protocol** that backends may also satisfy.

Recommendation: **(2) for v0**, promoting to (1) if a second backend needs it. It keeps the
minimal protocol minimal and matches the precedent that already exists for maintenance.

## D. Non-goals

- **Checkpoint eviction.** LangGraph checkpoints live behind the injected checkpointer, not
  in `MemoryStore`, and for a long-running daemon may be the larger pile. Out of scope here
  and worth its own investigation — an episode pruned by this proposal still leaves its
  checkpoint history behind.
- **The superlinear within-run growth** (`seam.py:66` persisting cumulative `messages`). A
  separate, bug-shaped fix: the seam persists a payload already retained elsewhere. Worth
  doing first if a consumer is feeling pressure now, since it needs no policy decisions.
- **Tombstones/retraction records for knowledge** (maintenance-pass §D, bundle schema v0.2).
  This proposal never deletes a knowledge record.
- **Summarize-on-evict.** Distilling an episode *because* it is about to be pruned is the
  deep round's job; invoking it from a retention pass couples two cadences.

## E. Graduation criteria

- Two episodes under one `task_id`, `keep_last=1`, both consolidated → the older episode's
  trajectory namespace and `deep:` marker are gone, its index entry remains with
  `disposition="pruned"`, and every knowledge record derived from it is untouched.
- An unconsolidated episode is **not** pruned by default, and **is** pruned under
  `unconsolidated=True`, stamped `pruned_unconsolidated`.
- After pruning, `unconsolidated_episodes` neither returns nor re-processes the pruned
  episode.
- Episodes whose `task_id` defaulted to their `run_id` are never pruned by `keep_last`.
- `--dry-run` deletes nothing and reports both counts.
- A pruned episode's `run_id` is still rejected by `record_episode` — pruning reclaims bytes,
  never names (the invariant recorded in memory-construction.md).

## F. Open questions

1. **Content hash in the tombstone.** The Phase 4.7 review asked for one so a pruned
   `trajectory:<run_id>` stays *auditable* rather than merely attributable. Hashing at prune
   time is cheap and truthful. Worth it, or is attribution enough for v0?
2. **Does `keep_last` belong on the CLI only, or also as a policy on `Services`** so a daemon
   can prune without an operator? The maintenance-without-the-agent decision says CLI; a
   long-running daemon with no operator is the case that would argue otherwise.
3. **Interaction with `--source` (Phase 4.7 §B.3).** Should pruning take the same mandatory
   source-boundary treatment? It deletes rather than reads, so the leak argument does not
   apply — but deleting another tenant's episodes is its own hazard.
4. **Second policy: a global age bound**, for episodes that form singleton `task_id` groups
   and are therefore invisible to `keep_last`. Needed for v0, or a follow-up?
