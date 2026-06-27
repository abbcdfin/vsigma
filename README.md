# vsigma: Cognitive Architecture Toolkit for Autonomous Systems

**vsigma** is a production-grade toolkit of libraries, utilities, and structural patterns for architecting, implementing, and orchestrating complex cognitive networks — the foundational plumbing for building highly specialized, resilient agents.

> The name reads, on the surface, as the aggregation of multi-dimensional vector operations ($\Sigma \vec{v}$) into unified, goal-driven action. Whatever else it might allude to, we leave to the reader.

## Why vsigma

A single execution paradigm like **ReAct** is not enough on its own. Real cognitive work happens across loops operating at different frequencies and levels of abstraction — high-level orchestration, mid-level plan-and-refine, and a fast inner reasoning loop. vsigma provides modular, stateful building blocks to assemble these architectures deliberately, rather than forcing every problem through one monolithic prompt.

It also tackles a known weakness of LLMs — their inability to gauge task complexity unaided — with scaffolding for **dynamic effort estimation**, so an architecture can route work to the right level of machinery.

And because no two agents run under the same conditions, vsigma provides infrastructure for building **memory systems** that adapt to differing runtime constraints — from tight, latency-sensitive contexts to long-horizon agents that need durable recall.

Above storage sits **context management** — the active layer that governs what actually enters the model's finite window at each step: selecting and compacting from memory, scoping which tools are exposed, and prioritizing loop outputs. And running through all of it, **observability** captures each agent's trajectory — its thoughts, tool calls, and observations — so behavior can be evaluated, debugged, and tuned.

vsigma builds on **[LangGraph](https://langchain-ai.github.io/langgraph/)** as its state-machine runtime, rather than reimplementing checkpointing, async orchestration, and memory management from scratch.

## Evolution Path

* **Phase 1 — Toolkit Mode (current):** a modular repository of specialized building blocks (routing layers, lean state configurations, loop-breakers, and estimation scaffolding) used to explicitly assemble deterministic, task-specific cognitive architectures.
* **Phase 2 — Orchestration Engine Mode (future):** an autonomous runtime that assesses an open-ended goal on the fly, dynamically generates agent definitions, compiles a tailored state graph, and executes it.

## Quick start

```bash
make install                 # set up the environment (uv)
make check                   # lint + type-check + tests
uv run python -m vsigma.demo # run the example web-browsing agent (offline, no API key)
```

Assemble an agent from blocks:

```python
from vsigma import GraphBuilder, ReActState, react_node, break_on_attempts, \
    HUMAN_INTERRUPT, human_interrupt_node, InMemoryStore, TrajectoryLog, make_web_tools
from langgraph.graph import END

g = GraphBuilder(ReActState)
g.add("act", react_node(llm, make_web_tools()))          # llm: any OpenAI-compatible client
g.add(HUMAN_INTERRUPT, human_interrupt_node)
g.route_conditional("act", break_on_attempts(
    retry="act", escalate=HUMAN_INTERRUPT, limit=6, done=END))
g.add_edge(HUMAN_INTERRUPT, "act")
agent = g.compile(memory=InMemoryStore(), observer=TrajectoryLog())

run = agent.run("Find the latest post title on https://example.com")
print(run.status, run.result)
```

To use a real model, set `VSIGMA_LLM_BASE_URL`, `VSIGMA_LLM_API_KEY`, and (optionally) `VSIGMA_LLM_MODEL` — no code change.

## Documentation

* **[`docs/product_specification.md`](docs/product_specification.md)** — the *what & why*: the problem, the core loop taxonomy, and the effort-estimation model.
* **[`docs/architecture_design.md`](docs/architecture_design.md)** — the *how*: tech stack, system architecture, and engineering guidelines.

> **Status:** Phase 1, early development. The design documents capture current intent and will evolve as the implementation matures.
