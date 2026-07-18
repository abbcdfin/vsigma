# Proposal: Agent-Authored Tools — the Skill Pipeline

**Status:** DRAFT 2026-07-18 — written to sleep on; review before any acceptance.
**Depends on:** the dynamic-tools proposal (itself DRAFT) — the catalog, the gate, and
the `source` provenance are the substrate this builds on. Nothing here should be
implemented before that lands *and* a consumer demonstrates the need.
**Prompted by:** the question "can the agent write new code and load it as tools?" The
current answer is no at every layer, on purpose. But the underlying want — Voyager-style
skill acquisition, an agent that grows a library of capabilities from experience — is
legitimate and will eventually be asked for by a consumer. If it arrives without a
design, it will be improvised as `write_file` → loader → reload, which is
self-modification with no review point. This proposal records the safe construction *in
advance*, so the improvised version never looks like the easy path.

## A. Problem

"The agent registers a tool it wrote" collapses two acts that must never be one act:

1. **Authoring** — the agent produces code. This is harmless in itself; code at rest is
   data.
2. **Loading** — some process imports and executes that code with its own privileges.
   This is the dangerous act, and today no vsigma path performs it (tool lists are
   frozen at construction; nothing in the library calls `exec`/`importlib` on model
   output; the dynamic-tools catalog only toggles visibility of already-vetted handles).

Every unsafe design fuses the two: the author is also the admitter. The agent writes
into the live plugin directory; the loader picks it up; the agent has extended its own
capabilities with no one in the loop. Dynamic-tools §B.5 names this the back door, and
the fix is the same discipline the memory system already enforces for facts: **an
artifact produced by an LLM is staged, gated, and only then ingested — and the producer
never operates the gate.** Facts get this treatment at 0.3 confidence; code, which
*executes*, deserves it more.

## B. Design

The pipeline is the bundle pipeline with code as cargo. Four stages, each with a named
owner:

| stage | act | owner | vsigma's part |
|---|---|---|---|
| 1. draft | agent writes a skill bundle to inert staging | agent (via meta-tool) | the bundle contract |
| 2. gate | shape validated; safety reviewed | human, or sandboxed harness — never the author | shape validation only |
| 3. admit | code loaded (or wrapped) and cataloged with provenance | operator-side loader | `catalog.add(source=...)` |
| 4. execute | admitted skill runs, preferably out-of-process | app / MCP server | the `Tool` contract |

### 1. The skill bundle — a draft is data, not capability

The agent's authoring surface is a meta-tool (`draft_skill`, minted like the catalog's
meta-tools) that writes a **skill bundle** into a staging directory the loader never
reads: one code file plus a manifest —

```
staging/<skill-id>/
  manifest.json    # name, description, parameters schema, rationale,
                   # author ("agent:<run_id>"), created_at, status: "draft"
  skill.py         # the code; must expose Tool objects via @tool, like any plugin
```

The manifest mirrors the memory bundle's: identity, provenance, and a claim of shape.
`rationale` is required — the agent must say *why* it needs this capability and what
existing tools failed; that sentence is the first thing a reviewer reads. Drafting can
happen mid-run and costs nothing: the run continues with the tools it has, and the
observation says so — `"skill drafted; awaiting review"` — never implying availability.

### 2. The gate — shape is vsigma's to check; safety is not

`validate_skill_bundle` (mirroring `validate_bundle`) checks what a machine can
honestly check: manifest schema, name not colliding with an existing catalog entry,
the code file parsing, declared parameters being a valid JSON schema. **It is
explicitly not a security scan** — static analysis of hostile code is a losing game,
and pretending otherwise would invite operators to trust it. Safety comes from the
review step, which has exactly two acceptable forms:

- **Human review** (the default): the pending drafts are the operator's queue; nothing
  proceeds until a person marks the bundle admitted. For bumblebee's scale this is the
  right answer for a long time.
- **A sandboxed harness** (the automated option, later): the draft runs its own
  declared tests inside an isolated environment with no host privileges, and only a
  green, resource-bounded run permits admission. This is a real engineering project —
  it must not be faked with an allowlist of imports.

The invariant over both: **the authoring agent never operates the gate.** Not via a
meta-tool, not via a second run of the same agent, not via "the model reviewed its own
diff". The reviewer is a different principal or a different, non-agentic mechanism.

### 3. Admission — provenance is permanent

Admission is the operator-side loader importing the reviewed bundle (or better,
mounting it behind an MCP server — §B.4) and registering it:

```python
catalog.add(*load_skill(bundle_dir), source=f"agent-authored:{skill_id}")
```

The `source` tag never washes out: exports, logs, and any future audit can always
distinguish operator-installed capability from agent-authored capability. Revocation is
`catalog.disable(name)` — instant, no unload required, because exposure was always
visibility (dynamic-tools §B.2). The staged bundle moves to `admitted/` (or
`rejected/`) so the staging directory is also the audit trail.

### 4. Execution — out-of-process by default

Even admitted, agent-authored code should not run with the daemon's privileges. The
recommended admission target is a **skill-host MCP server**: a thin, app-side process
that loads admitted bundles and serves them over MCP. The daemon then consumes them via
`make_mcp_tools` like any other server — the agent holds a typed handle; the code runs
in a process that can be resource-limited, containerized, and killed. In-process
loading remains possible for operators who accept the risk, but every document
describing this pipeline states the default plainly: the skill host is the intended
shape, in-process is the shortcut.

### 5. What vsigma ships vs. what stays app-side

vsigma's surface is deliberately thin: the skill-bundle schema, `validate_skill_bundle`,
the `draft_skill` meta-tool, and the catalog admission it already has. The staging
directory layout, the review UX, the loader, the sandbox, and the skill host are all
application territory — consistent with "vsigma never loads code" (dynamic-tools §B.5),
which this proposal does not bend: even here, the `import` happens in the app's loader
or the app's skill host, never in the library.

## C. Non-goals / hard lines

- **vsigma importing or executing agent-authored code** — never. The library's
  contribution ends at contracts and validation of shape.
- **Auto-admission without a sandbox** — a gate that is a regex or an import allowlist
  is theater; if no sandbox exists, the gate is a human, full stop.
- **The agent approving its own drafts** in any guise — including a "reviewer" prompt
  run on the same model with the same context.
- **Hot-reload of the live plugin directory** — the staging/admitted split exists so
  the watched-and-loaded location is never agent-writable.
- **Skill sharing between agents** — a drafted skill admitted for one agent becoming
  visible to others is a multi-agent (Phase 5) question; the provenance tag is designed
  so that decision can be made later without archaeology.

## D. The path to autonomy — the gate as a pluggable principal

The operator's stated end goal is an agent that extends its own capability *without
intervention*. This section records how that goal and this proposal's hard lines
coexist, so the vision is on paper next to the discipline.

The reframe: this design's essence is **"the author never operates the gate"** — not
"a human sits in the loop." The human is today's gate because a human is the only
verifier we have; the pipeline was deliberately shaped so the gate is a *pluggable
principal*. Autonomy is reached by replacing the human gate with machine verification,
rung by rung, never by removing the gate. "Without intervention" is a goal;
"without verification" is not.

The ladder, by risk class of the authored artifact:

| rung | artifact | gate | status |
|---|---|---|---|
| 1. knowledge | facts distilled from runs | none needed — a wrong fact degrades output, it doesn't execute; confidence tiers + supersede are the correction loop | **autonomous today** (distill → 0.3 → deep round → 0.9) |
| 2. procedures | instruction-only SKILL.md packages extracted from trajectories | mechanical and honestly automatic — "text only, no scripts, no collisions" is machine-decidable; worst case is bad advice, the same risk class as rung 1 | designed: the procedure-extraction proposal |
| 3. code | executable skills (this proposal) | a *real* sandbox harness: the draft's declared tests run isolated and resource-bounded; green auto-admits into the skill host under least privilege; misbehavior auto-`disable`s | human gate now; automation is an engineering project, not a philosophy change |

Two properties hold at every rung, including full rung-3 autonomy:

- **The gate mechanism stays outside the agent's write access.** The dangerous loop
  is not "agent writes a buggy tool" — it is "agent extends its capability to extend
  capabilities": editing gate policy, writing into admitted directories, granting
  itself the exec tool. Breaking that loop structurally is what makes every dial
  below it safe to turn.
- **Provenance and revocation never wash out.** However automatic admission becomes,
  `source="agent-authored:…"` remains visible everywhere and `disable` remains
  instant — autonomy raises the admission rate, never the audit opacity.

Practical consequence: most of the *felt* autonomy arrives at rung 2, which crosses
no hard line in §C. Rung 3's automated gate is worth building only after rung 2 is
live and the sandbox can be done seriously (§B.2's warning stands: an import
allowlist is not a sandbox).

## E. Graduation criteria

- On a live run: the agent drafts a skill with a rationale, the run completes normally
  without it, the operator reviews and admits it, and a *subsequent* run calls it —
  with `source="agent-authored:…"` visible in the catalog.
- A rejected draft never becomes callable, and its bundle survives in `rejected/` as
  the audit record.
- The boundary test: the agent, given its full toolset, cannot cause code it wrote to
  execute without the gate — attempted shortcuts (writing into the admitted directory,
  requesting its own draft) fail closed.
- Revocation observed live: `disable` removes the skill from the next step's exposure.

When validated, this graduates beside dynamic-tools' "capability admission" section in
architecture_design, as its second half: facts and code, one discipline.
