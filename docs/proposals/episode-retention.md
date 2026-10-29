# Proposal: Episode Retention — Pruning Trajectory Without Losing Episodes

**Status:** DRAFT 2026-09-06; **revision 5** 2026-10-27. Revision 4 moved retention into the memory
pass with a declarative policy, as a component of [scope-lifespans](./scope-lifespans.md).
Revision 5 answers an audit (2026-10-27) and its follow-up (2026-10-29): the memory pass
consolidates only ended episodes, its marker records the transcript's row count, one test of
"consolidated" governs selection, pruning and prior context, `failed` episodes are final, each
lifecycle record has one writer, and the tombstone is written before anything is deleted.
Revision 3, rebased on Phases 4.8–4.10, is commit `16208b5`. What changed is in §G.5–§G.7;
revision 2's open questions are answered in §F.
**Component of:** [scope-lifespans](./scope-lifespans.md), which holds the model: this proposal
enforces the episode rung's lifespan for transcripts.
**Prompted by:** "will `run_id`s run out?" (Phase 4.7). They will not (`uuid4`), but the storage
behind them grows without bound.
**Closes:** [memory-construction](./memory-construction.md)'s open question, "how long raw
episodes are kept after the deep pass has processed them", honouring its two invariants: a
pruned episode's tombstone carries a content hash and a disposition, and a pruned `run_id` is
never reused.

## A. Problem

1. **Nothing in memory is ever deleted.** `MemoryStore` is `write`/`read`/`query`
   (`memory.py:14-17`); `SQLiteStore` adds only `namespaces()` (`stores.py:59`). `retention.py`
   deletes checkpoints, never memory.
2. **The pile is the capture rows.** With memory configured, every turn writes an `input:<uuid>`
   row and one `<node>:<uuid>` row per node firing into `episode:<run_id>/context` (`run.py:177`,
   `interceptor.py:106`). Since 4.8 a row holds only the messages its firing added, so an
   episode's rows grow linearly with its length, but episodes accumulate forever. A daemon on
   `SQLiteStore` (bumblebee) grows on disk with every turn.
3. **Everything else is one small record per run:** the `episodes/index` entry, the `deep:`
   marker once maintenance has run, and the episode summary `episode:<run_id>`
   (`consolidation.py:183-196`), each about 100 B. Knowledge grows with what is learned, and
   maintenance supersedes it rather than deleting it.
4. **Reusing a `run_id` is not the answer** (§G.1). It destroys trajectory the deep round never
   read; Phase 4.7 rejects recycled ids.

## B. Design

### B.1 The unit is the episode; the group is (owner, `task_id`)

Storage stays keyed by `run_id`. Count-based retention groups episodes by **(owner, `task_id`)**,
where the owner is the index's (`user_id`, `session_id`): never by `task_id` alone, which is
caller-supplied, so two tenants can share one (§G.4). `task_id` is in the index since 4.7.

### B.2 What is deleted, and what survives

| | Fate | Why |
|---|---|---|
| capture rows (`input:*`, `<node>:*` in `episode:<run_id>/context`) | **deleted** | the pile |
| episode summary `episode:<run_id>` | **kept** | a conclusion. A one-shot run keeps it in the same namespace, so pruning deletes keys, not the namespace |
| `maintenance/state` `deep:<run_id>` | **deleted** | dead once the rows are gone |
| `episodes/index` `<run_id>` | **kept**, unchanged: ownership is write-once (Phase 4.7) | a pruned `run_id` is never reused |
| `episodes/tombstone` `<run_id>` | **written**: `disposition`, `pruned_at`, `content_sha256` | keeps `source_ref="trajectory:<run_id>"` on derived records attributable and checkable (§B.5). Its own key, written only by the memory pass, so no other writer can overwrite it (scope-lifespans §A) |
| knowledge | **kept** | prune the source, never the conclusions |
| session memory, checkpoints | thread expiry (scope-lifespans §C.2) | another rung |

`unconsolidated_episodes` must skip tombstoned episodes. A pruned one-shot run's namespace
survives with its summary, and would otherwise reach the deep pass as an empty, unconsolidated
episode (`maintenance.py:107`).

### B.3 Which episodes may be pruned

vsigma enforces this, whatever the policy says. An episode is eligible only if:

1. **It ended for good:** a final status, `done`, `failed` or `abandoned`, in `episodes/status`
   (scope-lifespans §C.1). An `escalated` episode is parked, and a resume would write into a
   pruned namespace (revision 2 §B.5); a `running` one is still being written, or died and waits
   for expiry's sweep to record it `abandoned` (scope-lifespans §C.1). Episodes without a status
   record predate the lifecycle and are eligible only under `prune_legacy=True` (§H.3).
2. **It was consolidated, completely:** it has a `deep:` marker whose row count equals its
   transcript's. This one test decides "consolidated" everywhere: what the pass selects, this
   rule, and prior-context protection (§B.3.3). The memory pass consolidates only ended episodes
   (legacy ones as today), and the marker records how many rows it read. Rows added after the
   marker, as when a legacy episode was consolidated while paused and then resumed, make the
   episode unconsolidated again: the pass selects it, and pruning skips it until it is
   re-consolidated. Since 4.8 the deep pass
   reads the whole transcript (`episode_trajectory(..., window=None)`); revision 2's blocking
   prerequisite (§F.1) is resolved. `prune_unconsolidated=True` opts in to pruning before
   maintenance, recorded as `pruned_unconsolidated` (§G.3).
3. **No unconsolidated episode still reads it.** `episode_trajectory` prepends up to three
   earlier episodes of the same session as prior context (`max_prior_turns=3`,
   `consolidation.py:101-121`), in both the reflexive and the deep round. An episode is not
   eligible while any of the next three episodes of its session is unconsolidated by §B.3.2's
   test, its marker missing or stale, unless `prune_unconsolidated=True`. A turn that has not
   happened yet loses its grounding if its predecessors are pruned first; that is accepted,
   since they are kept by recency in practice.
4. **It is inside the selected sources:** the same mandatory boundary as maintenance.
   `sources=[...]`, or an explicit `None` for all (`--source`/`--all-sources`). Unknown ownership
   is matched only by `None` (`maintenance.py:81-99`).

### B.4 The policy

```python
@dataclass(frozen=True)
class RetentionPolicy:
    keep_last: int | None = None        # the N newest per (owner, task_id) stay
    max_age: timedelta | None = None    # episodes older than this go
    prune_unconsolidated: bool = False  # explicit opt-in (§B.3.2)
    prune_legacy: bool = False          # episodes without a status record (§B.3.1)
```

- vsigma applies it to eligible episodes only (§B.3), so no policy can prune unsafely.
- An episode is pruned when it breaks any limit that is set.
- `max_age` is needed from the start (§F.3): one-shot runs default `task_id` to their `run_id`,
  so `keep_last` alone never prunes them, and they are most runs.
- A policy with neither limit raises at construction, as do `keep_last < 1` and a non-positive
  `max_age`.

### B.5 The memory pass

Retention joins the existing maintenance pass, which consolidates first and then prunes, so an
episode consolidated in this pass is prunable in the same pass:

```python
def run_maintenance(
    store,                                  # a store that can delete (§C)
    llm: LLM,
    *,
    sources: list[Scope] | None,
    scope: Scope,
    constructor: str,
    out_dir: str | Path,
    limit: int = 20,
    retention: RetentionPolicy | None = None,   # new; None is today's consolidate-only pass
    dry_run: bool = False,
) -> MaintenanceReport
```

- **For each episode picked, the tombstone comes first:** hash its rows (canonical JSON,
  insertion order), write the tombstone with the hash, then delete the rows and the `deep:`
  marker. The hash is mandatory, and the rows are its only source (revision 2 §B.4), so it is
  stored before anything is deleted.
- **The tombstone is the prune's intent.** The protocol has no transactions to make the writes
  and deletes atomic, so a tombstoned episode with rows or a marker left is a prune a crash
  interrupted, and each pass finishes those before selecting new work. A final episode's rows
  no longer change, so the hash stays true until the deletes complete.
- `MaintenanceReport` gains the retention counts: `pruned`, `pruned_unconsolidated`, and the
  eligibility skips by reason (parked, unconsolidated, read as prior context, legacy), so
  "12 pruned, 2 before consolidation" is alertable.
- `dry_run` deletes nothing and reports the same counts.
- CLI: a new verb `vsigma memory maintain` takes `consolidate`'s flags plus `--keep-last N`,
  `--max-age DAYS`, `--prune-unconsolidated` and `--prune-legacy`. `consolidate` stays the
  consolidation-only pass.
- vsigma never schedules it. Cron runs the CLI daily, or a daemon calls the function on its own
  timer, outside its turns (maintenance-pass §B).
- **One memory pass per store at a time**, a documented boundary (scope-lifespans §B). Nothing
  today stops two overlapping passes from selecting the same batch; a lease in the database is
  later.

## C. Prerequisite: a store that can delete

The `MemoryStore` protocol is `write`/`read`/`query`. Pruning needs `delete`, as does thread
expiry's store side. The foundation is a LangGraph `BaseStore` behind the protocol
(scope-lifespans §D, thread-native-runtime §G.7), which brings `delete` without widening the
protocol. `run_maintenance` then takes that store, and `store.namespaces()` becomes the adapter's
`list_namespaces`. Widening the protocol itself would break every custom store, a `v0.3.0`
change no consumer needs.

## D. Consumers

- **bumblebee** is the consumer this is for: a daemon on `SQLiteStore` with
  `reflexive_consolidator`. Cron runs `vsigma memory maintain --max-age 30 …` daily, or its daemon
  calls `run_maintenance(..., retention=RetentionPolicy(max_age=timedelta(days=30)))`.
- **A web app** whose store is an `InMemoryStore` per connection, freed on disconnect and read
  by nothing, has nothing to do.

What stays unbounded across all rungs, and how a session's memory ends, is in scope-lifespans
§A and §C.

## E. Graduation criteria

- Two consolidated `done` episodes under one (owner, `task_id`), `keep_last=1`: the older one's
  capture rows and `deep:` marker are gone, its summary and index entry stay, and its tombstone
  carries `disposition="pruned"` and a `content_sha256`. Knowledge derived from it is untouched.
- A `running` or `escalated` episode is not consolidated. A `failed` one is consolidated and is
  prunable.
- An episode whose transcript gained rows after its marker is selected again and is not pruned
  until it is re-consolidated.
- An episode consolidated and pruned in the same pass: consolidation runs first.
- Two tenants both using `task_id="nightly-report"` keep their own allowance. Omitting both
  `--source` and `--all-sources` errors, as for `consolidate`.
- An `escalated` or `running` episode is never pruned. One recorded `abandoned` is eligible.
- An unconsolidated episode is not pruned by default, and is pruned under
  `prune_unconsolidated=True`, stamped `pruned_unconsolidated`.
- An episode that a later episode of its session reads as prior context is not pruned by default
  while that later episode's marker is missing or stale.
- Failure injection: a pass that fails after writing the tombstone, or after deleting the rows,
  is finished by the next pass, and the tombstone's hash matches the original rows. A failure
  before the tombstone deletes nothing.
- After pruning, `unconsolidated_episodes` never returns a tombstoned episode, including a
  one-shot run whose namespace survives with its summary.
- `max_age` prunes one-shot runs, whose `task_id` defaulted to their `run_id`.
- An episode without a status record is pruned only under `prune_legacy=True`.
- `retention=None` is today's pass, unchanged.
- `dry_run` deletes nothing and reports every count.
- A pruned `run_id` is still rejected by `record_episode`: pruning reclaims bytes, never names.

## F. Revision 2's open questions, answered

1. **How to fix the capped deep pass?** Fixed in Phase 4.8: the deep pass reads the whole
   transcript (`window=None`); the reflexive round keeps its window.
2. **CLI only, or a policy on `Services`?** Both callers, one mechanism: `run_maintenance` for a
   daemon's timer, `vsigma memory maintain` for cron. No policy on `Interceptor` (formerly
   `Services`); vsigma never schedules.
3. **A global age bound?** Yes, from the start: `max_age` (§B.4).
4. **Checkpoint eviction?** Its own proposals: [thread-retention](./thread-retention.md) (Phase
   4.10) and [thread-expiry](./thread-expiry.md).

## G. Decision log

- **G.1 — Reuse of a `run_id` to bound growth: REJECTED** (2026-09-06). Measured: it bounds the
  namespace by overwriting, destroying trajectory the deep round never read; it inherits the
  episode's `deep:` marker, so the slot is permanently marked consolidated; and it leaves
  LangGraph's checkpoint history growing regardless. Phase 4.7 rejects recycled ids.
- **G.2 — `run_id` derived from `user_id` + a caller-supplied `run_name`: REJECTED**
  (2026-09-06). It closes cross-owner collision but not cross-*session* collision; it puts
  persona identifiers into `source_ref`, a required field of the published bundle schema; and it
  creates a second source of ownership truth beside the index. `task_id` already provides a
  stable caller-supplied name, and is correct precisely because it does not key storage.
- **G.3 — Never pruning unconsolidated episodes: WITHDRAWN** (2026-09-06). Maintenance is
  operator-triggered, so a nightly job under weekly maintenance would find nothing prunable and
  the disk fills anyway. A safety rule that disables the feature is not a safety rule. Replaced
  by the safe default plus an explicit flag (§B.3).
- **G.4 — Revision 1 errors, withdrawn rather than quietly fixed** (2026-09-06): grouping by bare
  `task_id` (cross-tenant deletion); assuming `task_id` was persisted; claiming the
  source-boundary rule does not apply to deletion; treating a `deep:` marker as proof of
  processing when the source was capped; an optional content hash; omitting resume-after-prune.
- **G.5 — Revision 3 changes** (2026-10-27): rebased on 4.8–4.10. The capped-deep-pass
  prerequisite and the superlinear within-run growth are gone (4.8). New: capture rows deleted by
  key, keeping a one-shot run's summary, and tombstoned episodes skipped by maintenance; the
  terminal status stamped into the index; prior-context readers protecting their predecessors;
  `keep_last` and `older_than` policies; an inventory of what stays unbounded.
- **G.6 — Revision 4 changes** (2026-10-27):
  - pruning joins `run_maintenance` (consolidate, then prune) and a `vsigma memory maintain` verb,
    replacing the separate `prune_episodes`;
  - a declarative `RetentionPolicy` replaces policy functions; `older_than` becomes `max_age`;
  - the prerequisite becomes a store that can delete via `BaseStore`, replacing a `SQLiteStore`
    extra;
  - the status stamps, session memory and the inventory move to scope-lifespans;
  - revision 3 §D's claim that session-scope knowledge exists only for sessions without a
    `user_id` was wrong: by default all reflexive knowledge lands in session scope
    (`consolidation.py:51-72`).

- **G.7 — Revision 5 changes** (2026-10-27), answering an audit:
  - the memory pass consolidates only ended episodes; the marker records the transcript's row
    count, and growth after it makes the episode unconsolidated again (§B.3.2);
  - `failed` is a final, prunable status, and statuses come from scope-lifespans' lifecycle,
    kept in `episodes/status`;
  - the tombstone moves to its own key, `episodes/tombstone`, leaving the write-once index entry
    untouched (§B.2);
  - one memory pass per store, as a documented boundary (§B.5);
  - after the audit's follow-up (2026-10-29): the tombstone, with its hash, is written before
    the rows are deleted and is the prune's intent, finished by the next pass after a crash
    (§B.5); prior-context protection uses the same completeness test as selection and pruning
    (§B.3.3).

## H. Verdicts to decide

1. **Retention in the memory pass:** `run_maintenance(..., retention=...)` and
   `vsigma memory maintain`, consolidating before pruning. Recommended: yes.
2. **The declarative policy** (§B.4), with no policy functions for now. Recommended: yes.
3. **Episodes without a status record** are prunable only under `prune_legacy=True`, never by
   inference. Recommended: yes; the explicit-configuration posture of memory-scope-promotion
   §B.1.
4. **Built after the `BaseStore` adapter** (scope-lifespans §E step 4), in a `v0.2.x` release.
   Recommended: yes.
