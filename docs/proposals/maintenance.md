# Proposal: Maintenance — the Background Jobs an Agent Needs

**Status:** **GRADUATED** 2026-10-31 (Phase 4.14, v0.3.0). Built as `src/vsigma/maintenance.py`, `src/vsigma/memory_pass.py`, `Agent.maintain()`, and `vsigma memory maintain` CLI.
**Scope:** how vsigma's background jobs are built and run. How long each scope keeps what it
holds is [scope-lifespans](./scope-lifespans.md), which the jobs enforce.
**Jobs:** [thread-expiry](./thread-expiry.md) (Phase 4.12), the memory pass
([maintenance-pass](./maintenance-pass.md), Phase 4.3) and
[episode-retention](./episode-retention.md); later
[procedure-extraction](./procedure-extraction.md).
**Needs:** [store-adapter](./store-adapter.md), for a store that can delete.

## A. Problem

| | Thread expiry | Memory pass |
|---|---|---|
| Work | end idle sessions and forgotten runs; recover dead episodes | consolidate transcripts into knowledge |
| Cost | a scan and some deletes | LLM calls, batched |
| Runs | in the app's process | from cron, outside the app |
| Entry point | `ThreadExpiry.run()` | `run_maintenance`, `vsigma memory consolidate` |

1. **The app wires what vsigma already knows.** It chooses the jobs, orders them, handles their
   failures, and passes each the checkpointer and store its agent already holds. Every new job
   changes every app.
2. **Storage grows unless the app acts.** Over one 200-turn session with short replies, checkpoints
   took 20.5 MB, or 202 KB keeping only the latest; transcripts took 232 KB and bookkeeping 26 KB.
   Nothing bounds any of this by default.
3. **The memory pass needs a tenant list.** `sources` and `scope` name one tenant per call, so an
   app with many users must enumerate them, although `episodes/index` records every episode's
   owner.
4. **The memory pass is not yet safe to run in the app.** It selects every transcript without a
   `deep:` marker, running and paused episodes included (`maintenance.py:81-117`), marks them
   consolidated and never reads them again. Nothing stops two passes on one store, which doubles
   the LLM cost and writes near-duplicate knowledge.
5. **`run_maintenance` is misnamed.** It and `MaintenanceReport` run and report the memory pass
   alone.

## B. Design

### B.1 One call

```python
agent.maintain()                                          # recover and prune; no LLM
agent.maintain(distiller=llm)                             # also consolidate
agent.maintain(expiry=ExpiryPolicy(idle_ttl=timedelta(days=7)))   # also end idle sessions
agent.maintain(retention=RetentionPolicy(max_bytes=...))  # another budget; None turns pruning off
```

The checkpointer and store come from the agent (`Agent.graph`, `Agent.memory`). The jobs run in this
order, so an episode recorded `abandoned` is consolidated, and can be pruned, in the same call:

| Job | Runs when |
|---|---|
| Thread expiry | `expiry` is given |
| Recovery sweep | the agent has a store and a checkpointer, whose locks tell a live run from a dead one |
| Memory pass | `distiller` is given |
| Old checkpoints | the agent has a checkpointer, and `retention` keeps `checkpoints="latest"` |
| Transcript budget | the store can delete, and `retention` is not `None` |

- **By default it prunes and spends nothing.** Old checkpoints go, and transcripts stay within 1 GiB
  per store (episode-retention §B.4). No conversation and no knowledge is deleted.
- **Ending a conversation and spending on an LLM need the app's word.** `expiry` and `distiller`
  have no defaults. To consolidate with the agent's own LLM, the app passes it. A later job with a
  cost or a loss of the same kind gets its own argument; any other joins without a change to the app.
- **Signature:** `maintain(*, expiry=None, distiller=None, retention=RetentionPolicy(), limit=20,
  out_dir=None, now=None, dry_run=False) -> MaintenanceReport(expiry, memory, retention)`. A job that
  did not run reports `None`.
- **It covers the agent's whole checkpointer and store,** for every user. Old checkpoints skip a busy
  or paused thread until a later call. A paused thread holds no lock, so the pause is read under the
  thread's lock, not from the scan, which a turn may have overtaken. Pruning a pause that arrived after
  the scan makes a resume rerun finished subgraph steps. `keep_latest` refuses a `DeltaChannel`, so an
  agent with one sets `checkpoints="all"`.
- **Every job always runs.** If any raises, `maintain` then raises an `ExceptionGroup` of the
  failures.
- **`dry_run` commits nothing.** A memory dry run still calls the distiller and writes a bundle to
  review (`memory_pass.py:166-171`).
- **What cannot be done raises:** an agent with neither checkpointer nor store, a `distiller` without
  a `MaintainableStore`, a `distiller` with no `out_dir` on a store with no path, or a checkpointer
  expiry does not support, which the sweep and old checkpoints also need. A store that cannot delete
  skips the transcript budget and reports it.
- **`maintain` needs one agent per store.** It treats the store as its agent's: the sweep sees only
  that agent's locks and pauses, so another agent's live or parked turn looks dead, and would be
  abandoned and then pruned. Each template makes its own checkpointer, so this is easy to do by
  accident, and `maintain` raises when another live `Agent` in the process holds its store. Agents may
  still share a store without calling it, and `MemoryPass` and `EpisodeRetention` stay usable there
  (§B.3).
- **One timing for all jobs:** the app calls `maintain` as often as the slowest job needs, say
  hourly. Expiry's limits are usually days, and a pass with no newly ended episode makes no LLM
  call.
- **It is not a hard cap.** Latest checkpoints stay until `expiry` ends their threads, and bookkeeping
  (about 130 B per run) stays forever. Transcripts protected from pruning can exceed the budget, and
  the report says so.

### B.2 Knowledge lands with its owner

`maintain` routes each episode by its `episodes/index` entry:

| Owner | Destination |
|---|---|
| `user_id` | `persona(user_id)` |
| `session_id` only | `session(session_id)` |
| neither, or no entry | skipped, and counted in `MemoryPassReport.unowned` |

- **One batch, bundle and ingest per destination,** so tenants never mix and a persona's sessions
  cost one LLM call, not one each. This is the "one pass per persona" maintenance-pass §B asks of
  multi-tenant operators, done for them.
- **One pass, one report:** the guard of §C.1.2 spans every batch of the call, and `limit` bounds the
  call, not each owner. `MaintenanceReport.memory` is one `MemoryPassReport`, whose `bundles` lists one
  bundle per batch.
- **Never `AGENT`:** it is visible to every persona (Phase 4.7).
- **A batch reads only its owner's turns.** Prior context is still the last three turns of the session
  (`consolidation.py:106-110`), and another persona's among them are left out, not replaced by older
  ones, so it never reaches past what retention protects.
- **A failed batch blocks only its owner.** The others proceed and the failure is raised. A batch
  that always fails is retried, at LLM cost, on every call: a known limit, and a loud one.
- **Nothing that cannot finish holds up the rest:** unowned episodes and failed batches do not use up
  `limit`, or the oldest of them would fill it on every call and stop consolidation for everyone.
- **Defaults:** `limit` bounds the episodes per call, oldest first; bundles go to
  `<store path>.bundles/` unless `out_dir` says otherwise; the constructor is `memory-pass`.

### B.3 Two levels

As with templates and primitives, `agent.maintain` is the fast path and the jobs stay usable alone:
`ThreadExpiry` for separate timing, `EpisodeRetention` likewise, and `MemoryPass` with explicit
`sources` and `scope` for agent-wide knowledge or a review step. The CLI uses `MemoryPass` and
`EpisodeRetention`.

### B.4 What every job does

1. **Policy as data**, declared once (`ExpiryPolicy`, `RetentionPolicy`).
2. **Safe to call repeatedly:** a call does what is due, cheaply when nothing is, and jobs need
   no order between them to be safe (scope-lifespans §C).
3. **`dry_run` and a report** on every call.
4. **Crash-safe:** intent is recorded before anything is deleted, and progress advances only
   after success, so the next call retries.
5. **Busy means skip:** a job skips a thread or store that is in use, and leaves it to a later call. It reports
   it.
6. **No scheduling:** the app calls `maintain` when it chooses; vsigma starts no thread.

### B.5 Where a job runs

A job that needs thread locks runs in the app's process, where the lock registry lives and knows
a checkpointer by object (`threads.py:41-42`): thread expiry and old checkpoints. A job that needs
only the store can run in the app or outside it, because its safety lives in records any process
sees (statuses, markers, tombstones, the bundle gate): the memory pass and the transcript budget.
The boundaries of scope-lifespans §B hold: one process per checkpointer, one maintainer per store.
`agent.maintain` runs in the app only.

### B.6 Names

| Today | Becomes |
|---|---|
| `run_maintenance(store, llm, *, sources, scope, constructor, out_dir, limit, dry_run)` | `MemoryPass(store, llm, *, sources, scope, constructor, out_dir, limit=20).run(*, dry_run=False)` |
| `MaintenanceReport`, the memory pass's report | `MemoryPassReport` |
| — | `MaintenanceReport`, the report of `Agent.maintain` |
| "the maintenance pass" in the docs | "the memory pass" |

`run_maintenance` has no callers in this repository besides the CLI (`cli.py:126`) and its tests,
and the web app uses neither name, so the renames come without aliases. They are breaking, so they
ship in `v0.3.0`, and `upgrading.md` says what changed. Kept: `vsigma memory consolidate`,
`unconsolidated_episodes`, the `maintenance/state` namespace (stored data) and the filename
`maintenance-pass.md`. `vsigma/maintenance.py` holds `maintain`, `MaintenanceReport` and
`MAINTENANCE_NS`; the memory pass moves to its own module. `ExpiryPolicy` stays as it is;
`ThreadExpiry`'s policy becomes optional, so the sweep can run without a limit.

## C. The memory pass

### C.1 Inside the app — built first

1. **Only ended episodes:** a final status (`done`, `failed`, `abandoned`). An episode with no status
   ran outside `Agent` and has no lifecycle, so only an explicit `sources=None` selects it, as with
   unknown ownership. The marker records the namespace's size from `namespace_sizes`, read before the
   transcript, and an episode counts as consolidated only while the two match. Counting rows instead
   reads every transcript on every call, about 7 s per GiB against 43 ms for 4,000 namespaces. This is
   scope-lifespans §C.3, built now: it needs only reads and a marker field.
2. **One pass per store at a time:** an in-process guard keyed by the store object. A call while
   another runs returns at once with `MemoryPassReport.busy` set.
3. **Sharing the process:** `PersistentStore` serializes its connection under a lock, so a background
   thread can share the agents' store. The pass's LLM calls compete with live turns: run it
   off-peak, with a small `limit`, or give it its own LLM.
4. **Never inside a turn:** the run-end hook is the reflexive round's place (architecture §9).

This amends maintenance-pass §B, which keeps the pass out of the agent's process.

### C.2 Outside the app — delayed

Running the pass from cron, with a more capable model or an external agent (export → bundle →
ingest), is delayed. It needs the one-pass rule across processes: a lease record in the store,
taken atomically, renewed between batches and expiring if its holder dies. Until then a store's
pass runs in the app or from cron, never both.

## D. Build order

1. [store-adapter](./store-adapter.md), its own phase, comes first.
2. One phase, also released as `v0.3.0`: `MemoryPass` (§B.6, §C.1), then `Agent.maintain` with owner
   routing (§B.1, §B.2), then [episode-retention](./episode-retention.md) as its default, then the
   docs, where §B.4–§B.5 move into the architecture.
3. The outside run (§C.2), when a consumer needs it.
4. New jobs, as their proposals are accepted.

## E. Alternatives considered

- **The app builds the jobs:** `Maintenance(expiry=ThreadExpiry(...), memory=MemoryPass(...))`. It
  repeats what the agent holds, and each new job changes every app.
- **Consolidating by default with the agent's LLM.** It removes one argument, but makes the default
  call spend money, and an app must opt out. The default stays free.
- **Pruning only when the app asks.** An app that never sets a limit grows without bound, which is
  the problem.
- **A `MaintenancePolicy` object.** Keyword arguments say the same with one class fewer.
- **Limits declared on the templates.** Four templates gain arguments that only the timer reads.
- **The app names `sources` and `scope`.** It must list its tenants, and one mistaken scope is the
  cross-tenant leak of Phase 4.7. Routing by owner cannot express that mistake.
- **Unowned episodes land in `AGENT`.** That is an inferred widening, visible to every persona.
- **`run_maintenance` also runs expiry.** The CLI calls it in its own process, where expiry would
  hold another checkpointer object, see none of the agents' locks and could delete a thread
  mid-turn.
- **A coordinator with intervals** (`tick()`). That is scheduling in vsigma (§B.4 rule 6). Revisit
  when a third job lands.
- **A control channel between the CLI and the app.** The shared store already coordinates them.

## F. Non-goals

- Scheduling: the app or cron owns time.
- Ending conversations or spending on an LLM without the app's word.
- Deleting knowledge (scope-lifespans §G).
- Agent-wide knowledge from `agent.maintain`: that takes an explicit `MemoryPass`.
- Running one checkpointer's or one store's jobs from several app processes.

## G. Verdicts (decided 2026-10-31)

1. **The job rules and where a job runs** (§B.4–§B.5). **decided 2026-10-31: yes.**
2. **One call, `agent.maintain(...)`** (§B.1). It reverses the 2026-10-30 rejection of one
   `maintain()` for every job, and answers its reasons: costly and lossy jobs are opt-in, expiry runs
   first, every job always runs, and the pass runs in the app anyway (§C.1). **decided 2026-10-31: yes.**
3. **The default prunes; consolidating is opt-in** (§B.1) — **decided 2026-10-31: yes**, with a budget
   of 1 GiB of transcripts per store. This departs from "the app declares limits" (scope-lifespans
   §B) for this one default.
4. **Knowledge lands with its owner; unowned episodes are skipped** (§B.2). An app running without
   a `user_id` or `session_id` gets no deep knowledge from `maintain` until it sets one, or uses
   `MemoryPass`. **decided 2026-10-31: yes.**
5. **The renames, without aliases, in `v0.3.0`** (§B.6). bumblebee is unchecked and may need a
   one-line fix. **decided 2026-10-31: yes.**
6. **The memory pass inside the app** (§C.1), amending maintenance-pass §B. **decided 2026-10-31: yes.**
7. **Only ended episodes, now** (§C.1.1), with the marker recording size in place of the row count first
   decided. **decided 2026-10-31: yes;** the in-app pass is unsafe without it.
8. **A busy pass skips and reports** (§C.1.2). **decided 2026-10-31: yes.**
