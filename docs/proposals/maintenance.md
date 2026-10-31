# Proposal: Maintenance — the Background Jobs an Agent Needs

**Status:** **DRAFT** 2026-10-31. Awaits a Decision Check Point (§G). §A's comparison comes from
[scope-lifespans](./scope-lifespans.md) §B and §E's first alternative from its §F, both decided
2026-10-30 (§H.1); the rest is new.
**Prompted by:** the owner (2026-10-31): thread expiry and the memory pass both need running in
the background, and later jobs will join them, so how a job is built and run is a topic of its
own, apart from what any one job does. How long each scope keeps what it holds stays in
scope-lifespans, which two of the jobs enforce.
**Jobs:** [thread-expiry](./thread-expiry.md) (Phase 4.12) and
[maintenance-pass](./maintenance-pass.md) (the memory pass, Phase 4.3). Drafts that would join:
[episode-retention](./episode-retention.md) (extends the memory pass) and
[procedure-extraction](./procedure-extraction.md) (a pass beside it).

## A. Problem

vsigma has two background jobs, built separately:

| | Thread expiry | Memory pass |
|---|---|---|
| Work | end idle sessions and forgotten runs; recover dead episodes | consolidate transcripts into knowledge |
| Cost | a scan and some deletes | LLM calls, batched |
| Cadence | minutes | daily |
| Runs | in the app's process | outside it, by cron |
| Entry point | `ThreadExpiry.run()` | `run_maintenance`, `vsigma memory consolidate` |
| Needed by | any app with persistent threads | apps with memory |

1. **No shared rules.** Each job decided its own shape. They converged (§B.1), and
   procedure-extraction already copies the memory pass, but nothing tells the next job what it
   must do.
2. **The memory pass is built to run outside the app.** maintenance-pass §B recorded that
   maintenance runs "without the agent — and without the agent's process". scope-lifespans §B
   and episode-retention have since mentioned a daemon's own timer, but nothing documents it or
   makes it safe (3 and 4). An app without cron, such as a daemon in a container, has to wrap the
   CLI in a scheduler of its own.
3. **The pass reads episodes that have not ended.** `unconsolidated_episodes`
   (`maintenance.py:81-117`) selects every episode transcript without a `deep:` marker, running
   and paused ones included. It marks them consolidated and never reads them again, so what they
   record afterwards is never learned. A nightly cron rarely meets a live turn; a pass inside the
   app meets them all the time. scope-lifespans §C.3 (verdict §H.7) already decided the fix, for a
   later step.
4. **One pass per store is not enforced.** scope-lifespans §B documents one maintainer per store.
   Two passes at once select the same episodes: twice the LLM cost, and near-duplicate knowledge,
   since two distills of one episode differ in wording and records are content-addressed.

## B. Design

### B.1 What every job does

The two built jobs already follow these rules; a new job must:

1. **Policy as data.** The app declares limits once (`ExpiryPolicy`; episode-retention's
   `RetentionPolicy`), and the job decides what is due.
2. **Safe to call repeatedly.** A call does what is due and returns, cheaply when nothing is.
   Jobs need no order between them: each reads what the others left (scope-lifespans §C).
3. **`dry_run` and a report.** `dry_run=True` reports what a call would do and writes nothing.
   Every call returns a report (`ExpiryReport`, `MaintenanceReport`).
4. **Crash-safe.** Intent is recorded before anything is deleted (the forget record, the
   tombstone), and progress advances only after success (markers advance after ingest), so the
   next call retries.
5. **Busy means skip.** A job that changes a thread holds its lock (thread-concurrency) and skips
   a busy one. A job over a store runs alone on it (§C.1) and skips if another is running. Either
   way the report says what was skipped.
6. **vsigma never schedules.** The app's timer or cron calls the job; vsigma starts no thread.

### B.2 Where a job runs

The locks a job needs decide it, not its cost:

- **A job that needs thread locks runs in the app's process.** The lock registry lives in that
  process and knows a checkpointer by object (`threads.py:41-42`). Thread expiry is one.
- **A job that needs only the store can run in the app or outside it.** Its safety lives in
  records that any process opening the database sees: statuses, markers, the bundle gate. The
  memory pass is one.

The boundaries of scope-lifespans §B hold for both: one process per checkpointer, one maintainer
per store.

### B.3 Names

`run_maintenance`, `MaintenanceReport`, the `maintenance/state` namespace and the module
`vsigma/maintenance.py` name the memory pass alone. They predate this topic, and renaming public
API would break callers.

## C. The memory pass, both ways

### C.1 Inside the app — first

The app calls `run_maintenance` from its own timer, off the request path, as it calls
`expiry.run()`:

```python
def memory_job(stop: threading.Event) -> None:
    while not stop.wait(6 * 3600):
        run_maintenance(store, distiller, sources=[persona("alice")], scope=persona("alice"),
                        constructor="app", out_dir="/srv/bundles")
```

1. **Only ended episodes.** Selection takes episodes with a final status (`done`, `failed`,
   `abandoned`), and episodes with no status record as today (legacy). The marker records the
   transcript's row count, and an episode counts as consolidated only while the two match. This
   is scope-lifespans §C.3 as decided (§H.7), built ahead of the `BaseStore` adapter: it needs
   only reads and a marker field. Pruning still waits for the adapter.
2. **One pass per store at a time.** A guard in the process, keyed by the store object as the
   thread lock is keyed by the checkpointer. A call while another runs returns at once with
   `MaintenanceReport.busy` set. The app passes the agents' own store object.
3. **Sharing the process.** `SQLiteStore` serializes its connection under a lock
   (`stores.py:21-25`), so a timer thread can share the agents' store. The pass's LLM calls do
   compete with live turns, most of all on one local GPU: run it off-peak, with a small `limit`,
   or give it its own LLM.
4. **maintenance-pass §B is amended:** the pass may run in the agent's process, on the app's
   timer. It still never runs inside a turn: the run-end hook is the reflexive round's place
   (architecture §9), and the deep round batches.

### C.2 Outside the app, coordinated — delayed

**Delayed by the owner (2026-10-31):** the in-app pass (§C.1) comes first, and this waits for a
later release. It would let an environment with a more capable model or agent run the pass from
cron: `vsigma memory consolidate --model …`, or an external agent through `vsigma memory export`
→ bundle → `vsigma memory ingest`, whose gate checks what it writes.

Its coordination with the app is the one-pass rule across processes: a lease record in the
store, with a holder and an expiry, taken atomically (SQLite `BEGIN IMMEDIATE`), renewed between
batches and released at the end, so a crashed holder blocks nothing for long. A pass that finds
it held skips. The store is already shared, so the CLI and the app need no channel between them.
The `MemoryStore` protocol has no atomic compare-and-set: `SQLiteStore` would provide one as a
backend extra, as it provides `namespaces()`, or the `BaseStore` adapter would.

Until then `vsigma memory consolidate` stays as built in Phase 4.3, under a documented boundary:
a store's pass runs in the app or from cron, never both. An app that moves the pass in removes
its cron entry.

## D. Build order

1. The memory pass inside the app (§C.1), in a compatible `v0.2.x` release.
2. The outside run under a store lease (§C.2), delayed until a consumer needs it.
3. New jobs as their proposals are accepted: episode-retention after the `BaseStore` adapter
   (scope-lifespans §E), procedure-extraction.

## E. Alternatives considered

- **One `maintain()` for every job.** Rejected in scope-lifespans §F and moved here: it would
  bring an LLM dependency to apps that need only expiry, let a failed consolidation hold up
  expiry, and the two jobs do not always run in the same process (§B.2). Maintenance is a set of
  rules, not a call.
- **A job registry or scheduler in vsigma.** Not until a third job shows duplication; the app's
  timer is a few lines.
- **A control channel between the CLI and the app** (socket, HTTP): ports and authentication for
  coordination the shared store already gives.

## F. Non-goals

- Scheduling: the app or cron owns time (moved from scope-lifespans §G).
- Running one checkpointer's or one store's jobs from several app processes.
- The external agent's own tooling, beyond export, bundles and ingest.

## G. Decision Check Point

1. **Maintenance as its own topic**, with the job rules (§B.1) and where a job runs (§B.2).
   Recommended: yes.
2. **The memory pass inside the app** (§C.1), amending maintenance-pass §B. Recommended: yes, the
   owner's priority (2026-10-31).
3. **Only ended episodes, now** (§C.1.1): scope-lifespans §C.3, decided as §H.7, moved ahead of
   the adapter. Recommended: yes; the in-app pass is unsafe without it.
4. **A busy pass skips and reports** (`MaintenanceReport.busy`) rather than raising, as expiry
   skips a busy thread. Recommended: yes.
5. **The outside run and the store lease** (§C.2): delayed by the owner (2026-10-31).

## H. Evidence

- `unconsolidated_episodes` (`maintenance.py:81-117`) selects any `episode:<id>/context`
  namespace without a `deep:` marker; nothing in the pass reads `episodes/status`.
- The `deep:` marker holds `at`, `constructor` and `bundle` (`maintenance.py:180-184`): no row
  count.
- `SQLiteStore` opens with `check_same_thread=False` and takes a lock on every call
  (`stores.py:21-25`).
- The thread lock registry is per process, weakly keyed by checkpointer (`threads.py:41-42`).
- maintenance-pass §B: "maintenance runs *without the agent* — and without the agent's process".
- procedure-extraction: "The pass lives in maintenance, next to `run_maintenance` … Mechanics
  mirror `run_maintenance` exactly".
