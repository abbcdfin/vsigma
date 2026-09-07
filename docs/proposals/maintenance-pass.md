# Proposal: The Maintenance Pass — a Runnable Deep Round

**Status:** PROPOSED 2026-07-15
**Prompted by:** studying nanobot's memory system (its cron-scheduled "Dream" pass with a
consumption cursor) against our own two-round consolidation design (§9 of the architecture).

## A. Problem

vsigma's deep round exists as a function (`deep_consolidate`) and a contract (the memory
bundle + ingest gate), but **nothing runs it and nothing remembers what it has processed**:

- There is no way to ask a store "which episodes has the deep pass not seen yet?" — an
  operator re-running consolidation would re-distill every episode, every time.
- Wiring trajectories → distill → bundle → validate → ingest requires custom Python; the
  promised "external constructor in any language" path works, but the *common* case (an
  operator with a store, an LLM endpoint, and cron) has no entry point.
- There is no read-side command at all: inspecting memory means raw `sqlite3`.

Meanwhile every design decision we've recorded recently — knowledge promotion across
scopes, learning from failed runs, session-scoped lessons reaching agent scope — ends with
"…that's the deep round's job." A pump nobody can run is not a pump.

nanobot demonstrates the missing piece is small: a **consumption cursor** plus a
**batched, scheduled pass**. Its Dream tracks `.dream_cursor` over an append-only history
and edits durable memory in batches on a cron interval. We adopt the cursor-and-batch
shape; we keep our own commitment path (bundle gate, not direct writes — see §E).

> **Superseded in part (2026-09-07, Phase 4.7):** the CLI examples below predate the
> mandatory ownership boundary. `--source persona:<id>|session:<id>` (repeatable) or
> `--all-sources` is now required, and `--scope` no longer defaults to `agent` — see
> [memory-scope-promotion](./memory-scope-promotion.md) §B.3. The cursor design is unchanged.

## B. Design: maintenance cursor + `vsigma memory consolidate`

### The cursor: processed-episode markers

Episodes are keyed by `run_id` (unordered), so an integer cursor doesn't fit. Instead the
pass records **one marker per consolidated episode** in a dedicated operational namespace:

```
namespace:  maintenance/state
key:        deep:<run_id>
value:      {"at": <iso timestamp>, "constructor": <name>, "bundle": <path>}
```

`maintenance/state` is deliberately **not a scope-ladder namespace**: `visible_scopes()`
never yields it, so markers can never be recalled into a prompt or count as knowledge.
It rides on the same `MemoryStore` (no second storage system, survives with the store,
copies with the file). "Unprocessed" is then a set difference:

```
episodes(store) − {run_id : marker exists}
```

Enumerating episodes needs namespace listing, which the minimal `MemoryStore` protocol
lacks — deliberately; it stays minimal. `SQLiteStore` grows a backend extra,
`namespaces()`, and the maintenance pass declares it works on stores that provide it.

### The command

```
vsigma memory consolidate --db agent.db
    [--scope agent]            # where distilled knowledge lands:
                               #   agent | global | persona:<id> | session:<id>
    [--out ./bundles]          # bundle output directory (one subdir per pass)
    [--limit 20]               # max episodes per invocation (nanobot-style batching)
    [--dry-run]                # write + print the bundle, do NOT ingest or advance
    [--model NAME]             # override VSIGMA_LLM_MODEL for this pass
    [--constructor NAME]       # provenance label (default: <model>/deep)
```

Flow, per invocation:

```
open store ── list unprocessed episodes (≤ limit, oldest first)
    │
    ├─ none → "nothing to consolidate", exit 0
    │
    └─ episode_trajectory() per episode  →  deep_consolidate(batch, llm, scope)
                                                    │
                                        write_bundle(out/<timestamp>)
                                                    │
                      --dry-run? ── yes → print records, STOP (nothing advanced)
                                                    │ no
                                        validate + ingest (the existing gate)
                                                    │
                                        write deep:<run_id> markers  ← only now
```

The LLM comes from the standard env (`VSIGMA_LLM_*` via `default_llm()`); the command
refuses to run without one — the deep round is defined by using a capable model, and a
silent degraded pass would write low-quality records at high (0.9) confidence.

**Failure semantics (the invariant): markers advance only after successful ingest.**
An LLM error, a malformed distill, a validation failure, or a crash mid-pass leaves every
episode unmarked — the next invocation retries the same batch. Re-ingest of a partially
committed bundle is already idempotent (content-addressed ids). A batch that distills
*zero* records still marks its episodes (they were processed and yielded nothing;
reprocessing forever is the alternative) but writes no empty bundle.

**Scope is the operator's explicit claim.** The default `--scope agent` fits the common
single-purpose-agent case and *is* a promotion decision: session- and persona-scoped
experience feeding an agent-wide distillation. That is exactly what the maintenance pass
is for — promotion as a deliberate, batched, reviewable act — and `--dry-run` is the
review point (read `records.jsonl`, then re-run without the flag). Multi-tenant operators
who must not cross personas run one pass per persona with `--scope persona:<id>`.
Per-record scope routing by the constructor itself is deferred (§D).

### Scheduling stays outside

nanobot schedules Dream internally (`intervalH`/cron config). We deliberately don't: the
recorded decision is that maintenance runs *without the agent* — and without the agent's
process. `vsigma memory consolidate` is cron-shaped by construction (idempotent, batched,
cheap when idle); a crontab line is the whole scheduler:

```
0 3 * * *  vsigma memory consolidate --db /srv/agent.db >> /var/log/vsigma-deep.log
```

## C. `vsigma memory export` — the read side

The inspection command that has been missing since the store existed:

```
vsigma memory export --db agent.db [--namespace persona:alice/knowledge]
```

Prints records as JSON lines (`{"namespace": …, "key": …, "value": …}`) to stdout — all
namespaces by default, one namespace when given. Composable with `jq`/`grep`; also the
extraction step for constructors that don't want to open SQLite themselves. Read-only.

## D. Non-goals / deferred (recorded so they aren't re-litigated)

- **Tombstones / retraction records** — nanobot's Dream *deletes* (dedupe, corrections,
  aging). Our bundle contract can only add/supersede; forgetting needs a retraction
  record type (bundle schema v0.2) and belongs with the strategy library work, measured
  under Phase 4.5's eval slice. The maintenance pass is where they'll execute once
  defined.
- **Per-record scope routing** — letting the deep constructor place each fact on its own
  rung (craft → agent, personal → persona) instead of one `--scope` per pass. Wants the
  propose-then-promote design; deferred until a consumer needs mixed batches.
- **Summarize-on-evict for session dialog** — nanobot's Consolidator validates the
  design; lands with Phase 4.5's dialog compaction, not here.
- **Internal scheduling** — see §B; cron owns time.
- **Non-SQLite stores** — the pass requires `namespaces()`; other backends opt in by
  providing it.

## E. What we deliberately did not copy from nanobot

- **Markdown files as the store** — nanobot's SOUL/USER/MEMORY split is a scope ladder
  frozen at three rungs for one user; ours is N-persona with visibility enforced by
  namespace, which flat files can't carry. The human-readable layer here is the bundle.
- **Constructor edits memory directly** — Dream holds write tools on the durable files,
  with git-revert as the safety net. We keep validate-before-commit: the maintenance
  pass writes a bundle and walks through the same gate as any foreign constructor.
  (Bundles kept under git give the same auditability nanobot gets from GitStore —
  recommended practice, documented in getting_started.)

## F. Graduation criteria

- An operator (or cron) runs `vsigma memory consolidate` repeatedly against a live
  agent's store: first run distills and ingests, second run reports nothing to do;
  a failed pass leaves the cursor untouched and the retry succeeds.
- Session-scoped lessons from a real consumer (bumblebee) reach agent scope through a
  pass with `--dry-run` review — the promotion story demonstrated end to end.
- `vsigma memory export` replaces raw `sqlite3` in the docs' inspection examples.

When validated, this graduates into architecture_design §9 (consolidation) and the CLI
section; tombstones and per-record routing return as their own proposal.
