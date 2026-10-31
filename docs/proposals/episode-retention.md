# Proposal: Episode Retention — Pruning Trajectory Without Losing Episodes

**Status:** **GRADUATED** 2026-10-31 (Phase 4.14, v0.3.0). Built as `src/vsigma/retention_episodes.py` (`EpisodeRetention`, `RetentionPolicy`, `RetentionReport`).
**Component of:** [scope-lifespans](./scope-lifespans.md), which holds the model: this proposal
enforces the episode rung's lifespan for transcripts. It runs by default through
[maintenance](./maintenance.md) §B.1.
**Closes:** [memory-construction](./memory-construction.md)'s open question, "how long raw
episodes are kept after the deep pass has processed them", honouring its two invariants: a
pruned episode's tombstone carries a content hash and a disposition, and a pruned `run_id` is
never reused.

## A. Problem

1. **Nothing in memory is ever deleted.** `retention.py` deletes checkpoints, never memory.
2. **The pile is the capture rows.** With memory configured, every turn writes an `input:<uuid>`
   row and one `<node>:<uuid>` row per node firing into `episode:<run_id>/context` (`run.py:219`,
   `interceptor.py:106`). A row holds only the messages its firing added, so an episode's rows grow
   linearly with its length, but episodes accumulate forever. A daemon on `PersistentStore`
   (bumblebee) grows on disk with every turn.
3. **Everything else is one small record per run:** the `episodes/index` and `episodes/status`
   entries, the `deep:` marker, and the episode summary `episode:<run_id>`
   (`consolidation.py:183-196`). Knowledge grows with what is learned, and the memory pass
   supersedes it rather than deleting it.

## B. Design

### B.1 The unit is the episode; the group is (owner, `task_id`)

Storage stays keyed by `run_id`. Count-based retention groups episodes by **(owner, `task_id`)**,
where the owner is the index's (`user_id`, `session_id`): never by `task_id` alone, which is
caller-supplied, so two tenants can share one.

### B.2 What is deleted, and what survives

| | Fate | Why |
|---|---|---|
| capture rows (`input:*`, `<node>:*` in `episode:<run_id>/context`) | **deleted** | the pile |
| episode summary `episode:<run_id>` | **kept** | a conclusion. A one-shot run keeps it in the same namespace, so pruning deletes keys, not the namespace |
| `maintenance/state` `deep:<run_id>` | **deleted** | dead once the rows are gone |
| `episodes/index` `<run_id>` | **kept**, unchanged: ownership is write-once (Phase 4.7) | a pruned `run_id` is never reused |
| `episodes/tombstone` `<run_id>` | **written**: `disposition` (`pruned` or `pruned_unconsolidated`), `pruned_at`, `content_sha256` | keeps `source_ref="trajectory:<run_id>"` on derived records attributable and checkable. Its own key, with one writer (scope-lifespans §A) |
| knowledge | **kept** | prune the source, never the conclusions |
| session memory, checkpoints | thread expiry and `keep_latest` | another rung |

`unconsolidated_episodes` must skip tombstoned episodes. A pruned one-shot run's namespace
survives with its summary, and would otherwise reach the memory pass as an empty, unconsolidated
episode (`maintenance.py:107`).

### B.3 Which episodes may be pruned

vsigma enforces this, whatever the policy says. An episode is eligible only if:

1. **It ended for good:** a final status, `done`, `failed` or `abandoned`, in `episodes/status`
   (scope-lifespans §C.1). An `escalated` episode is parked, and a resume would write into a
   pruned namespace; a `running` one is still being written, or died and waits for expiry's sweep.
   An episode with no status ran outside `Agent`, has no lifecycle, and is never eligible.
2. **It was consolidated, or nothing will consolidate it.** "Consolidated" has one test everywhere,
   for selection, this rule and §B.3.3: a `deep:` marker whose size equals the namespace's in
   `namespace_sizes`. Unconsolidated transcripts are protected whenever consolidation is in use: a
   `distiller` was given, or the store holds a `deep:` marker or a `pruned` tombstone, which covers a
   pass run from cron even after every marker has been pruned. Otherwise nothing will ever read them,
   so they are eligible, recorded as `pruned_unconsolidated`.
   `prune_unconsolidated=True` makes them eligible regardless.
3. **No unconsolidated episode still reads it.** `episode_trajectory` prepends up to three
   earlier episodes of the same session as prior context (`max_prior_turns=3`,
   `consolidation.py:101-121`). While consolidation is in use, an episode is not eligible while any
   of the next three episodes of its session, in index order and in any state, is unconsolidated,
   unless `prune_unconsolidated=True`. A pruned episode counts toward the three but is not
   unconsolidated: nothing will consolidate it, so it reads nothing.
4. **It is inside the selected sources:** the same mandatory boundary as the memory pass,
   `sources=[...]` or an explicit `None` for all. `agent.maintain` passes `None`, since it maintains
   the whole store. Unknown ownership is matched only by `None` (`maintenance.py:81-99`).

### B.4 The policy

```python
@dataclass(frozen=True)
class RetentionPolicy:
    max_bytes: int | None = 1024**3     # transcripts in this store
    keep_last: int | None = None        # the N newest per (owner, task_id) stay
    max_age: timedelta | None = None    # episodes older than this go
    checkpoints: Literal["all", "latest"] = "latest"  # read by agent.maintain only
    prune_unconsolidated: bool = False  # §B.3.2
```

- **`RetentionPolicy()` is the default of `agent.maintain`.** One limit vsigma chooses, so an app
  that never sets one is still bounded. 1 GiB is well above what a typical app needs for recent
  history.
- **Least relevant goes first.** A finished transcript never reaches a prompt, because recall sees
  only the current episode (`scope.py:72-74`); only the memory pass and prior context read it. So
  over budget, consolidated episodes go before unconsolidated ones, and within each the oldest goes
  first (`created_at` in the index, which has one-second precision, so index order breaks ties), until
  the store is under budget.
- **Size comes from `namespace_sizes`** (store-adapter §B.4), with no scan of the rows: the budget
  counts the `episode:*/context` namespaces, and a prune frees only its rows' bytes, since a one-shot
  run's summary stays.
- **`keep_last` counts every episode of the group not yet pruned,** whatever its status, so a running
  episode is one of the newest. **`max_age`** compares `created_at`, stored naive in local time.
- **Eligibility always applies** (§B.3), so no policy can prune unsafely. An episode is pruned when
  it breaks any limit that is set. Transcripts protected by §B.3 can exceed the budget, and the
  report says so.
- `max_age` matters beside `keep_last`: one-shot runs default `task_id` to their `run_id`, so
  `keep_last` alone never prunes them, and they are most runs.
- `max_bytes < 1`, `keep_last < 1` and a non-positive `max_age` raise at construction.

### B.5 The job

```python
EpisodeRetention(store, policy, *, sources, consolidating=False).run(*, now=None, dry_run=False) -> RetentionReport
```

It stands apart from the memory pass, because the default call has no LLM and so no pass. In
`agent.maintain` it runs after the pass, so an episode consolidated in a call is prunable in that
call.

- **For each episode picked, the tombstone comes first:** hash its rows (canonical JSON: sorted keys,
  ASCII form, so a lone surrogate hashes; first-write order), write the tombstone with the hash, then
  delete the rows and the `deep:` marker. The rows
  are the hash's only source, so it is stored before anything is deleted.
- **The tombstone is the prune's intent.** A tombstoned episode with rows or a marker left is a prune
  a crash interrupted, and each run finishes those before selecting new work. A final episode's rows
  no longer change, so the hash stays true until the deletes complete.
- **`RetentionReport`** holds `pruned`, `pruned_unconsolidated`, the skips by reason (parked,
  unconsolidated, read as prior context, no status), `over_budget` bytes and, under `agent.maintain`,
  `checkpoints_pruned`, so "12 pruned, 2 before consolidation" is alertable.
- `dry_run` deletes nothing, an interrupted prune included, and reports the same counts.
- **CLI:** `vsigma memory maintain` takes `consolidate`'s flags plus `--max-bytes`, `--keep-last`,
  `--max-age DAYS` and `--prune-unconsolidated`, and always runs the memory pass, then this job with
  `consolidating=True`. `consolidate` stays consolidation only.
- **One maintainer per store,** a documented boundary (scope-lifespans §B).

## C. Prerequisite: a store that can delete

`EpisodeRetention` takes a `MaintainableStore`, from [store-adapter](./store-adapter.md). A store
that cannot delete skips the job under `agent.maintain`, which reports it, and raises when the job is
built directly.

## D. Consumers

- **bumblebee**, a daemon on `PersistentStore` with `reflexive_consolidator`: `agent.maintain()` on its
  timer bounds its transcripts with no other change.
- **A web app** with an `InMemoryStore` per connection, freed on disconnect, has nothing to do. On
  `PersistentStore` it gets the same default.

What stays unbounded across all rungs, and how a session's memory ends, is in scope-lifespans
§A and §C.

## E. Graduation criteria

- Two `done` episodes under one (owner, `task_id`), both consolidated, `keep_last=1`: the older
  one's capture rows and `deep:` marker are gone, its summary and index entry stay, and its tombstone
  carries `disposition="pruned"` and a `content_sha256`. Knowledge derived from it is untouched.
- Over `max_bytes`, consolidated episodes go first, oldest first, and pruning stops under budget.
- With no `deep:` marker, no `pruned` tombstone and no `distiller`, unconsolidated episodes are pruned
  and stamped `pruned_unconsolidated`. With a `distiller`, a `deep:` marker or a `pruned` tombstone they
  are kept, and `over_budget` reports the excess. A hash over a lone surrogate works.
- An `escalated`, `running` or status-less episode is never pruned. One recorded `abandoned` is
  eligible.
- An episode whose namespace changed size after its marker is selected again and is not pruned
  until it is re-consolidated.
- An episode consolidated and pruned in the same call: consolidation runs first.
- Two tenants both using `task_id="nightly-report"` keep their own allowance. Omitting both
  `--source` and `--all-sources` errors, as for `consolidate`.
- An episode that a later episode of its session reads as prior context is not pruned while
  consolidation is in use and that later episode's marker is missing or stale.
- Failure injection: a run that fails after writing the tombstone, or after deleting the rows, is
  finished by the next run, and the tombstone's hash matches the original rows. A failure before the
  tombstone deletes nothing.
- After pruning, `unconsolidated_episodes` never returns a tombstoned episode, including a
  one-shot run whose namespace survives with its summary.
- `max_age` prunes one-shot runs, whose `task_id` defaulted to their `run_id`.
- `dry_run` deletes nothing and reports every count.
- A pruned `run_id` is still rejected by `record_episode`: pruning reclaims bytes, never names.

## F. Questions answered

1. **The capped deep pass.** Fixed in Phase 4.8: the deep pass reads the whole transcript
   (`window=None`); the reflexive round keeps its window.
2. **CLI only, or a policy on `Interceptor`?** Both callers, one mechanism: `agent.maintain` for an
   app's timer, `vsigma memory maintain` for cron. No policy on `Interceptor`; vsigma never schedules.
3. **A global age bound?** Yes: `max_age` (§B.4).
4. **Checkpoint eviction?** Its own proposals: [thread-retention](./thread-retention.md) (Phase
   4.10) and [thread-expiry](./thread-expiry.md).

## G. Alternatives considered

- **Reusing a `run_id` to bound growth.** It overwrites trajectory the deep round never read,
  inherits the old episode's `deep:` marker, and leaves checkpoints growing. Phase 4.7 rejects it.
- **A `run_id` derived from `user_id` and a caller's name.** It puts persona identifiers into
  `source_ref`, a required field of the published bundle schema, and adds a second source of
  ownership beside the index.
- **Never pruning unconsolidated episodes.** An app with no distiller could never prune at all, so
  the disk fills anyway.
- **Retention inside the memory pass.** The default call has no LLM, so it would never run.
- **Grouping by bare `task_id`.** It deletes across tenants.
- **An optional content hash.** The rows are its only source, so it could never be added later.
- **Pruning by file size.** SQLite does not shrink a file on delete, so the size would not fall.
- **A per-owner budget.** It is fairer, since one heavy user can push out a light user's oldest
  transcripts, but it leaves the total unbounded. `keep_last` covers per-owner limits.
- **Relevance scored per query.** It needs the semantic index (Phase 4.5), and finished transcripts
  do not reach prompts anyway.
- **Pruning knowledge.** A non-goal of scope-lifespans §G; it needs the tombstones of
  maintenance-pass §D.

## H. Verdicts (decided 2026-10-31)

1. **A separate job, run by `agent.maintain` after the memory pass, with `vsigma memory maintain` for
   cron** (§B.5). **decided 2026-10-31: yes.**
2. **The declarative policy** (§B.4), with no policy functions for now. **decided 2026-10-31: yes.**
3. **`RetentionPolicy()` as the default, at 1 GiB of transcripts per store** — **decided 2026-10-31:
   yes.**
4. **Unconsolidated transcripts are protected when consolidation is in use, and prunable when it is
   not** (§B.3.2). Without a distiller, deleted transcripts cannot be consolidated later.
   **decided 2026-10-31: yes.**
5. **Consolidated first, then oldest, as "least relevant"** (§B.4). **decided 2026-10-31: yes.**
6. **Episodes with no status are never pruned** (§B.3.1). **decided 2026-10-31: yes.**
7. **Built with maintenance, after the store adapter, in `v0.3.0`.** **decided 2026-10-31: yes.**
