# Proposal: Store Adapter — LangGraph's Store Behind `MemoryStore`

**Status:** **ACCEPTED** 2026-10-31. Waits: all, to be built as Phase 4.13 (`v0.3.0`).
**Implements:** thread-native-runtime §G.7 and [scope-lifespans](./scope-lifespans.md) §D.
**Unblocks:** [episode-retention](./episode-retention.md) and forgetting session memory
(scope-lifespans §C.2), which both need a store that can delete.

## A. Problem

1. **Memory cannot be deleted.** `MemoryStore` is `write`/`read`/`query` (`memory.py:13-25`), and
   `SQLiteStore` adds only `namespaces()` (`stores.py:59`).
2. **vsigma owns a storage engine LangGraph already ships.** `SqliteStore` in
   `langgraph-checkpoint-sqlite` has delete, TTL, namespace listing and a vector index, behind a
   `BaseStore` interface other backends also implement. That goes against the thesis of
   thread-native-runtime: store nothing LangGraph already stores.
3. **This is the cheapest time to switch.** Neither consumer has left the `v0.1.0` pin and vsigma
   is not on PyPI. Once `v0.3.0` stores exist, a later switch needs a migration.

## B. Design

### B.1 The shape

`SQLiteStore(path)` becomes `PersistentStore(path)`, a thin adapter over LangGraph's `SqliteStore`,
with the same API and module, and no alias. The name says what an app gets and does not name a
backend, since `BaseStore` also has database backends; it also ends the clash with LangGraph's
`SqliteStore`. vsigma's stdlib engine is removed. `scope.py`, records, consolidation and the bundle
gate do not change. The adapter class stays private until a consumer needs a second backend.

A throwaway prototype of about 90 lines passed all 284 tests in place of `SQLiteStore`
(2026-10-31). Those tests do not cover deleting or an old file (§D).

### B.2 What the adapter must supply

| `SqliteStore` | Adapter |
|---|---|
| Namespace labels cannot contain `.`, and user ids can | Encodes `%` and `.` in the label |
| Values must be objects, and `orjson` rejects lone surrogates and integers over 64 bits and turns NaN into null | Stores each value as JSON text, `{"s": sequence, "j": text}`, so everything `json.dumps(value, default=str)` accepts today round-trips the same |
| `search` orders by last update, with one-second timestamps | `query` sorts by the sequence, which a rewrite keeps, so the protocol's first-write order holds |
| `search` and `list_namespaces` return a page | Reads every page |
| A prefix search also returns deeper namespaces | Filters to the exact namespace |
| `batch` commits what ran before a failing statement: `_cursor` runs `COMMIT` in a `finally` (`langgraph/store/sqlite/base.py:1053-1057`, version 3.1.1) | A small subclass overrides `_cursor` to roll back on an exception; a test pins it |

- **The sequence** is a time-ordered id assigned at write, never read from the current maximum, which
  two writers could read at once.
- **The text** is UTF-8 JSON (`ensure_ascii=False`), about half the size of today's escaped form for
  Chinese. Text that cannot encode, such as a lone surrogate, uses the ASCII form.
- **One lock** covers each read-then-batch, as `SQLiteStore` holds one today. `SqliteStore`'s own lock
  covers a single batch only, which loses count: 8 threads writing 1,600 rows left a record saying 499.

### B.3 The namespace record

Listing namespaces in first-write order by scanning rows took 15 ms at 500 episodes in the
prototype, against 0.3 ms today. That grows with the store and holds its lock, blocking live turns.
Memory-pass selection and the retention budget both need listing, so the adapter keeps one record
per namespace: `{"first": sequence, "rows": n, "bytes": n}`.

- **Atomic with its row:** each write or delete updates the record in the same `batch`, one
  transaction once `_cursor` rolls back (§B.2). Without that, a delete whose record update fails
  leaves the rows gone and the count stale for good.
- **Removed at zero rows,** so `namespaces()` lists only namespaces with rows.
- **No reads of LangGraph's tables:** sizes come from the record, not from SQL over its private
  schema.

### B.4 The protocols

`MemoryStore` does not change, so custom stores keep working. A second protocol names what
maintenance needs:

```python
class MaintainableStore(MemoryStore, Protocol):
    def namespaces(self) -> list[str]: ...                  # first-write order
    def namespace_sizes(self) -> dict[str, int]: ...        # stored value bytes
    def delete(self, namespace: str, keys: list[str]) -> None: ...   # one transaction
```

`PersistentStore` and `InMemoryStore` implement it. `InMemoryStore` stays free of any dependency.

- **Sizes count each value's UTF-8 JSON text, not the file.** SQLite reuses freed pages, so pruning
  stops a file growing but does not shrink it. `VACUUM` is the operator's.
- **Speed and size** in the prototype, over 2,000 rows: a write takes 33 µs against 14 µs, a query
  205 µs against 60 µs, and the file is 23% larger. Neither matters beside an LLM call.

### B.5 No migration

Existing store files are not migrated, and their memory does not carry over.

- **An old file fails loudly.** Opening a file with rows in the old `memory` table raises, naming
  the file and telling the app to use a new path, so a deployment cannot start with an empty memory
  by accident. The file is left untouched, and `v0.2.3`'s `vsigma memory export` can still read it.
  The check comes first, on a read-only connection (`mode=ro`): old stores are WAL, and closing an
  ordinary connection checkpoints them, which changes the file.
- **Records need no legacy handling.** Every new store's markers carry a row count, and every
  episode an `Agent` runs has a status.
- **`upgrading.md` says this plainly,** with the rename, for the web app and bumblebee.

### B.6 The dependency

`langgraph-checkpoint-sqlite` moves from a dev dependency to a required one, which brings
`sqlite-vec` and `aiosqlite`. There is no extra: every install gets the same store. On a platform
with no `sqlite-vec` build, vsigma does not install or import, which is accepted. The optional
imports of `SqliteSaver` in `expiry.py:27` and `retention.py:32` go.

## C. Build order

One phase, released in `v0.3.0` ahead of [maintenance](./maintenance.md):

1. The adapter with §B.2 and `delete`, with the suite green on it.
2. The namespace record and `namespace_sizes`, and `MaintainableStore` on both stores.
3. The old-file check, the dependency, the rename, and the docs: `upgrading.md`, `getting_started.md`,
   and architecture §§8–9, which describe the store as stdlib.

Done means:

- The suite passes on the adapter; `query` keeps first-write order after a rewrite; a user id with a
  dot round-trips.
- A lone surrogate, an integer over 64 bits, NaN and a `datetime` read back as they do from today's
  store, and a turn whose reply holds a lone surrogate finishes.
- `namespace_sizes` equals a recount after writes, rewrites and deletes, including from several threads
  at once.
- A failure inside a batch leaves the row and its record unchanged, tested by fault injection on a
  delete with its record update and on a delete emptying a namespace, and by killing a process
  mid-write.
- An old WAL file with frames not yet checkpointed raises and is byte-identical afterwards.

## D. Alternatives considered

- **Add `SQLiteStore.delete` to the stdlib store.** A few lines now, but stores keep filling in a
  format the adapter would later have to migrate.
- **Migrate existing files.** One-way, crash-sensitive work for data the owner chose to drop.
- **An optional `vsigma[sqlite]` extra.** Two kinds of install to support, for platforms no consumer
  uses.
- **Keep the name `SQLiteStore`.** It differs from LangGraph's `SqliteStore` only in capitalization,
  and names a backend that could change.
- **`FileStore`.** It would be wrong once a database backend exists.
- **Replace `MemoryStore` with `BaseStore`.** Every memory module changes, with no gain over the
  adapter.
- **Add `delete` to `MemoryStore`.** That breaks every custom store.
- **Read sizes with SQL over LangGraph's table.** A full read on each call, tied to a private schema.
- **Recount a namespace after a failed batch,** instead of overriding `_cursor`. The rows are already
  gone, and whatever failed the batch can fail the recount.
- **Store values as objects (`{"s", "v"}`).** A reply with a lone surrogate would raise in capture and
  end the run, where today's store accepts it.

## E. Non-goals

- The vector index (Phase 4.5), TTL, and backends other than SQLite: this makes them possible, and
  none has a consumer yet. A database backend would add a way to construct `PersistentStore`, beside
  the path, plus its own failure handling in place of the `_cursor` override.
- One database file for the checkpointer and the store.
- Several processes writing one store: the boundaries of scope-lifespans §B hold.

## F. Verdicts (decided 2026-10-31)

1. **Build the adapter now, ahead of maintenance, for `v0.3.0`** — **decided 2026-10-31: yes.**
2. **No data migration** (§B.5) — **decided 2026-10-31: yes;** an old file raises.
3. **`langgraph-checkpoint-sqlite` is required, with no extra** (§B.6) — **decided 2026-10-31: yes.**
4. **`SQLiteStore` is renamed `PersistentStore`, with no alias, and the adapter class stays private**
   (§B.1). **decided 2026-10-31: yes.**
5. **`MemoryStore` stays as it is, with `MaintainableStore` beside it** (§B.4). **decided 2026-10-31: yes.**
6. **The namespace record** (§B.3). **decided 2026-10-31: yes.**
