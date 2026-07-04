# Active Plan — Phase 4: Real Memory & Context

> JIT breakdown for the phase in flight. Durable roadmap: [`plan.md`](./plan.md).
> Design refs: [memory subsystem](../docs/proposals/memory-construction.md) (§C–§E),
> [running-context model](../docs/proposals/running-context-model.md).
> Started 2026-07-04 from tag `pre-phase-4`.

**Ground rules:** every slice lands green (`make check`), offline-testable (FakeLLM), no new
runtime deps (SQLite = stdlib). Schema-second: the bundle schema is *extracted from* slice 3,
not written first.

## Slice 1 — Scope ladder in code `[ ]`
- `scope.py`: `Scope` (global/agent/persona/session/episode/step) → namespace
  `"level:id/kind"`; `Kind = knowledge | context`
- `user_id` (persona) on `BaseState`; `Agent.run(..., user_id=)`
- Seam writes → `episode:<run_id>/context` (mechanical trajectory capture = retention)
- Visibility on the retrieve path: `recall(memory, state)` queries only visible scopes;
  **test:** persona-private record never served to another persona

## Slice 2 — Records + SQLite store `[ ]`
- `MemoryRecord` + `Provenance` (origin, consolidator, source_ref, confidence, version),
  dict round-trip (JSONL-ready)
- `SQLiteStore` (stdlib `sqlite3`) behind the unchanged `MemoryStore` protocol;
  **test:** round-trip + persistence across reconnect

## Slice 3 — Construction engine + bundle contract `[ ]`
- `construction.py`: `distill(sources, llm, …) -> list[MemoryRecord]` — LLM fills the record
  schema (constrained), provenance mandatory; offline via FakeLLM
- `bundle.py`: `write_bundle` / `validate_bundle` / `ingest_bundle`
  (manifest.json + records.jsonl; deterministic stdlib validation; agent proposes → gate commits)
- CLI: `vsigma memory validate|ingest` (argparse, console script)
- **Extract** the schema → `docs/schemas/memory-bundle.schema.json` (v0, versioned)
- **test:** end-to-end docs → distill → bundle → validate → ingest → recall reads it back

## Slice 4 — Two-round consolidation + context assembly `[ ]`
- Reflexive: `reflexive_consolidator(llm=None)` → run-end hook on `Services`
  (bounded: ≤1 LLM call; llm=None degrades to mechanical episode summary); writes
  provisional records (origin=consolidated, consolidator=reflexive, low confidence)
- Deep: same `distill` engine fed trajectories (consolidator=deep), emits a bundle;
  **test:** same engine both rounds; deep supersedes a round-1 record
- Context: `SlidingWindowContext` (real compaction in the existing seam slot)

## Deferred out of Phase 4 (recorded, not lost)
- Semantic index / embeddings + external vector adapters (opt-in extra)
- Strategy library beyond the basics (weighted retrieval, forgetting, promotion policies)
- Shape variety (v0 ships kv-fact records only); knowledge/context API handles (namespaces suffice)
- `vsigma memory build` CLI for full doc-bootstrap orchestration (library path exists)

## Wrap-up `[ ]`
- Public API exports, `getting_started.md` memory section, plan.md checkboxes,
  checkpoint, proposal status notes → tag `phase-4`
