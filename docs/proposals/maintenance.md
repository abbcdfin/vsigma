# Proposal: Maintenance — the Background Jobs an Agent Needs

**Status:** **DRAFT** 2026-10-31. Awaits a Decision Check Point (§G).
**Scope:** how vsigma's background jobs are built and run. How long each scope keeps what it
holds is [scope-lifespans](./scope-lifespans.md), which two of the jobs enforce.
**Jobs:** [thread-expiry](./thread-expiry.md) (Phase 4.12) and the memory pass
([maintenance-pass](./maintenance-pass.md), Phase 4.3); later
[episode-retention](./episode-retention.md) and [procedure-extraction](./procedure-extraction.md).

## A. Problem

| | Thread expiry | Memory pass |
|---|---|---|
| Work | end idle sessions and forgotten runs; recover dead episodes | consolidate transcripts into knowledge |
| Cost | a scan and some deletes | LLM calls, batched |
| Runs | in the app's process | from cron, outside the app |
| Entry point | `ThreadExpiry.run()` | `run_maintenance`, `vsigma memory consolidate` |

1. **No shared rules and no single entry point.** Each job chose its own shape, and the app wires
   both, their order and their failures by hand.
2. **The memory pass is not yet safe to run in the app.** It selects every transcript without a
   `deep:` marker, running and paused episodes included (`maintenance.py:81-117`), marks them
   consolidated and never reads them again. Nothing stops two passes on one store, which doubles
   the LLM cost and writes near-duplicate knowledge.
3. **`run_maintenance` is misnamed.** It and `MaintenanceReport` run and report the memory pass
   alone.

## B. Design

### B.1 What every job does

1. **Policy as data**, declared once (`ExpiryPolicy`).
2. **Safe to call repeatedly:** a call does what is due, cheaply when nothing is, and jobs need
   no order between them (scope-lifespans §C).
3. **`dry_run` and a report** on every call.
4. **Crash-safe:** intent is recorded before anything is deleted, and progress advances only
   after success, so the next call retries.
5. **Busy means skip:** a job skips a thread or store that another operation holds, and reports
   it.
6. **No scheduling:** the app calls the jobs when it chooses; vsigma starts no thread.

### B.2 Where a job runs

A job that needs thread locks runs in the app's process, where the lock registry lives and knows
a checkpointer by object (`threads.py:41-42`): thread expiry. A job that needs only the store can
run in the app or outside it, because its safety lives in records any process sees (statuses,
markers, the bundle gate): the memory pass. The boundaries of scope-lifespans §B hold: one
process per checkpointer, one maintainer per store.

### B.3 One call for the app

```python
maintenance = Maintenance(
    expiry=ThreadExpiry(checkpointer, ExpiryPolicy(idle_ttl=timedelta(days=7)), store=store),
    memory=MemoryPass(store, distiller, sources=[persona("alice")], scope=persona("alice"),
                      constructor="app", out_dir="/srv/bundles"),
)
report = maintenance.run()     # MaintenanceReport(expiry=ExpiryReport, memory=MemoryPassReport)
```

- **Either part is optional**, and one is required. An app without memory needs no LLM.
- **Expiry runs first**, so the dead episodes it records `abandoned` are consolidated in the same
  call.
- **Both parts always run.** If either raises, `run()` then raises an `ExceptionGroup` of the
  failures.
- **`run(*, now=None, dry_run=False)`** passes `dry_run` to both parts and `now` to expiry. A part
  not configured reports `None`.
- **One timing for both:** the app calls `run()` as often as the slower job needs, say hourly.
  Expiry's limits are usually days, and a pass with no newly ended episode makes no LLM call.
  Each job keeps its own entry point for an app that wants separate timing.
- **In the app only**, since it holds expiry (§B.2). The CLI runs the memory pass alone.

### B.4 Names

| Today | Becomes |
|---|---|
| `run_maintenance(store, llm, *, sources, scope, constructor, out_dir, limit, dry_run)` | `MemoryPass(store, llm, *, sources, scope, constructor, out_dir, limit=20).run(*, dry_run=False)` |
| `MaintenanceReport`, the memory pass's report | `MemoryPassReport` |
| — | `MaintenanceReport`, the report of `Maintenance.run()` |
| "the maintenance pass" in the docs | "the memory pass" |

`run_maintenance` has no callers besides the CLI (`cli.py:126`) and its tests, so the renames
come without aliases. They are breaking, so they ship in `v0.3.0`, and `upgrading.md` says what
changed. Kept: `vsigma memory consolidate`, `unconsolidated_episodes`, the `maintenance/state`
namespace (stored data) and the filename `maintenance-pass.md`. `vsigma/maintenance.py` holds
`Maintenance`, `MaintenanceReport` and `MAINTENANCE_NS`; the memory pass moves to its own module.

## C. The memory pass

### C.1 Inside the app — built first

1. **Only ended episodes:** a final status (`done`, `failed`, `abandoned`), or no status record
   (legacy). The marker records the transcript's row count, and an episode counts as consolidated
   only while the two match. This is scope-lifespans §C.3, built now: it needs only reads and a
   marker field.
2. **One pass per store at a time:** an in-process guard keyed by the store object. A call while
   another runs returns at once with `MemoryPassReport.busy` set.
3. **Sharing the process:** `SQLiteStore` serializes its connection under a lock
   (`stores.py:21-25`), so a background thread can share the agents' store. The pass's LLM calls
   compete with live turns: run it off-peak, with a small `limit`, or give it its own LLM.
4. **Never inside a turn:** the run-end hook is the reflexive round's place (architecture §9).

This amends maintenance-pass §B, which keeps the pass out of the agent's process.

### C.2 Outside the app — delayed

Running the pass from cron, with a more capable model or an external agent (export → bundle →
ingest), is delayed. It needs the one-pass rule across processes: a lease record in the store,
taken atomically, renewed between batches and expiring if its holder dies. Until then a store's
pass runs in the app or from cron, never both.

## D. Build order

1. One phase, released as `v0.3.0`: `MemoryPass` (§B.4, §C.1), then `Maintenance` (§B.3), then
   the docs, where §B.1–§B.2 move into the architecture.
2. The outside run (§C.2), when a consumer needs it.
3. New jobs, as their proposals are accepted.

## E. Alternatives considered

- **The app calls each job itself.** It still can, but every app would wire the same two calls.
- **`run_maintenance` also runs expiry.** The CLI calls it in its own process, where expiry would
  hold another checkpointer object, see none of the agents' locks and could delete a thread
  mid-turn.
- **A coordinator that decides which jobs are due** (`tick()`, with intervals). That is
  scheduling in vsigma (§B.1 rule 6). Revisit when a third job lands.
- **A control channel between the CLI and the app.** The shared store already coordinates them.

## F. Non-goals

- Scheduling: the app or cron owns time.
- Running one checkpointer's or one store's jobs from several app processes.

## G. Decision Check Point

1. **The job rules and where a job runs** (§B.1–§B.2). Recommended: yes.
2. **One call, `Maintenance(...).run()`** (§B.3). It reverses the 2026-10-30 rejection of one
   `maintain()` for every job, and answers its reasons: the memory part is optional, expiry runs
   first, both parts always run, and the pass runs in the app anyway (§C.1). Recommended: yes.
3. **The renames, without aliases, in `v0.3.0`** (§B.4). Recommended: yes.
4. **The memory pass inside the app** (§C.1), amending maintenance-pass §B. Recommended: yes.
5. **Only ended episodes, now** (§C.1.1). Recommended: yes; the in-app pass is unsafe without it.
6. **A busy pass skips and reports** (§C.1.2). Recommended: yes.
