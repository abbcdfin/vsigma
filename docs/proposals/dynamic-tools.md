# Proposal: Dynamic Tool Exposure — `tools_fn` and the Gated Catalog

**Status:** DRAFT 2026-07-18 — awaiting review.
**Prompted by:** bumblebee's service/daemon planning: one long-lived process serving many
channels and users wants different tool exposure per request, per user, and per playbook
step — and eventually wants the agent itself to be able to *gain* a capability at
runtime. Today both are impossible below the graph level: a react node's toolset is
frozen at construction, and the only way to change what an agent can do is to rebuild it.

## A. Problem

`react_node(llm, tools)` closes over its tool list at graph-construction time: the
name→tool map and the spec list are computed once and every invocation presents the same
tools. That is the right default — visible, testable, no hidden state — but it leaves
two real gaps:

1. **No per-call exposure.** "This tool only exists for admin users", "the write tools
   appear only after the plan is approved", "this channel gets the read-only set" — all
   of these are *state-driven* decisions, and today the only expression vsigma offers is
   topology: duplicate the node per variant and route between them. Coarse variants
   (planner vs. executor) belong in topology; per-user permission checks do not — that
   way lies a combinatorial node explosion.
2. **No admitted path for runtime extension.** An agent cannot acquire a capability
   mid-run, and there is no vsigma answer for "the daemon connected a new MCP server —
   how does the running agent learn about it?" The unstated alternative — the app
   mutating a tool list the node happens to reference — is exactly the kind of invisible
   side channel the seam design exists to prevent.

The second gap has a security face that shapes the whole design: **"register a new tool"
is "authorize new code to run" wearing a nicer name.** The moment the *agent* (not the
operator) influences what joins its toolset, admission needs the same discipline the
memory bundle system applies to facts: staging → validation gate → explicit ingest,
every admission inspectable. An agent silently extending itself is a non-goal forever.

## B. Design

### 1. `tools_fn` — the toolset becomes a function of state

`react_node` (and the template ladder above it) accepts either the existing list or a
selector:

```python
ToolSelector = Callable[[State], list[Tool]]   # in tools.py

def react_node(llm: LLM, tools: list[Tool] | ToolSelector) -> NodeFn: ...
```

- A plain list behaves exactly as today (validated once, closed over — zero change for
  every existing caller and test).
- A selector is called **once per step**, and the returned list produces both the spec
  list shown to the model *and* the name→tool map used to execute — within a step, what
  the model sees and what can run are the same list by construction. A tool that
  disappears between steps yields the existing soft error (`error: unknown tool`), never
  a crash.
- Contract on the selector: cheap, side-effect-free, total (never raises for reachable
  states). It is a *view* over capability, not a place to do work.

App-side, per-user exposure is now a four-line function — no catalog required:

```python
def tools_for(state: State) -> list[Tool]:
    if state.get("user_id") in ADMINS:
        return [search, deploy]
    return [search]

node = react_node(llm, tools_for)
```

### 2. `ToolCatalog` — capability with provenance, visibility as the switch

For the daemon case — capability that changes over the life of the process — a plain
closure isn't enough: something must own the mutable set, remember where each tool came
from, and be safe to share. `ToolCatalog` (new module `catalog.py`) is that owner:

```python
catalog = ToolCatalog()
catalog.add(search, weather)                                  # operator-admitted, enabled
catalog.add(*tickets.tools, source="mcp:tickets", enabled=False)  # connected, not exposed
catalog.enable("create_ticket")                               # operator flips visibility
node = react_node(llm, catalog.select)                        # select: ToolSelector
```

- Every entry carries `source` (who supplied it: `"app"`, `"mcp:<server>"`,
  `"langchain"`) and `enabled`. `select(state)` returns the enabled tools; it *is* a
  `ToolSelector`, so the catalog composes with §B.1 rather than replacing it (an app can
  wrap `catalog.select` in its own permission filter).
- **The invariant that makes it safe:** the catalog never fetches, builds, or executes
  code. Capability enters only through `add()` — an operator-side call, in practice fed
  by the existing adapters (`tool(fn)`, `from_langchain`, `make_mcp_tools`). Admission
  and enabling are *visibility control over already-vetted handles*, nothing more.
  "Download a tool" is not a catalog operation and never will be (§C).
- Mutation is guarded by a lock; `select` snapshots under it. This is the first vsigma
  object designed to be shared across threads — deliberate, because its home is a
  daemon serving concurrent channels.

### 3. The gate — how the *agent* asks, and who says yes

The agent-facing side is two meta-tools the catalog can mint, mirroring the bundle
pipeline's staging → gate → ingest:

```python
node = react_node(llm, catalog.select)
catalog.add(*catalog.meta_tools(gate=policy))   # they are ordinary Tools themselves
```

| meta-tool | what it does |
|---|---|
| `list_available_tools` | names + descriptions of *disabled* entries — the shelf the agent may ask from |
| `request_tool(name)` | files a request; the gate decides |

The `gate` is a policy callable, `Callable[[Tool, State], bool]`:

- **Gate approves** → the tool is enabled and — because specs are recomputed per step
  (§B.1) — appears on the very next step. The observation says so:
  `"tool 'create_ticket' is now available"`.
- **Gate declines or no gate given** → the request lands in `catalog.pending` for
  operator review, and the observation says *that*:
  `"request recorded; 'create_ticket' requires operator approval"`. Nothing is enabled.
  The default is the closed position.

The parallel to memory construction is exact and intentional: the shelf of disabled
tools is the staging artifact, the gate is `validate_bundle`, enabling is `ingest`, and
`source` + the pending queue are the provenance trail. One discipline, two kinds of
cargo — facts there, capabilities here.

### 4. What stays topology

Coarse context switches — planner toolset vs. executor toolset, read phase vs. write
phase — remain graph structure, as today. The rule of thumb for the docs: **if the
variant is visible in the playbook, it's a node; if it depends on who's asking or what
just happened, it's a selector.** `tools_fn` does not replace routing; it removes the
temptation to abuse routing for permission checks.

### 5. What stays app-side: plugin discovery

The consumer pattern that motivated this section of review: an agent whose tools are
*discovered* — a plugin directory scanned at startup, each module contributing `Tool`
objects. That loader belongs in the application, permanently, because a plugin loader by
definition imports and executes the discovered code — and vsigma's line is that it never
does that. The component that decides which directory is trusted must be the operator's
own code.

The split is clean because `@tool` already provides the discovery marker: a plugin file
is just a module containing `Tool` instances, and the app-side loader is ~15 lines of
`importlib` — scan, import, collect `isinstance(v, Tool)`, hand the list to the agent
constructor (or to `catalog.add(*tools, source=f"plugin:{name}")`).

Two boundaries the docs must state when this pattern is documented:

- **Trust:** loading a plugin runs its code with the host's privileges — the plugin
  directory is operator-controlled configuration, the same trust level as installing a
  dependency. It must never be writable by anything else, *including the agent itself*
  (an agent that can write its own plugin dir and trigger a reload has acquired
  self-modification through the back door — the exact thing §A's gate exists to
  prevent).
- **Startup vs. runtime:** discovery at construction time needs nothing from this
  proposal — it works against today's vsigma. Only *hot-loading* (new files appearing
  while the daemon runs, exposed to a live agent) involves the catalog: the loader
  `add()`s the new tools and the selector makes them visible on the next step.

### 6. Skill packages — the fourth tool source (instructions, not code)

The Claude-Code-style SKILL package (`SKILL.md` with name/description frontmatter and a
body of procedural instructions, optionally with supporting files) is the inverse of the
plugin problem: it is *instructions, not code*, so reading it executes nothing and the
"vsigma never loads code" line is not even approached. It therefore belongs **in
vsigma**, as a fourth adapter beside hand-written / `from_langchain` / `make_mcp_tools`:

```python
skills = load_skills(Path("skills"))     # -> list[Tool], one per package
node = react_node(llm, [*skills, *other_tools])
```

The mapping is exact, because **progressive disclosure is just the tool contract**: the
frontmatter name + description become the tool spec (always visible to the model, a few
lines each); invoking the tool returns the SKILL.md body as the observation (loaded into
context only on demand, plus pointers to the package's supporting files). No new runtime
mechanism, no always-on context cost — the same economy the format was designed for,
delivered by machinery that already exists.

Scope of the adapter, stated as hard lines:

- **Instructions only.** Skill packages often ship executable helper scripts the body
  tells the model to run. The adapter never loads or executes them — whether they can
  run depends entirely on whether the operator gave the agent a shell/exec tool, which
  is an app-level capability decision. An adapter that quietly executed bundled scripts
  would be the plugin-loader problem returning through markdown.
- **Minimal frontmatter.** Read `name` and `description`; ignore harness-specific
  fields (allowed-tools and the like) rather than half-emulating another product's
  semantics.
- **Which directory to trust** stays operator configuration, like every other source.

Skills compose with the rest of this proposal for free: `catalog.add(*skills,
source=f"skill:{name}")` gives per-package provenance and gating, and a selector can
expose different skill sets per channel like any other tools.

## C. Non-goals / deferred

- **Downloading or generating executable code at runtime** — permanently out of scope.
  Capability enters through operator-connected sources behind the `Tool` contract (MCP
  being the designed extension mechanism: the code runs server-side, the agent holds a
  typed handle). The catalog will never have a "fetch" verb.
- **Semantic tool retrieval** — selecting the *relevant* few from a large catalog per
  query needs embedding infrastructure and shares it with Phase 4.5's semantic recall;
  deferred to that phase. `select(state)` is the seam it would slot into.
- **Persistent catalog state** — enable/disable decisions and the pending queue die with
  the process for now. If daemons need approvals to survive restarts, that's a later
  slice (likely a small operational namespace, like the maintenance markers).
- **Argument-level permissions** ("may call `deploy` but only with `env=staging`") —
  belongs inside the tool or a wrapping policy tool, not in exposure control.
- **A `load_tools_from(path)` helper** — the plugin loader stays app-side (§B.5). If a
  second consumer beyond bumblebee writes the same fifteen lines, reconsider — but a
  vsigma-shipped loader blurs "vsigma never loads code" and is not worth it for one
  consumer.

## D. Graduation criteria

- bumblebee's daemon serves two channels with different toolsets from **one** graph via
  a selector — no node duplication, verified by transcript.
- A live agent lists the shelf, requests a gated tool, and uses it on the next step
  under an approving gate; under the default (no gate), the same request lands in
  `pending` and the toolset is unchanged.
- A tool disabled between steps degrades to the existing soft error, observed on a live
  run — never an exception.
- Existing static-list callers pass untouched (the union type is additive).
- A real SKILL package loads via `load_skills` on a live model: the spec footprint stays
  at name + description until invoked, the body arrives as an observation, and no
  bundled script is ever executed by the adapter itself.

When validated, §B.1 graduates into architecture_design's react-seam section and
§§B.2–B.3 as a new "capability admission" section beside memory construction.
