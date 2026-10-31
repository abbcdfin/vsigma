# Active Plan — Phase 4.13: Store Adapter

**Target:** Implement [`docs/proposals/store-adapter.md`](../docs/proposals/store-adapter.md) (ACCEPTED as Phase 4.13, target release `v0.3.0`), incorporating all Architect review requirements.
**Scope:** `SQLiteStore` is renamed to `PersistentStore` with no alias. `PersistentStore` adapts LangGraph's `SqliteStore` via private subclass `_SqliteStore` that overrides `_cursor` to roll back on exceptions. It implements `MaintainableStore` with delete, atomic namespace record management, and byte accounting. Old stores fail loudly via read-only check. `InMemoryStore` implements `MaintainableStore`. `langgraph-checkpoint-sqlite` promoted to required dependency.

---

## Invariants & Design Contracts

1. **Public API & Rename (`PersistentStore`):** `PersistentStore(path: str | Path)` replaces `SQLiteStore` in `src/vsigma/stores.py` with no compatibility alias. Exposes `write`, `read`, `query`, `namespaces`, `close`, `delete`, and `namespace_sizes`. Implements both `MemoryStore` and `MaintainableStore`.
2. **Namespace & Key Encoding:** LangGraph store namespace labels cannot contain `.`; vsigma user IDs can. The adapter encodes namespaces as `("m", namespace.replace("%", "%25").replace(".", "%2E"))` and decodes as `labels[1].replace("%2E", ".").replace("%25", "%")`.
3. **Value Wrapping & JSON Text Encoding:** Values are stored wrapped as `{"s": sequence, "j": text}`:
   - Serialized via `json.dumps(value, default=str, ensure_ascii=False)`.
   - If UTF-8 encoding raises `UnicodeEncodeError` (e.g. lone surrogates), falls back to `json.dumps(value, default=str, ensure_ascii=True)`.
   - Integers over 64 bits, NaNs, lone surrogates, and datetimes serialize and round-trip faithfully as today.
   - `sequence` is a monotonic time-ordered integer `max(self._last + 1, time.time_ns())`. Rewrites of existing keys preserve their original `sequence`, ensuring `query` preserves first-write order across updates.
4. **Adapter Lock & Atomic Namespace Record (`("n",)`):**
   - An adapter lock (`threading.Lock`) protects every read-then-batch operation, `namespaces()`, and `namespace_sizes()`, preventing lost updates across concurrent threads.
   - Metadata record per namespace: `{"first": sequence, "rows": n, "bytes": n}` stored under meta namespace `("n",)`.
   - Built directly in Step 1 so `namespaces()` is available immediately for `cli.py` and `maintenance.py`.
   - Updated in the same `_store.batch()` as row mutations.
   - Deleted when `rows == 0`, so empty namespaces drop from enumeration.
   - `namespaces()` reads namespace records and sorts by `first` without scanning data rows.
   - `namespace_sizes()` returns `{ns: record["bytes"]}` directly from meta records.
5. **Stored Value Byte Accounting:** `bytes` counts `len(text.encode("utf-8"))` of the stored JSON text. `delete` deduplicates keys and ignores missing keys when calculating row and byte decrements. Recount matches incremental totals across concurrent writes, rewrites, and deletes.
6. **Cursor Rollback Subclass (`_SqliteStore`):**
   - In LangGraph's `SqliteStore`, `_cursor` executes `COMMIT` inside `finally:`, committing earlier statements in a batch even if subsequent statements fail.
   - A private subclass `_SqliteStore(SqliteStore)` overrides `_cursor(transaction=True)` to execute `ROLLBACK` in `except BaseException:`, re-raising the error, and `COMMIT` only in `else:`.
7. **Read-Only Old-File Rejection:**
   - Before setting WAL mode or calling `_store.setup()`, if the file exists and is non-empty, inspect it using a read-only URI connection: `sqlite3.connect(f"file:{resolved_path}?mode=ro", uri=True)`.
   - If table `memory` exists and has rows (`SELECT 1 FROM memory LIMIT 1`), raise `RuntimeError(f"Store file '{self.path}' contains pre-v0.3.0 data in table 'memory'. vsigma does not migrate existing stores; please specify a new path. The file was left untouched.")`.
   - Closing a `mode=ro` connection avoids checkpointing uncheckpointed WAL frames, leaving the file byte-identical.
8. **`MaintainableStore` Protocol & `InMemoryStore`:**
   - Defined in `src/vsigma/memory.py` and exported from `src/vsigma/__init__.py`:
     ```python
     class MaintainableStore(MemoryStore, Protocol):
         def namespaces(self) -> list[str]: ...                  # first-write order
         def namespace_sizes(self) -> dict[str, int]: ...        # stored value bytes
         def delete(self, namespace: str, keys: list[str]) -> None: ...   # one transaction
     ```
   - `InMemoryStore` implements `MaintainableStore` using existing dict insertion order (no extra tracking).
   - In `InMemoryStore.namespace_sizes()`, snapshot namespaces (`list(self._data.items())`) to safely iterate beside concurrent live turns, and calculate bytes using the same UTF-8 / ASCII fallback JSON encoding.
9. **Dependency Promotion:** `langgraph-checkpoint-sqlite` moves to required dependencies in `pyproject.toml`. Optional import fallbacks for `SqliteSaver` in `src/vsigma/expiry.py` and `src/vsigma/retention.py` are removed.

---

## Step-by-Step Execution Plan

### Step 1 — Adapter Core, `_SqliteStore` Rollback, Namespace Record, and `delete` (`src/vsigma/stores.py`) `[ ]`
- Implement `_SqliteStore(SqliteStore)`:
  - Override `_cursor(transaction=True)`: execute `BEGIN`, yield cursor, `ROLLBACK` on exception, `COMMIT` on success.
- Implement namespace encoding helpers:
  - `_encode(namespace: str) -> tuple[str, str]`: `("m", namespace.replace("%", "%25").replace(".", "%2E"))`
  - `_decode(labels: tuple[str, ...]) -> str`: `labels[1].replace("%2E", ".").replace("%25", "%")`
- Implement `_dump_json(value: object) -> tuple[str, int]`:
  - Try `json.dumps(value, default=str, ensure_ascii=False)`. If `encode("utf-8")` raises `UnicodeEncodeError`, fallback to `json.dumps(value, default=str, ensure_ascii=True)`. Return `(text, len(encoded_bytes))`.
- Implement `PersistentStore(path: str | Path)`:
  - Adapter lock: `self._lock = threading.Lock()`.
  - Sequence generation: `_sequence() -> int`: `max(self._last + 1, time.time_ns())`.
  - Connection init: `check_same_thread=False, isolation_level=None`.
  - Operations:
    - `write(namespace: str, key: str, value: object) -> None`:
      - With `self._lock`: format JSON text with `_dump_json(value)`; inspect existing item to preserve `sequence` on rewrite; compute byte/row deltas against existing namespace record in `("n",)`; submit data `PutOp` and meta `PutOp` in `_store.batch(...)`.
    - `read(namespace: str, key: str) -> object | None`:
      - With `self._lock`: retrieve item, unwrap `json.loads(item.value["j"])`.
    - `query(namespace: str) -> list[tuple[str, object]]`:
      - With `self._lock`: paged search over exact namespace (filter `tuple(item.namespace) == labels`), page size 1000; sort by `item.value["s"]`; return `[(item.key, json.loads(item.value["j"]))]`.
    - `delete(namespace: str, keys: list[str]) -> None`:
      - With `self._lock`: deduplicate keys (`set(keys)`); fetch existing items; calculate decrements ignoring missing keys; submit row `PutOp(..., None)` and updated/deleted meta record in `_store.batch(...)`.
    - `namespaces() -> list[str]`:
      - With `self._lock`: paged search over `("n",)`; sort by `record["first"]`; return decoded namespace names.
    - `namespace_sizes() -> dict[str, int]`:
      - With `self._lock`: paged search over `("n",)`; return `{_decode(item.key): item.value["bytes"]}`.
    - `close() -> None`: close connection.
- Rename `SQLiteStore` to `PersistentStore` in `src/vsigma/stores.py` and `src/vsigma/__init__.py`. Update existing internal references in `src/vsigma/cli.py`, `src/vsigma/maintenance.py`, and test suite.
- Verify test suite passes on the adapter.

### Step 2 — `MaintainableStore` Protocol & `InMemoryStore` (`src/vsigma/memory.py`, `src/vsigma/__init__.py`) `[ ]`
- In `src/vsigma/memory.py`:
  - Define `MaintainableStore(MemoryStore, Protocol)`:
    - `def namespaces(self) -> list[str]: ...`
    - `def namespace_sizes(self) -> dict[str, int]: ...`
    - `def delete(self, namespace: str, keys: list[str]) -> None: ...`
  - Update `InMemoryStore` to implement `MaintainableStore`:
    - `namespaces() -> list[str]`: return `list(self._data.keys())` (insertion order preserved).
    - `delete(namespace: str, keys: list[str]) -> None`: deduplicate keys, remove existing keys, delete namespace if empty.
    - `namespace_sizes() -> dict[str, int]`: snapshot items (`list(self._data.items())`); calculate value bytes using `_dump_json` logic.
- Export `MaintainableStore` from `src/vsigma/__init__.py`.
- Verify type checks and test suite.

### Step 3 — Read-Only Old-File Check, Dependency Promotion, and Documentation Updates `[ ]`
- In `src/vsigma/stores.py` `PersistentStore.__init__`:
  - If `Path(self.path).is_file()` and has size > 0:
    - Open with URI `f"file:{Path(self.path).resolve().as_posix()}?mode=ro"` with `uri=True`.
    - Query `SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory'`.
    - If table exists, check `SELECT 1 FROM memory LIMIT 1`.
    - If row exists, close and raise `RuntimeError(f"Store file '{self.path}' contains pre-v0.3.0 data in table 'memory'. vsigma does not migrate existing stores; please specify a new path. The file was left untouched.")`.
    - Set WAL pragma and call `_store.setup()` only after check passes.
- In `pyproject.toml` and `uv.lock`:
  - Move `langgraph-checkpoint-sqlite` from `dev` dependency group to main `dependencies`.
  - Re-lock with `uv lock`.
- In `src/vsigma/expiry.py` and `src/vsigma/retention.py`:
  - Unconditionally import `SqliteSaver` from `langgraph.checkpoint.sqlite`.
  - Preserve `DeltaChannel` compatibility guard.
- Documentation updates:
  - `docs/upgrading.md`: document `v0.3.0` breaking changes: `SQLiteStore` renamed to `PersistentStore` with no alias, required `langgraph-checkpoint-sqlite`, no store migration, old store raises `RuntimeError`, `MaintainableStore`.
  - `docs/getting_started.md`: update line 396 and all other references from `SQLiteStore` / "stdlib-only" to `PersistentStore`.
  - `docs/architecture_design.md`: update §§1, 8, 9 (replacing stdlib SQLiteStore references with PersistentStore adapter over LangGraph's SqliteStore).

### Step 4 — Verification, Rollback Testing, and Completion (`tests/test_store_adapter.py`) `[ ]`
- Create `tests/test_store_adapter.py`:
  - Namespace encoding/decoding tests: dots, percent signs, and user IDs round-trip.
  - Value fidelity tests: lone surrogates (`"\ud800"`), integers > 64 bits (`2**100`), `float("nan")`, and `datetime` round-trip faithfully; agent turn with surrogate in reply finishes.
  - First-write order in `query()` preserved across key updates.
  - `namespaces()` order and empty namespace omission.
  - `delete()` ignores duplicate and missing keys, updates counts correctly.
  - Multi-threaded concurrency test: concurrent writes/deletes verify `namespace_sizes` matches exact recount.
  - Old-file test: create an uncheckpointed WAL database fixture (via child process exiting with `os._exit(0)`), verify opening with `PersistentStore` raises `RuntimeError` and file (and WAL) remains byte-for-byte identical.
  - Fault-injection batch rollback tests with SQL triggers:
    1. Delete plus record update: trigger on `items` fails during record update, verify deleted items are rolled back.
    2. Delete emptying namespace: trigger on `items` fails during record deletion, verify deleted items are rolled back.
  - Subprocess kill test mid-write verifying transaction consistency.
- Full verification gate:
  - `uv lock --locked`
  - `uv run ruff check`
  - `uv run mypy`
  - `uv run pytest` (284 existing + new store adapter tests passing)
- Create local tag `phase-4.13`.

---

## Done Criteria Checklist (from `store-adapter.md` §C)

- [ ] The suite passes on the adapter; `query` keeps first-write order after a rewrite; a user id with a dot round-trips.
- [ ] A lone surrogate, an integer over 64 bits, NaN and a `datetime` read back as they do from today's store, and a turn whose reply holds a lone surrogate finishes.
- [ ] `namespace_sizes` equals a recount after writes, rewrites and deletes, including from several threads at once.
- [ ] A failure inside a batch leaves the row and its record unchanged, tested by fault injection on a delete with its record update and on a delete emptying a namespace, and by killing a process mid-write.
- [ ] An old WAL file with frames not yet checkpointed raises and is byte-identical afterwards.
