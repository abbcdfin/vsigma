# Proposal: Toolkit Layering — Augment-not-Replace + Agent Templates

> **Status: GRADUATED (2026-07-04)** into
> [`architecture_design.md`](../architecture_design.md) §2 — kept as the historical record of
> rationale and refinements. Implemented in Phase 2.5: `GraphBuilder` removed;
> `wrap`/`Services`/`Agent`/`Run` work on a raw LangGraph `StateGraph`; `make_react_agent` /
> `make_plan_execute_agent` templates added; example/demo/tests/docs migrated.
> Supersedes the construction model in the [composition-model proposal](./composition-model.md).
> The **code is the source of truth**.
>
> **Refinements vs. draft:**
> - Added `add_wrapped` / `add_router` helpers (operate on a `StateGraph` you own) because
>   LangGraph's strict node typing makes the bare `sg.add_node(name, wrap(...))` path noisy
>   under mypy-strict. `wrap` is still the primitive.
> - Templates form a **graceful-degradation ladder**: `make_react_agent` is the *flat* baseline
>   ("less structure, more intelligence"; pure ReAct loop, matching LangGraph's
>   `create_react_agent`, bounded by `recursion_limit`); `make_guarded_react_agent` adds
>   human-interrupt escalation; `make_plan_execute_agent` adds planning. (`make_react_agent`
>   was repurposed to the flat loop; the guarded one is the renamed original.)

## Motivation

A design review concluded vsigma was leaning toward a **wrapper that replaces** LangGraph's
`StateGraph` rather than an **opinionated layer that augments** it. Evidence: `GraphBuilder`
is ~1:1 with `StateGraph` (indirection + feature-lag + type casts), and the first consumer
(bumblebee) immediately hit friction where our abstraction *subtracted* capability LangGraph
already had (LangChain tools). The fix is to (1) stop hiding LangGraph and (2) provide a
proper "good start" tier so not every agent rebuilds the graph by hand.

## Three tiers

1. **Primitives** — building blocks (`react_node`, `break_on_attempts`, …) + `wrap` / `Services`
   / `Run`, used on a *raw* LangGraph `StateGraph`. Full control, full LangGraph power.
2. **Templates** — one-line, opinionated, prebuilt agents (`make_react_agent`, …). The good
   start. Replaces `GraphBuilder`'s convenience role — better, because a template is a concrete
   known-good topology, not a generic re-skin of graph construction.
3. **Orchestration engine (Phase 2 product)** — generates/picks graphs from a goal. Templates
   are its curated precursor and building material.

## Decision 1 — Augment, don't replace (drop `GraphBuilder`)

`wrap()` is the core value as a composable primitive (`node → node`), applying the
cross-cutting seam (context → node → memory → observability) to any node on a plain
`StateGraph`:

```python
from langgraph.graph import StateGraph, START
from vsigma import wrap, Services, Run, react_node

svc = Services(memory=InMemoryStore(), observer=TrajectoryLog())
sg = StateGraph(ReActState)
sg.add_node("act", wrap(react_node(llm, tools), svc))
sg.add_edge(START, "act")
graph = sg.compile(checkpointer=...)
run = Run(graph, svc).start("task")     # run/resume/status/result/trajectory over a vanilla graph
```

- **`GraphBuilder` is removed.** Its only non-facade value (apply-seam-uniformly, hold service
  config, run ergonomics) is delivered by `wrap` + `Services` + `Run`, which sit *beside*
  `StateGraph` instead of hiding it.
- **No more `StateGraph` mirroring** → no feature-lag, no casts, smaller surface.
- Trade-off: users wire the graph in LangGraph directly (a few more lines, but one honest graph
  API), and must apply `wrap()` themselves (or use a template, below). Acceptable.

## Decision 2 — Agent templates (`make_*`)

Prebuilt topologies that compose the blocks for the common cases:

```python
agent = make_react_agent(llm, tools, memory=..., limit=4)
agent = make_plan_execute_agent(llm, tools)
agent = make_critique_agent(llm, tools, validator=run_tests)
```

Rules that keep templates honest (augment, not re-hide):
- A template **returns an inspectable, extensible graph** (or thin handle exposing it), never a
  sealed box — you can run it, crack it open, or drop it into a bigger graph as a node.
- Built only on the **public blocks + raw `StateGraph`**; lives in core (`vsigma/agents/`).
- **Naming: `make_*`** — consistent with `make_mcp_tools` / `make_web_tools`.

## Decision 3 — Model-awareness: explicit yes, inference no

- **Explicit declared capabilities → supported, opt-in, measured.** Templates may accept hints
  (`context_window`, `supports_tools`, `tier`) and adjust (e.g. enable compaction on a small
  window, add a plan/critique stage for a weaker model). This is transparent *configuration*.
- **Inference from the model string → rejected for v1.** Mapping model → capability → topology
  is brittle, goes stale every release, and silently changes agent shape (opaque, anti-
  determinism). That belongs to the Phase-2 orchestration engine, and any auto-tuning must be
  tied to eval.

## Migration impact

Pre-1.0, Phase 2, one consumer (bumblebee) → cheap now. Removing `GraphBuilder` touches the
example, demo, tests, `getting_started.md`, and the public API; templates then simplify the
example/demo. A migration note goes to bumblebee.

## Graduation criteria

Promote into [`architecture_design.md`](../architecture_design.md) only after:
- `wrap()` demonstrably applies the seam on a raw `StateGraph` (test), and `Run` works over a
  vanilla compiled graph.
- At least two `make_*` templates exist, each returning an inspectable/extensible graph and
  built only on public blocks.
- The example/demo are simpler under templates than under the old `GraphBuilder`.
