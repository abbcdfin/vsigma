# Proposal: Token-Budget Context Assembly — Bounding What We Send

**Status:** DRAFT 2026-07-22 — awaiting review.
**Prompted by:** the point that an agent should adapt to the model's context window and
compact accordingly. Today's context policies bound a *count* (`SlidingWindowContext.
max_messages`, `RecallContext.max_facts`); a count is a proxy for the thing that actually
constrains the call — tokens against the model's window. A turn can be 20 tokens or
2,000, and the framework currently has no way to know or care.
**Sibling, not dependency:** session dialog compaction (separate proposal) bounds *what
we store and replay across rounds*; this bounds *what we send in a single call*.

## A. Problem

Two gaps, one structural and one informational.

**Structural:** the context budget is a *whole-window* property. The model's window must
simultaneously hold the pinned system prompt, `RecallContext`'s injected facts, the
replayed history, and the current turn — all competing for the same tokens. It can
therefore only be enforced where the whole window is assembled: the `ContextManager.
assemble` seam (`context.py:20-21`). Compacting any single piece (the dialog, say) cannot
guarantee the total fits — `RecallContext` can add ten facts on top and overflow anyway.

**Informational:** vsigma knows nothing about the model's window. The `LLM` protocol's
`complete` (`llm.py:59-62`) exposes no context-window size, and `LLMResponse`
(`llm.py:31-44`) carries no token usage. The framework can neither look the limit up nor
learn what a call actually cost. Adaptive compaction has to close this gap first.

## B. Design

### 1. Two small protocol additions

- **`usage` on `LLMResponse`** — an optional field carrying `prompt_tokens` /
  `completion_tokens` when the provider reports them (most OpenAI-compatible endpoints
  do). This lets the framework learn the *actual* token cost of the window it just sent —
  independently useful for cost tracking, and the calibration signal for §B.3.
- **The budget is app-configured, not introspected.** vsigma does not try to derive the
  window from the model — an arbitrary OpenAI-compatible endpoint won't reliably
  self-report it, and a model-name→window lookup table rots. The app, which knows its
  deployment, supplies a budget (max input tokens, i.e. window minus reserved output
  headroom) as a parameter on the context policy.

### 2. `TokenBudgetContext` at the assembly seam

A new `ContextManager` that enforces a token budget over the fully-assembled window:

```python
TokenBudgetContext(max_input_tokens=120_000, inner=RecallContext(inner=...))
```

It assembles `inner` first, then trims the result to fit — dropping oldest history turns
until the estimate is under budget, **never** evicting the pinned system prompt or the
current turn (a call that can't fit those two is a misconfiguration, surfaced as an
error, not silently mangled). Because it composes over `inner` via the existing seam
mechanism (`context.py:85-88`), it sees `RecallContext`'s injections and the history
*together* — which is the whole point: the budget is enforced on the sum, not piecemeal.

### 3. Estimation: heuristic guard, usage calibration — no tokenizer

- **Predictive guard:** a dependency-free `chars / 4` estimate decides trimming before
  the call. Imprecise, but the budget carries headroom, so approximate is safe.
- **Reactive calibration:** the `usage.prompt_tokens` from the previous response
  (§B.1) is the ground truth for what the last window actually cost. The estimator
  adjusts its chars-per-token ratio toward the observed value, so the heuristic tracks
  the real tokenizer over a session without ever importing one.

This hybrid gets predictive trimming (no wasted over-limit call) with self-correction (no
tokenizer dependency, no per-model tables).

### 4. Graceful degradation — the house rule

- No budget configured → behaves exactly as the count-based policies do today.
- Provider reports no `usage` → heuristic-only; still bounds the window, just less
  precisely. Nothing breaks; precision degrades.

## C. Non-goals / hard lines

- **A real tokenizer dependency** (tiktoken and friends) — out of scope for v0. Exact
  counting is a later precision upgrade behind this same `ContextManager` protocol; the
  heuristic-plus-usage path is the honest default for arbitrary endpoints.
- **vsigma introspecting the model's window** — never. The app supplies the budget; no
  magic model-name lookup table lives in the library.
- **Session storage bounding** — that is the sibling proposal. This governs the bytes
  *sent* per call, not the turns *stored* across rounds.
- **Semantic / summarizing compaction** — drop-oldest only; an LLM-summarized overflow is
  a heavier future option, not this.

## D. Graduation criteria

- With `max_input_tokens` set below the raw window size, the assembled window's estimated
  tokens stay under budget across a long run, while the pinned system prompt and the
  current turn always survive.
- The whole-window property holds: `RecallContext` facts plus replayed history are
  trimmed *together* to respect one budget (not each to its own count).
- Calibration: after real calls report `usage`, the estimate tracks server-reported
  `prompt_tokens` within a stated tolerance.
- Degradation: no budget → identical to count-based behavior today; no `usage` reported →
  the heuristic path still bounds the window.

When validated, this graduates into architecture_design's context-management section as
the budget-aware policy — the token-accurate sibling of `SlidingWindowContext`.
