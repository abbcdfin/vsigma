# Getting Started with vsigma

Build an agent using vsigma as a dependency. This is the consumer entry point — a quick,
runnable path, not the full reference (which grows in the design docs and, later, a manual).

> **Status:** vsigma is in early development (Phase 2). The public API may change. If you hit
> friction or a missing capability, that's useful signal — write it down (see *Feedback*).

## Install

vsigma requires **Python ≥ 3.11** and is managed with [`uv`](https://docs.astral.sh/uv/).

```bash
# Local/editable (developing against a checkout side-by-side):
uv add --editable ../vsigma

# Or, once published:
# uv add vsigma
```

## The mental model

vsigma gives you **blocks** you compose into a **graph** (the *loop*), which you `run` for one
task. You bring the state shape, the LLM, and your tools; vsigma supplies the assembler and
the cross-cutting services (memory, observability) woven around every node.

vsigma **augments LangGraph, it doesn't replace it**, and offers two levels:

- **Templates** — prebuilt topologies you instantiate with an LLM + tools (the fast path),
  forming a graceful-degradation ladder — start flat, add structure only when needed:
  - `make_react_agent` — flat ReAct loop, the baseline ("less structure, more intelligence";
    bounded only by `recursion_limit`, which you can raise for longer tasks).
  - `make_guarded_react_agent` — adds a loop-breaker that escalates to a human after `limit`
    attempts.
  - `make_plan_execute_agent` — adds an up-front planning step.

  The guarded/plan templates take a `limit` (attempts before escalating to a human). Their
  `recursion_limit` backstop defaults to a value coupled to `limit` so escalation always trips
  before LangGraph's hard limit; pass `recursion_limit=` to any template to override.
- **Primitives** — building blocks (`react_node`, `break_on_attempts`, …) plus the seam
  (`wrap` / `Services`, or `add_wrapped` / `add_router`) applied to a *raw* LangGraph
  `StateGraph`. Full control.

Either way you get an `Agent` (a compiled loop); `agent.run(task)` returns a `Run`
(`.status`, `.result`, `.trajectory`, `.resume(...)`).

Full design rationale: [`docs/architecture_design.md`](./architecture_design.md) and the
[toolkit-layering proposal](./proposals/toolkit-layering.md).

## Your first agent (runs offline) — via a template

```python
from vsigma import make_react_agent, Tool, FakeLLM, call, say

clock = Tool(name="clock", description="Return today's date.",
             parameters={"type": "object", "properties": {}},
             func=lambda args: "2026-06-28")

llm = FakeLLM([call("clock"), say("Today is 2026-06-28.")])

agent = make_react_agent(llm, [clock])
run = agent.run("What is today's date?")
print(run.status, run.result)
```

## Full control — primitives on a raw `StateGraph`

When a template isn't enough, build the graph yourself and apply the seam per node. You keep
all of LangGraph (streaming, subgraphs, checkpointers); vsigma just adds the cross-cutting
services:

```python
from langgraph.graph import START, StateGraph
from langgraph.checkpoint.memory import MemorySaver
from vsigma import (Agent, Services, add_wrapped, add_router, react_node,
                    break_on_attempts, human_interrupt_node, HUMAN_INTERRUPT,
                    ReActState, InMemoryStore, TrajectoryLog)

svc = Services(memory=InMemoryStore(), observer=TrajectoryLog())
sg = StateGraph(ReActState)
add_wrapped(sg, "act", react_node(llm, tools), svc)          # seam-wrapped node
add_wrapped(sg, HUMAN_INTERRUPT, human_interrupt_node, svc)
sg.add_edge(START, "act")
add_router(sg, "act", break_on_attempts(retry="act", escalate=HUMAN_INTERRUPT, limit=4))
sg.add_edge(HUMAN_INTERRUPT, "act")

agent = Agent(sg.compile(checkpointer=MemorySaver()), svc)
run = agent.run("...")
```

(`add_wrapped` is sugar for `sg.add_node(name, wrap(node, svc, name=name))`; use raw `wrap`
if you prefer.)

## Defining tools

A `Tool` is any callable with a name and JSON-schema parameters; the function receives the
parsed arguments and returns an observation string:

```python
def fetch(args: dict[str, object]) -> str:
    ...  # do the work
    return "observation text the model will see"

my_tool = Tool(
    name="fetch",
    description="What it does, for the model.",
    parameters={"type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"]},
    func=fetch,
)
```

`Tool` is the uniform contract; you can build one from several sources:

```python
from vsigma import tool, from_langchain, make_mcp_tools

# 1. plain function (name/description inferred):
@tool
def fetch(args: dict[str, object]) -> str:
    """What it does, for the model."""
    ...

# 2. an existing LangChain / LangGraph tool:
vs_tool = from_langchain(some_langchain_tool)

# 3. an MCP server's tools:
toolset = make_mcp_tools(connect)   # toolset.tools : list[Tool]
```

Pass plain functions through `tool()`, `from_langchain()`, or `make_mcp_tools()` before
handing them to `react_node` — it accepts vsigma `Tool`s only, and will tell you so clearly
if you pass something else.

## Using a real model

The agent depends only on the `LLM` protocol. For any OpenAI-compatible endpoint, set:

```bash
export VSIGMA_LLM_BASE_URL=https://your-endpoint/v1
export VSIGMA_LLM_API_KEY=sk-...
export VSIGMA_LLM_MODEL=your-model        # optional
```

```python
from vsigma import default_llm
llm = default_llm()    # OpenAICompatibleLLM if env is set, else None
```

`default_llm()` returns `None` when unconfigured, so you can fall back to a `FakeLLM`.

## Resilience: escalate and resume

`break_on_attempts` finishes when the agent is `done`, escalates to a human-interrupt node
when the attempt budget is spent, otherwise retries. A run that escalates pauses; continue it
with feedback:

```python
run = agent.run("...")
if run.status == "escalated":
    run.resume("here is the guidance you needed")
```

## Testing your agent offline

Do **not** use a real (or dummy) API key to test — that hits the network. Use **`FakeLLM`**,
the built-in offline engine. Script its turns tersely with `call` (a tool call) and `say`
(a final answer):

```python
from vsigma import FakeLLM, call, say

llm = FakeLLM([
    call("fetch", url="https://example.com"),   # 1st turn: call a tool
    say("Here is the answer."),                  # 2nd turn: finish
])
```

`FakeLLM` is deterministic — perfect for asserting that your graph routes and terminates as
expected, with no credentials.

For a quick "does this graph wire up and finish?" smoke test where you don't want to script
each step, use **`StaticLLM`**, which always returns the same final answer:

```python
from vsigma import StaticLLM
agent = build_agent(StaticLLM("ok"))   # any topology will just terminate
```

Switch to `default_llm()` only for live runs.

## Watching the model live

`run.trajectory` records *that* each step happened; to see *what the model said* as it
happens, wrap any LLM in `LoggingLLM` and flip the switch:

```python
from vsigma import LoggingLLM, enable_llm_logging, disable_llm_logging

llm = LoggingLLM(default_llm())          # composes with any LLM, incl. FakeLLM
enable_llm_logging()                     # replies + tool calls + latency → stdout
agent = make_react_agent(llm, tools)
agent.run("...")
disable_llm_logging()                    # back to silent
```

- `enable_llm_logging(file="llm.log")` logs to a file instead of stdout.
- `enable_llm_logging(logging.DEBUG)` additionally logs the **full prompt** each call
  (usually too much — that's why it's opt-in at DEBUG).
- It's ordinary stdlib logging under the logger name `vsigma.llm`, so if you already
  configure `logging` yourself, just set that logger's level/handlers and skip the helpers.
  Without any of this, `LoggingLLM` is silent — never surprise output.

## Inspecting a run

```python
run.status        # "running" | "done" | "escalated"
run.result        # the agent's final answer (or None)
run.trajectory    # list of TrajectoryEvent (thoughts/tool calls per step)

from vsigma import episode
agent.memory.query(episode(run.run_id).namespace("context"))  # the seam's episodic capture
```

## Memory: scopes, persistence, construction, consolidation

Memory namespaces follow the **scope ladder** (`global / agent / persona / session /
episode`) × **kind** (`knowledge` vs `context`); retrieval respects visibility — one
persona's private memory is never served to another:

```python
from vsigma import SQLiteStore, Services, TrajectoryLog, persona, recall, session

store = SQLiteStore("agent.db")                       # persistent, stdlib-only
store.write(persona("alice").namespace("knowledge"), "pref", "prefers concise answers")

svc = Services(memory=store, observer=TrajectoryLog())
agent = make_react_agent(llm, tools, services=svc)
run = agent.run("...", session_id="s1", user_id="alice")

recall(store, {"user_id": "alice", "session_id": "s1"})   # everything visible to this turn
```

**Bootstrapping knowledge from documents** goes through the construction engine — or any
external agent, fully offline. The contract is a **memory bundle** (`manifest.json` +
`records.jsonl`, schema: [`docs/schemas/memory-bundle.schema.json`](./schemas/memory-bundle.schema.json)):

```bash
vsigma memory validate ./bundle          # the deterministic gate
vsigma memory ingest   ./bundle --db agent.db
```

```python
from vsigma import distill, write_bundle, ingest_bundle
records = distill([("docs/team.md", text)], capable_llm, scope=persona("alice"),
                  constructor="my-model/1")
bundle = write_bundle("out/bundle", records, constructor="my-model/1")
ingest_bundle(bundle, store)             # or via the CLI above
```

**Consolidating experience** is two rounds of the same engine: a bounded run-end hook
(the agent's own LLM — or none, degrading to mechanical capture) and an offline deep pass
with a capable model that supersedes the provisional records:

```python
from vsigma import reflexive_consolidator, deep_consolidate, episode_trajectory

svc = Services(memory=store, observer=TrajectoryLog(),
               run_end=reflexive_consolidator(llm))          # round 1, provisional
records = deep_consolidate([episode_trajectory(store, run.run_id)],
                           capable_llm, scope=persona("alice"),
                           constructor="capable/deep")        # round 2 → bundle → ingest
```

**Getting memory into the prompt** is a context policy in the same seam slot — opt-in
(`RecallContext` injects visible knowledge, confidence-ranked and bounded; the default
`PassthroughContext` never silently injects):

```python
from vsigma import RecallContext, SlidingWindowContext

svc = Services(memory=store, observer=TrajectoryLog(),
               context=RecallContext(max_facts=8, min_confidence=0.3))

# For long episodes, compose with compaction (inner runs first):
svc = Services(memory=store, observer=TrajectoryLog(),
               context=RecallContext(inner=SlidingWindowContext(max_messages=30)))
```

## Where to go next

- A fuller agent: [`examples/web_browsing/`](../examples/web_browsing/) (browse-and-answer).
- The *what & why*: [`docs/product_specification.md`](./product_specification.md).
- The *how* / design model: [`docs/architecture_design.md`](./architecture_design.md) and
  [`docs/proposals/`](./proposals/).

## Feedback

vsigma is being co-designed against real agents. If something was missing, confusing, or
forced an awkward workaround, record it (e.g. a `VSIGMA_FEEDBACK.md` in your project) — that
list directly drives toolkit improvements.
