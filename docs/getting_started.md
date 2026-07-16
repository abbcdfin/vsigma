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
  - `make_reflexion_agent` — adds a deterministic critic: every candidate answer is
    validated before the run counts as done; failures are fed back and the agent revises.

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

## Multi-round dialog: sessions

One `agent.run()` is one episode. For several rounds of interaction — each new round
aware of what came before — open a `Session`:

```python
session = agent.session("s1", user_id="alice")
session.send("What color is the sky?")           # episode 1
run = session.send("Why?")                       # episode 2 — carries the dialog
```

- Each `send` starts a new episode seeded with the dialog so far (user/assistant turns
  only — tool traffic and critique feedback are episode-local working noise).
- The dialog is persisted in the session's memory namespace, so on a persistent store
  (`SQLiteStore`) a session **rehydrates**: reopen `agent.session("s1")` in a new
  process and the conversation continues.
- If the previous round escalated, `send` routes the payload to `resume()` on that run —
  one verb for "here is the next human input", mid-run or between runs.
- `send(payload, channel="slack")` records the channel as event metadata (the model
  never sees it) — sessions are transport-agnostic; whatever delivers the message (your
  CLI loop, an HTTP endpoint, later a message bus) terminates in `session.send`.

## Resilience: escalate and resume

`break_on_attempts` finishes when the agent is `done`, escalates to a human-interrupt node
when the attempt budget is spent, otherwise retries. A run that escalates pauses; continue it
with feedback:

```python
run = agent.run("...")
if run.status == "escalated":
    run.resume("here is the guidance you needed")
```

## Critique: validate before done (Reflexion)

When "finished" isn't the same as "right", add a deterministic critic. The validator is
any `str -> None | critique` function — `None` accepts, a string rejects and is fed back
so the agent revises. The strongest validators are objective checks (run the tests,
validate against a schema), not a model grading itself:

```python
from vsigma import make_reflexion_agent

def validator(candidate: str) -> str | None:
    return None if is_valid(candidate) else "what failed, verbatim"

agent = make_reflexion_agent(llm, tools, validator, limit=4)
run = agent.run("...")          # done only once the validator accepts
```

A candidate that keeps finishing but never validates escalates to the human-interrupt
node after `limit` attempts (then `run.resume(...)` as usual) — it never spins. The
worked example is a code kata where the validator runs the tests in a subprocess:
`python -m examples.code_kata.demo` (offline; note the loop converges to "passes the
tests", not "is correct" — the critique is only as good as the tests).

### Choosing a validator

A validator doesn't have to prove the output is *correct* — it only has to reject
outputs that are *demonstrably broken*. Lower the bar from "verify correctness" to
"catch the cheap, common failure modes" and most agents have several clauses available:

- **Format / contract** — parses as JSON, matches the schema, required sections present,
  no placeholder residue ("TODO", "…"). The most universal family: malformed output is
  the most common way agents fail their downstream consumers.
- **Execution** — the code compiles, the SQL `EXPLAIN`s, the config validates, the
  command passes `--dry-run`.
- **Ground-truth reconciliation** — the output checks against what the agent had: every
  number in the summary appears in the source, cited URLs were actually fetched, line
  items sum to the stated total. You can't verify the *analysis*, but you can verify it
  isn't fabricating its evidence.
- **Policy / constraints** — business rules from the task: budget under X, no forbidden
  recipients, mandatory fields filled.
- **Round-trip** — parse what was generated, decode what was encoded.

Two rules. The validator is latent in your task spec: every "the output must…" sentence
is a clause. And if you can't write *any* clause, don't fake the gate — use the flat or
guarded template instead; a gate that can't reject anything is ceremony.

Concrete clauses are yours to write — they encode *your* output contract, so they live
in your project, not in vsigma (same rule as tools: `Tool` is core, the web tools are an
example). A format-contract clause is typically a few lines:

```python
import json

def expect_json(candidate: str) -> str | None:
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return f"output is not valid JSON: {exc}"
    missing = [k for k in ("summary", "sources") if k not in data]
    return f"missing required keys: {', '.join(missing)}" if missing else None
```

What vsigma provides is the composition: `all_of` chains clauses so the first failure
becomes the critique — deterministic checks first:

```python
from vsigma import all_of, llm_judge

validator = all_of(
    expect_json,                  # your format contract
    my_reconciliation_check,      # any str -> None | critique fn
    llm_judge(judge_llm, rubric="every claim cites one of the sources"),
)
```

`llm_judge` is the deliberate exception to determinism — a second model grading the
candidate against a rubric (reply `PASS` or the violations). Use a *different*, ideally
stronger, model than the generator; keep it **last** in `all_of` so it only sees
candidates that already passed everything objectively checkable; it fails closed (a
judge error becomes a critique, so the attempt budget escalates rather than the run
crashing). When verification itself needs tools (fetch the cited URLs, query the
database), the validator can wrap a whole checker agent — build one with
`make_react_agent`, call `checker.run(...)` inside the validator, and parse its result;
nothing in the engine knows an agent lives behind the callable.

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

The deep round is also a **runnable operation** — the maintenance pass tracks which
episodes it has already processed and batches the rest through the bundle gate
(cron-shaped: idempotent, cheap when idle; design in
[`docs/proposals/maintenance-pass.md`](./proposals/maintenance-pass.md)):

```bash
vsigma memory consolidate --db agent.db --dry-run   # review records.jsonl first
vsigma memory consolidate --db agent.db             # then commit + advance markers
# --scope agent (default) | global | persona:<id> | session:<id>
```

It uses the `VSIGMA_LLM_*` environment for the capable model, and a failed pass never
advances the markers — rerunning retries the same batch. To inspect what's in a store:

```bash
vsigma memory export --db agent.db --namespace persona:alice/knowledge   # JSON lines
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

### Putting it together: an agent that learns from experience

A bare template does **not** learn: its defaults are an in-process store, no run-end
hook, and a context policy that never injects. Learning is a loop with two halves —
a **write side** (distill experience at run-end) and a **read side** (recall it into
later prompts) — and both are `Services` knobs on the same agent:

```python
from vsigma import (RecallContext, Services, SQLiteStore, TrajectoryLog,
                    make_react_agent, reflexive_consolidator)

store = SQLiteStore("agent.db")                       # survives the process
svc = Services(
    memory=store,
    observer=TrajectoryLog(),
    run_end=reflexive_consolidator(llm),              # write side: distill at run-end
    context=RecallContext(max_facts=8),               # read side: inject what's known
)
agent = make_react_agent(llm, tools, services=svc)

agent.run("figure out how to upload to the fileserver", user_id="alice")
# run-end → provisional knowledge, e.g. "the upload endpoint needs a session cookie"

run = agent.run("upload report.pdf", user_id="alice")
# RecallContext injects that fact — this run starts where the last one learned
```

The second run's prompt carries a tagged system message with the distilled facts;
without either half there is no loop (write-only hoards, read-only has nothing to read).
Scoping matters: knowledge lands in the serving persona's scope (`user_id`), and recall
respects visibility — pass the same `user_id` (or `session_id`) or the later run won't
see it.

What actually gets learned, and from what:

- Every run — success **or** escalation — leaves a factual episode summary in session
  context, so a retry can discover "a previous attempt at this task failed".
- Provisional *knowledge* (confidence 0.3) is distilled only from runs that end `done`:
  a cheap pass over a failure trajectory risks memorizing the mistake instead of the
  lesson. Learning *from failures* is the offline `deep_consolidate` pass (previous
  snippet) — a capable model comparing episodes, superseding the provisional records
  through the bundle gate.
- `reflexive_consolidator()` with no LLM still works: mechanical summaries only,
  nothing distilled — capture degrades gracefully, it never breaks the run.

## Where to go next

- A fuller agent: [`examples/web_browsing/`](../examples/web_browsing/) (browse-and-answer).
- The *what & why*: [`docs/product_specification.md`](./product_specification.md).
- The *how* / design model: [`docs/architecture_design.md`](./architecture_design.md) and
  [`docs/proposals/`](./proposals/).

## Feedback

vsigma is being co-designed against real agents. If something was missing, confusing, or
forced an awkward workaround, record it (e.g. a `VSIGMA_FEEDBACK.md` in your project) — that
list directly drives toolkit improvements.
