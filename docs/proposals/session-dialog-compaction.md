# Proposal: Session Dialog Compaction — Bounding What We Store and Replay

**Status:** DRAFT 2026-07-22 — awaiting review.
**Prompted by:** the observation that `Session._dialog` grows without bound —
every `send()` seeds the next run with `history=list(self._dialog)`, the full
raw dialog so far, with no upper limit. For a long-running session (the daemon
that accepts instructions over channels indefinitely) this grows storage, replay
cost, and eventually the model window, forever.
**Sibling, not dependency:** token-budget context assembly (separate proposal)
governs *what we send per call*; this governs *what we store and replay across
rounds*. Different layers, independently useful, deliberately kept apart.

## A. Problem

`Session` persists the dialog under one key (`"dialog"`) in the session's context
namespace and re-seeds it into every new round (`sessions.py:87-110`). The list only
ever grows: `_dialog_turns` re-derives the complete dialog from each run's final
messages and writes it back whole. Two costs follow, both unbounded in a long session:

1. **Storage and replay** — the persisted `"dialog"` value grows with every turn, and
   each `send()` pays to re-seed and re-derive the entire history.
2. **Redundancy with distilled knowledge** — when a run has no `user_id`, distilled
   knowledge lands in *session* scope (`_knowledge_scope`, consolidation.py:41-48). But
   the raw turns that knowledge was distilled from are *also* replayed in full every
   round. The distilled fact and its source turn sit in the same window describing the
   same thing — an extra LLM call bought a redundant copy.

The redundancy in (2) is the tell: session-scoped distillation only earns its keep once
the raw turns it summarizes have *left* the window. Nothing removes them today.

## B. Design

### 1. An opt-in dialog policy on `Session`

`Session` gains an injected compaction policy, defaulting to **none** — current
behavior (a lossless, unbounded dialog) is preserved exactly for short sessions:

```python
DialogPolicy = Callable[[list[dict[str, object]]], list[dict[str, object]]]

def keep_last(n: int) -> DialogPolicy:
    """Keep the most recent `n` dialog turns; drop older ones from replay."""
    return lambda turns: turns[-n:] if len(turns) > n else turns

Session(agent, session_id=..., user_id=..., dialog_policy=keep_last(20))
```

### 2. Applied at re-derivation, after distillation

The policy runs at the one place `_dialog` is rebuilt (`sessions.py:107-110`), which is
*after* the run-end hook has fired (`run.py:124-126`). The ordering is therefore free
and correct: **a turn is always distilled before it can be truncated.** A turn can never
be dropped from replay without its durable residue having been captured first.

Because `_dialog` is already clean user/assistant turns (`_dialog_turns` strips tool
traffic, system prompts, and critique feedback), the policy operates on a simple list —
a plain "keep last N" slice, with none of the orphaned-tool-message handling that
`SlidingWindowContext` needs on a raw message list.

### 3. Compaction is lossy for replay only — never for the record

Dropping a turn from `_dialog` removes it from the *replay path* and nothing else:

- **The raw trajectory survives independently.** Every node update is written to
  `episode(run_id).namespace("context")` (`seam.py:65`), keyed by run_id, entirely
  separate from `_dialog`. The maintenance / deep-consolidation pass reads from there
  (`episode_trajectory`, consolidation.py:51-61) and still sees every episode in full.
- **The distilled residue survives.** Facts distilled to session or persona scope remain
  in the store and re-enter later rounds through `RecallContext`.

So a compacted session is not an amnesiac session: raw turns that fall out of the window
are still reachable as distilled knowledge (via recall) and as raw trajectory (via the
deep pass). This is precisely what makes session-scope distillation stop being
redundant — after compaction, the raw window and the distilled facts cover **disjoint**
spans of the session instead of the same span twice.

## C. Non-goals / hard lines

- **Token awareness** — a turn count is a proxy for tokens; enforcing the model's actual
  context window is the sibling proposal's job, at the context seam where the whole
  assembled window is visible. This proposal bounds *stored turns*, nothing more.
- **Touching the raw episode trajectory** — never. The deep pass's source of truth stays
  complete; only the replay window is bounded.
- **Summarizing compaction** — folding dropped turns into an LLM-written running summary
  is a heavier future option; v0 is drop-oldest, because the distilled-knowledge path
  already *is* the summarization mechanism (run-end hook → recall).
- **Default-on** — the default is no policy, so short sessions stay byte-identical to
  today; long-running consumers opt in explicitly.

## D. Graduation criteria

- A long session under `keep_last(N)`: the persisted `"dialog"` never exceeds N turns,
  and replay cost stops growing.
- Dropped-turn recoverability: a fact stated in a turn later dropped from `_dialog`,
  distilled to session/persona scope, still surfaces via `RecallContext` in a subsequent
  round.
- The deep pass over that session's episodes still reads full raw trajectories —
  compaction left the `episode:*/context` namespaces untouched.
- Ordering property: no turn is ever truncated before the run-end hook has distilled its
  episode.
- Default (`dialog_policy=None`) reproduces today's dialog exactly.

When validated, this graduates into architecture_design beside the `Session` handle, as
its long-running-session story.
