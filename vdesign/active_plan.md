# Active Plan — Phase 4.13: Store Adapter

**Target:** Implement [`docs/proposals/store-adapter.md`](../docs/proposals/store-adapter.md) (ACCEPTED as Phase 4.13, target release `v0.3.0`).
**Scope:** `SQLiteStore` becomes a thin adapter over LangGraph's `SqliteStore` implementing `MaintainableStore` with delete, fast namespace enumeration via persistent namespace records, and stored value byte tracking. Existing store files are not migrated and raise on open. `InMemoryStore` implements `MaintainableStore`. `langgraph-checkpoint-sqlite` moves to required dependencies.

---

## Invariants & Design Contracts

1. **Public API & Interchangeability:** `SQLiteStore(path: str | Path)` retains its signature, module location (`src/vsigma/stores.py`), and public interface (`write`, `read`, `query`, `namespaces`, `close`). It implements both `MemoryStore` and `MaintainableStore`.
2. **Namespace & Key Encoding:** LangGraph store namespace labels cannot contain `.` while vsigma user IDs can. The adapter encodes namespaces as `("m", namespace.replace("%", "%25").replace(".", "%2E"))` and decodes as `labels[1].replace("%2E", ".").replace("%25", "%")`.
3. **Value Wrapping & Monotonic Sequence:** Values are wrapped as `{"s": sequence, "v": payload}`. `sequence` is a monotonic time-ordered integer `max(self._last + 1, time.time_ns())`. Rewrites of existing keys preserve their original `sequence`, ensuring `query` preserves first-write order across updates.
4. **Namespace Record (`_meta`/`_ns`):** The adapter keeps a metadata record per namespace: `{"first": sequence, "rows": n, "bytes": n}` stored under a dedicated meta namespace `("n",)`.
   - Every write or delete updates the data row and the namespace record in the same `batch()`, executing atomically in one SQLite transaction.
   - When `rows == 0`, the namespace record is deleted so `namespaces()` only enumerates non-empty namespaces.
   - `namespaces()` reads namespace records and sorts by `record["first"]` (first-write order) without scanning data rows.
   - `namespace_sizes()` returns `{ns: record["bytes"]}` directly from the records without SQL over LangGraph's private tables.
5. **Stored Value Byte Accounting:** `bytes` counts UTF-8 bytes of serialized JSON payloads (`len(json.dumps(value, default=str).encode("utf-8"))`). Recount matches incremental tally across writes, rewrites, and deletes.
6. **`MaintainableStore` Protocol & `InMemoryStore`:** Defined in `src/vsigma/memory.py`:
   ```python
   class MaintainableStore(MemoryStore, Protocol):
       def namespaces(self) -> list[str]: ...                  # first-write order
       def namespace_sizes(self) -> dict[str, int]: ...        # stored value bytes
       def delete(self, namespace: str, keys: list[str]) -> None: ...   # one transaction
   ```
   `InMemoryStore` implements `MaintainableStore` with zero external dependencies, tracking insertion order and exact value byte counts.
7. **Fail-Loud Old-File Rejection:** Opening an existing database containing a `memory` table with rows from pre-v0.3.0 vsigma raises `RuntimeError` immediately, naming the file and telling the caller to use a new path. The file is left completely untouched.
8. **Dependency Promotion:** `langgraph-checkpoint-sqlite` moves from `dev` to `dependencies` in `pyproject.toml`. Optional import fallbacks for `SqliteSaver` in `src/vsigma/expiry.py` and `src/vsigma/retention.py` are removed.
9. **Atomic Batch Rollback:** Transactions execute with `isolation_level=None` under `SqliteStore.batch`. Failures inside a batch roll back all changes (both data items and namespace records).

---

## Step-by-Step Execution Plan

### Step 1 — `SQLiteStore` Adapter Core & `delete` (`src/vsigma/stores.py`) `[ ]`
- Namespace encoding helpers:
  - `_encode(namespace: str) -> tuple[str, str]`: `("m", namespace.replace("%", "%25").replace(".", "%2E"))`
  - `_decode(labels: tuple[str, ...]) -> str`: `labels[1].replace("%2E", ".").replace("%25", "%")`
- `SQLiteStore` implementation:
  - `__init__(path: str | Path)`:
    - Set up `sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)`.
    - Enable `PRAGMA journal_mode=WAL;`.
    - Instantiate `langgraph.store.sqlite.SqliteStore(self._conn)`.
    - Initialize sequence generator state (`self._last = 0`).
  - Sequence generation:
    - `_sequence() -> int`: `max(self._last + 1, time.time_ns())`.
  - Operations:
    - `write(namespace: str, key: str, value: object) -> None`:
      - Payload serialization: `payload = json.loads(json.dumps(value, default=str))`.
      - Inspect existing item to preserve `sequence` on rewrite.
      - Put wrapped `{"s": sequence, "v": payload}` with `index=False`.
    - `read(namespace: str, key: str) -> object | None`:
      - Retrieve and return `item.value["v"]`.
    - `query(namespace: str) -> list[tuple[str, object]]`:
      - Paged search over exact namespace (filter `tuple(item.namespace) == labels`), page size 1000.
      - Sort items by `item.value["s"]`.
      - Return `[(item.key, item.value["v"]) for item in items]`.
    - `delete(namespace: str, keys: list[str]) -> None`:
      - Delete items via `PutOp(labels, key, None)` in `self._store.batch(...)`.
    - `close() -> None`:
      - Close connection.
- Verify basic operations and run test suite (`uv run pytest`).

### Step 2 — Namespace Record, `namespace_sizes`, and `MaintainableStore` (`src/vsigma/memory.py`, `src/vsigma/stores.py`) `[ ]`
- In `src/vsigma/memory.py`:
  - Define `MaintainableStore(MemoryStore, Protocol)`:
    - `def namespaces(self) -> list[str]: ...`
    - `def namespace_sizes(self) -> dict[str, int]: ...`
    - `def delete(self, namespace: str, keys: list[str]) -> None: ...`
  - Update `InMemoryStore` to implement `MaintainableStore`:
    - Track namespace creation order.
    - Implement `delete(namespace: str, keys: list[str]) -> None`: remove keys, delete namespace if empty.
    - Implement `namespaces() -> list[str]`: return non-empty namespaces in creation order.
    - Implement `namespace_sizes() -> dict[str, int]`: sum serialized JSON UTF-8 byte lengths per namespace.
- In `src/vsigma/stores.py`:
  - Use meta namespace `("n",)` with key `namespace`:
    - Value schema: `{"first": sequence, "rows": int, "bytes": int}`.
  - Update `write(namespace, key, value)`:
    - Compute `val_bytes = len(json.dumps(payload).encode("utf-8"))`.
    - Fetch existing data item (if any) and existing namespace meta record.
    - Determine byte delta and row count delta.
    - Put data item and updated meta record in a single `self._store.batch([...])`.
  - Update `delete(namespace, keys)`:
    - Fetch existing items for `keys` to calculate total byte and row decrement.
    - If `remaining_rows == 0`: issue `PutOp(("n",), namespace, None)` (remove meta record).
    - Else: update meta record with decremented `rows` and `bytes`.
    - Issue all deletes and meta update in a single `self._store.batch([...])`.
  - Implement `namespaces() -> list[str]`:
    - Paged search over meta namespace `("n",)`.
    - Sort meta records by `record["first"]`.
    - Return sorted namespace names.
  - Implement `namespace_sizes() -> dict[str, int]`:
    - Paged search over meta namespace `("n",)`.
    - Return `{item.key: item.value["bytes"] for item in items}`.

### Step 3 — Old-File Rejection & Required Dependency Transition `[ ]`
- In `src/vsigma/stores.py`:
  - In `SQLiteStore.__init__`:
    - If `Path(self.path).exists()` and has non-zero size, open a temporary connection (or inspect before `setup()`).
    - Query `SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory'`.
    - If table exists, check `SELECT count(*) FROM memory`.
    - If `count > 0`: raise `RuntimeError(f"Store file '{self.path}' contains pre-v0.3.0 data in table 'memory'. vsigma does not migrate existing stores; please specify a new path. The file was left untouched.")` without running `_store.setup()`.
- In `pyproject.toml` and `uv.lock`:
  - Move `langgraph-checkpoint-sqlite` from `dev` dependency group to main `dependencies`.
  - Re-lock with `uv lock`.
- In `src/vsigma/expiry.py` and `src/vsigma/retention.py`:
  - Remove optional `_SqliteSaver` try/except imports (now unconditionally imported from `langgraph.checkpoint.sqlite`).
  - Preserve `DeltaChannel` compatibility guard.
- Documentation updates:
  - `docs/upgrading.md`: breaking changes in `v0.3.0` (required `langgraph-checkpoint-sqlite`, store file format change without migration, old-file error, `MaintainableStore`).
  - `docs/architecture_design.md`: update §§1, 8, 9 references to stdlib SQLite.

### Step 4 — Verification, Rollback Testing, and Completion `[ ]`
- Create `tests/test_store_adapter.py`:
  - Test namespace encoding/decoding with dots, percent signs, and user IDs.
  - Verify first-write order preservation in `query()` after rewrites.
  - Verify `namespaces()` first-write order and omission of empty namespaces.
  - Verify `namespace_sizes()` matches exact recount across writes, rewrites, and deletes.
  - Verify `delete()` in one transaction across multiple keys.
  - Verify `MaintainableStore` protocol conformance for both `SQLiteStore` and `InMemoryStore`.
  - Verify old store file check: raises `RuntimeError` and leaves file contents byte-identical.
  - **Batch rollback tests:**
    - Fault injection: inject exception during `SqliteStore.batch` (e.g. invalid operation or database constraint/interrupt), verify neither data row nor namespace record changes.
    - Subprocess / process kill test: verify atomic transaction integrity.
- Full verification gate:
  - `uv lock --locked`
  - `uv run ruff check`
  - `uv run mypy`
  - `uv run pytest` (284 existing + new store adapter tests passing)
- Create local tag `phase-4.13`.

---

## Done Criteria Checklist (from `store-adapter.md` §C)

- [ ] The suite passes on the adapter; `query` keeps first-write order after a rewrite; a user id with a dot round-trips.
- [ ] `namespace_sizes` equals a recount after writes, rewrites and deletes.
- [ ] A failure inside a batch leaves the row and its record unchanged, tested by fault injection and by killing a process mid-write.
- [ ] An old file raises and is unchanged afterwards.
