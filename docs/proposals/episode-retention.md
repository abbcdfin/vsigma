# Proposal: Episode Retention — Pruning Trajectory Without Losing Episodes

**Status:** DRAFT 2026-09-06, **revision 2** (first review round folded in).
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

### B.1 The key is (owner, `task_id`); the unit is the episode

`run_id` remains the only storage key — trajectory namespace, checkpoint thread, maintenance
cursor entry, and `source_ref` all keep pointing at exactly one episode, and nothing
collides. Retention groups episodes and deletes whole ones:

    within one owner, keep the N most recent episodes per task_id; prune the rest

`task_id` is already the correlation identity — caller-supplied, defaulting to the `run_id`
(`run.py:60`). A recurring job names itself (`task_id="nightly-report"`) and gets a bounded
window of history without any two of its runs ever sharing storage.

**The group key is (owner, `task_id`), never `task_id` alone.** `task_id` is caller-supplied
and therefore *not* globally unique: two tenants can both run `"nightly-report"`. Grouped by
name alone, one tenant's runs consume the other's keep-last allowance and delete the other's
trajectory. Revision 1 got this wrong twice — it grouped by bare `task_id`, and §F.3 argued
the source-boundary rule "does not apply because pruning deletes rather than reads". That is
backwards: deleting across a tenant boundary is strictly worse than reading across one. A
pruning pass therefore takes the **same mandatory source selection** as the maintenance pass
([memory-scope-promotion](./memory-scope-promotion.md) §B.3) — at least one `--source`, or an
explicit `--all-sources` — and groups within the owner recorded in the index.

**The correlation key must be persisted, and now is.** Revision 1 assumed `task_id` was
available at prune time. It was not: it reaches the observer on every `TrajectoryEvent`, but
a `TrajectoryLog` is in-process, so after a restart the grouping key was simply gone. Phase
4.7's `episodes/index` gained a `task_id` field for this (`episodes.py`) — added while the
index was still new, since retro-fitting it would have meant migrating a namespace that
starts accumulating the moment 4.7 ships. It is recorded as *correlation*, distinct from the
`session_id`/`user_id` ownership axes, so §B.2's rejection of a third ownership axis is
untouched.

Grouping by (owner, `task_id`) beats a global TTL because it is workload-shaped: a nightly
job keeps its recent runs, a one-off keeps its single episode, and neither needs a tuned age
threshold. Episodes whose `task_id` defaulted to their `run_id` form singleton groups and are
therefore never pruned by this rule — a global age bound is a possible second policy, not
this one (§F.4).

### B.2 What is deleted, and what survives

| | Fate | Why |
|---|---|---|
| `episode:<run_id>/context` | **deleted** | the payload; the point of the exercise |
| `maintenance/state` `deep:<run_id>` | **deleted** | dead weight once the namespace is gone — `unconsolidated_episodes` iterates existing namespaces, so a pruned episode drops out of its input set regardless |
| `episodes/index` `<run_id>` | **kept**, stamped with disposition **and content hash** | the tombstone: `source_ref="trajectory:<run_id>"` on already-ingested records must stay both attributable *and* checkable (§B.4) |
| knowledge records | **kept** | prune the source, never the conclusions |
| LangGraph checkpoints | **not reachable** — see §D | a different store, behind the injected checkpointer |

### B.3 What "consolidated" must mean, and the default/opt-in split

```python
prune_episodes(store, keep_last=3)                        # consolidated episodes only
prune_episodes(store, keep_last=3, unconsolidated=True)   # explicit opt-in
```

**First, a `deep:` marker does not mean the episode was processed.** `episode_trajectory`
truncates to `steps[-REFLEXIVE_WINDOW:]` — the last 20 (`consolidation.py:62`) — and
`run_maintenance` calls that same capped function (`maintenance.py:93`). So for any episode
longer than 20 steps the deep constructor never saw the beginning, and the marker records
only that a pass *ran*. Pruning on that marker deletes early steps no constructor ever read,
silently, on the **default** path — the one that is supposed to be safe.

This is a defect in the shipped deep round, not only in this proposal: a constant named for
the reflexive round's per-run budget is silently bounding the offline batch pass, which has
no such budget. Retention is what makes it *unrecoverable*, because today the untouched steps
at least remain on disk. Two fixes are possible — give the deep pass its own uncapped source
function, or record the processed step range in the marker — and this proposal **depends on
one of them landing first**. Absent that, "consolidated" cannot be defined precisely enough
to prune on.

Second, pruning an episode the deep round has not yet processed destroys learning that was
never extracted. Refusing outright is *also* wrong: maintenance is operator-triggered (cron owns
time, by the maintenance-without-the-agent decision), so a nightly job under weekly
maintenance would find almost nothing prunable and the disk fills anyway. A safety rule that
disables the feature is not a safety rule.

So it is a choice, with the safe default — the same posture already settled in
[memory-scope-promotion](./memory-scope-promotion.md) §B.1: *escape hatch is explicit
configuration, never an inferred fallback*. The consumer who sets `keep_last=3` intends
bounded storage; whether that also costs them learning depends on the cadence of a cron job
they may not control, so the two must not be silently coupled.

### B.4 The tombstone is mandatory, and the loss is counted

Every pruned episode's surviving index entry records what happened:

```
run_id -> {task_id, session_id, user_id, created_at,
           disposition, pruned_at, content_sha256}
```

`disposition` is `pruned` or `pruned_unconsolidated`, and a pass reports both counts, so
"12 episodes pruned, 2 before consolidation" is alertable rather than discovered months later
as a week with no knowledge in it.

**`content_sha256` is required, not optional.** Revision 1 left it to §F. That contradicted
the invariant recorded in [memory-construction](./memory-construction.md) days earlier — that
a pruned episode's tombstone must carry *both* a content hash and a disposition — and the
contradiction mattered: with attribution alone, every `trajectory:<run_id>` lineage on an
already-ingested record becomes permanently unverifiable the moment the namespace is deleted.
Attribution says *who and when*; only the hash lets a later audit *check* that a record came
from what it claims. Hashing at prune time is one pass over bytes already in hand, and it is
the last moment it can ever be done.

### B.5 Only durably terminal episodes are prunable

An escalated episode is *parked*, not finished: its `Run` still exists, its checkpoint still
exists (§D leaves checkpoints alone), and `resume()` re-invokes the graph. Nothing consults
the index on that path, so `wrap` would write fresh entries into the namespace pruning had
just deleted — recreating a partial trajectory with no `deep:` marker, which
`unconsolidated_episodes` then hands to the next maintenance pass as though it were a new
episode. The pruned-and-gone invariant would be violated by an ordinary resume.

So pruning is restricted to episodes that reached a **durably terminal** state — `done`, per
the status the run-end hook already derives. Escalated episodes are never prunable, whatever
`keep_last` says, because a human may still answer them. If parked episodes ever need
reclaiming, the mechanism is an explicit abandon that closes the episode first, not a prune
that races a resume.

The narrower alternative — make writes to a pruned `run_id` fail — is worse: it turns a
routine `resume()` into an error at an arbitrary point mid-run, and the caller did nothing
wrong.

### B.6 Shape: a CLI verb

`vsigma memory prune --db … --keep-last N --source <scope>|--all-sources [--task <id>]
[--unconsolidated] [--dry-run]`,
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

- Two episodes under one `task_id` and one owner, `keep_last=1`, both fully consolidated →
  the older episode's trajectory namespace and `deep:` marker are gone, its index entry
  remains with `disposition="pruned"` and a `content_sha256`, and every knowledge record
  derived from it is untouched.
- **Two tenants both using `task_id="nightly-report"` keep their own `keep_last` allowance;**
  neither pass deletes the other's trajectory. Invoking with neither `--source` nor
  `--all-sources` errors, as it does for `consolidate`.
- An episode of more than `REFLEXIVE_WINDOW` steps is **not** prunable by default on the
  strength of a `deep:` marker alone — only once the full trajectory is demonstrably
  processed (§B.3's prerequisite).
- An unconsolidated episode is **not** pruned by default, and **is** pruned under
  `unconsolidated=True`, stamped `pruned_unconsolidated`.
- **An escalated episode is never pruned**, whatever `keep_last` says; a `resume()` after a
  pruning pass cannot recreate a deleted namespace.
- After pruning, `unconsolidated_episodes` neither returns nor re-processes the pruned
  episode.
- Every tombstone carries a `content_sha256` of what was deleted; no pruning path writes a
  tombstone without one.
- Episodes whose `task_id` defaulted to their `run_id` are never pruned by `keep_last`.
- `--dry-run` deletes nothing and reports both counts.
- A pruned episode's `run_id` is still rejected by `record_episode` — pruning reclaims bytes,
  never names (the invariant recorded in memory-construction.md).

## F. Open questions

1. **How should §B.3's prerequisite be fixed** — give the deep pass its own uncapped source
   function, or record the processed step range in the `deep:` marker? The first is simpler
   and makes "consolidated" mean what it says; the second is truthful about partial passes
   and costs no change to batch size. Either unblocks this proposal; neither belongs to it.
2. **Does `keep_last` belong on the CLI only, or also as a policy on `Services`** so a daemon
   can prune without an operator? The maintenance-without-the-agent decision says CLI; a
   long-running daemon with no operator is the case that would argue otherwise.
3. **A second policy: a global age bound**, for episodes that form singleton `task_id` groups
   and are therefore invisible to `keep_last` — which is *most ad-hoc runs*, since `task_id`
   defaults to `run_id`. Needed for v0, or a follow-up?
4. **Checkpoint eviction** (§D) is out of scope but may be the larger pile. Does it want its
   own proposal, or a section here once the `MemoryStore` delete seam exists?

## G. Decision log

- **G.1 — Reuse of a `run_id` to bound growth: REJECTED** (2026-09-06). Measured: it bounds
  the namespace by overwriting, destroying trajectory the deep round never read; it inherits
  the episode's `deep:` marker so the slot is permanently marked consolidated; and it leaves
  LangGraph's checkpoint history growing regardless. Phase 4.7 rejects recycled ids.
- **G.2 — `run_id` derived from `user_id` + a caller-supplied `run_name`: REJECTED**
  (2026-09-06). It does close cross-owner collision, but it does not close cross-*session*
  collision, it puts persona identifiers into `source_ref` — a required field of the
  published bundle schema, so they would travel in portable bundles — and it creates a second
  source of ownership truth alongside the index. `task_id` already provides a stable
  caller-supplied name, and is correct precisely because it does not key storage.
- **G.3 — Never pruning unconsolidated episodes: WITHDRAWN** (2026-09-06). Stated as a hard
  constraint in discussion, then withdrawn: maintenance is operator-triggered, so a nightly
  job under weekly maintenance would find nothing prunable and the disk fills anyway. A
  safety rule that disables the feature is not a safety rule. Replaced by the safe-default +
  explicit-flag posture of §B.3.
- **G.4 — Revision 1 errors, withdrawn rather than quietly fixed** (2026-09-06, first review
  round): grouping by bare `task_id` (cross-tenant deletion, §B.1); assuming `task_id` was
  persisted when it was not (§B.1); §F.3's claim that the source-boundary rule "does not
  apply because pruning deletes rather than reads" — backwards; treating a `deep:` marker as
  proof of processing when the source function is capped at 20 steps (§B.3); leaving the
  content hash optional against an invariant this project had already recorded (§B.4); and
  omitting the resume-after-prune path entirely (§B.5).
