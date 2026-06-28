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

- **`GraphBuilder`** — the single assembler: `add` nodes, `route_conditional` edges, `compile`.
- **Blocks** — `react_node` (tool-calling loop), `break_on_attempts` (loop-breaker → human
  interrupt), and more.
- **`compile()` returns a loop; `loop.run(task)` executes it** and returns a `Run`
  (`.status`, `.result`, `.trajectory`, `.resume(...)`).

Full design rationale: [`docs/architecture_design.md`](./architecture_design.md) and the
[composition-model proposal](./proposals/composition-model.md).

## Your first agent (runs offline)

```python
from vsigma import (
    GraphBuilder, ReActState, react_node, break_on_attempts,
    HUMAN_INTERRUPT, human_interrupt_node, InMemoryStore, TrajectoryLog,
    Tool, FakeLLM, LLMResponse, ToolCall,
)

clock = Tool(name="clock", description="Return today's date.",
             parameters={"type": "object", "properties": {}},
             func=lambda args: "2026-06-28")

llm = FakeLLM([
    LLMResponse(tool_calls=[ToolCall(id="1", name="clock", arguments={})]),
    LLMResponse(text="Today is 2026-06-28."),
])

g = GraphBuilder(ReActState)
g.add("act", react_node(llm, [clock]))
g.add(HUMAN_INTERRUPT, human_interrupt_node)
g.route_conditional("act", break_on_attempts(retry="act", escalate=HUMAN_INTERRUPT, limit=4))
g.add_edge(HUMAN_INTERRUPT, "act")
agent = g.compile(memory=InMemoryStore(), observer=TrajectoryLog())

run = agent.run("What is today's date?")
print(run.status, run.result)
```

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

## Inspecting a run

```python
run.status        # "running" | "done" | "escalated"
run.result        # the agent's final answer (or None)
run.trajectory    # list of TrajectoryEvent (thoughts/tool calls per step)
agent.memory.query(namespace)   # what the wrapper seam wrote through memory
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
