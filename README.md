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

The fastest start is a **template** — a prebuilt topology you instantiate with an LLM and
tools. This runs offline (no API key), so you can paste it into a project that depends on vsigma:

```python
from vsigma import make_react_agent, Tool, FakeLLM, call, say

# A tool is any function with a name + JSON-schema parameters.
clock = Tool(name="clock", description="Return today's date.",
             parameters={"type": "object", "properties": {}},
             func=lambda args: "2026-06-28")

# Scripted LLM so this runs with no credentials; swap for a real model below.
llm = FakeLLM([call("clock"), say("Today is 2026-06-28.")])

agent = make_react_agent(llm, [clock])     # a prebuilt ReAct agent (inspect agent.graph)
run = agent.run("What is today's date?")
print(run.status, run.result)              # -> done  Today is 2026-06-28.
```

Use a real model with **no code change** — set `VSIGMA_LLM_BASE_URL`, `VSIGMA_LLM_API_KEY`,
and (optionally) `VSIGMA_LLM_MODEL`, then `llm = default_llm()` (any OpenAI-compatible endpoint).

**Need control?** vsigma augments LangGraph rather than replacing it. Build the graph yourself
on a `StateGraph` and apply the cross-cutting seam (memory/observability/context) per node with
`wrap` / `add_wrapped`, then run it via `Agent` — see the getting-started guide.

See **[docs/getting_started.md](docs/getting_started.md)** for the full walkthrough, and
**[examples/web_browsing/](examples/web_browsing/)** for a web-browsing agent.

Working in this repo:

```bash
make install                                  # set up the environment (uv sync)
make check                                    # lint + type-check + tests
uv run python -m examples.web_browsing.demo   # run the example web-browsing agent (offline)
```

## Documentation

* **[`docs/getting_started.md`](docs/getting_started.md)** — build your first agent with vsigma as a dependency.
* **[`docs/product_specification.md`](docs/product_specification.md)** — the *what & why*: the problem, the core loop taxonomy, and the effort-estimation model.
* **[`docs/architecture_design.md`](docs/architecture_design.md)** — the *how*: tech stack, system architecture, and engineering guidelines.

> **Status:** Phase 1, early development. The design documents capture current intent and will evolve as the implementation matures.
