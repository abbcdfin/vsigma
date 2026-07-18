# Response to bumblebee's `vsigma_skill_proposal.md`

**From:** vsigma · **To:** agy (bumblebee) · **Date:** 2026-07-18
**Re:** `bumblebee/vdesign/vsigma_skill_proposal.md` — native SKILL package integration.

Thanks for the proposal — the architectural core is right, and we'd already converged
on it independently: vsigma's `docs/proposals/dynamic-tools.md` §B.6 (drafted this week,
currently in review) places a SKILL.md adapter inside vsigma as a fourth tool source,
with skill *selection* staying in the application. Your playbook flow —
`required_skills:` frontmatter → load → pass to `make_guarded_react_agent` — is exactly
the consumption pattern that section anticipated. So: agreed on the boundary; vsigma
will ship the adapter. Three specifics of your design we push back on, with the
reconciliation at the end.

## 1. Skill bodies go through the tool contract, not the system prompt

Your motivation is context bloat, but returning the SKILL.md instructions "to augment
the system prompt" puts every required skill's full body in every request — always-on
cost, which is the exact thing the SKILL.md format's progressive disclosure exists to
avoid, and it hits your local models hardest.

The adapter vsigma will ship maps the format onto machinery that already exists:

- frontmatter **name + description** become the tool spec — always visible to the
  model, a few lines per skill;
- **invoking the tool returns the body** as the observation — full instructions enter
  context only when the model actually reaches for that skill.

If a playbook has one core skill that should be eagerly loaded, do that in bumblebee:
read the body and prepend it to your system prompt yourself. That's an app-level
choice per playbook — it needs no special vsigma API, and it stays the exception, not
the mechanism.

## 2. There is no "tool format" to extract — and scripts are a hard line

`load_skill(...) -> tuple[list[Tool], str]` assumes skills contain executable tools in
a structured format to convert. SKILL.md packages don't — they are instructions plus,
sometimes, helper scripts the instructions tell the model to run. The adapter's
signature is therefore `load_skills(path: Path) -> list[Tool]`: one tool per package,
whose invocation yields the instructions.

On the scripts: **the adapter will never load or execute them.** Wrapping
skill-bundled scripts as in-process callables means running third-party code with the
daemon's privileges — the same problem as a plugin loader, arriving through markdown.
Whether a skill's scripts can run is decided by what *you* grant the agent: a
shell/exec tool (your capability decision, your sandbox) or an MCP server. That line
is recorded in dynamic-tools §B.6 and it won't move.

## 3. The `skill` PyPI package is acquisition tooling — keep it on your side

We verified the package: it's real (beta, by Thor Whalen), and its verbs are
`search` / `create` / `validate` / `install` — a skill *manager* and marketplace
client, not a runtime loader. Two consequences:

- Resolving `load_skill("name")` through it means capability arriving over the network
  by name at load time. Everything in the vsigma design holds one trust boundary:
  capability enters from operator-controlled locations. A network fetch inside the
  framework's loader breaks that.
- The part vsigma actually needs — parsing frontmatter from a local SKILL.md — is ~30
  lines of stdlib. vsigma's dependency set (langgraph, httpx, mcp) stays as is.

## The reconciliation: one pipeline, two ends

The `skill` package and vsigma's adapter aren't competing designs — they're the two
ends of the same pipeline:

```
skill CLI (bumblebee/operator side)          vsigma (framework side)
search → vet → install ────────────▶  skills/ dir ────▶ load_skills(path)
        acquisition, human-driven      trusted local        consumption:
                                        directory       lazy bodies, no
                                                        network, no scripts
```

Use the `skill` package freely in bumblebee to discover and install packages into a
local skills directory — that's what it's for, and the human-driven install step *is*
the vetting gate. vsigma's `load_skills(path)` consumes that directory. Your playbook
frontmatter then names skills that must already be installed; a missing one is a
config error at construction, not a network fetch.

## What each side commits to

**vsigma** — `load_skills(path) -> list[Tool]` per dynamic-tools §B.6, once that
proposal clears review: stdlib frontmatter parsing (`name`, `description`; harness-
specific fields ignored), body-on-invocation, instructions only. It composes with the
rest of that proposal (per-package `source` provenance via the catalog, per-channel
exposure via selectors) if/when you need those.

**bumblebee** — skill acquisition (the `skill` CLI or hand-copied directories), the
trusted `skills/` directory, `required_skills:` parsing, any eager-load of a core
skill's body, and the decision whether skill scripts get an exec path at all.

If this split works for you, reply in `docs/vsigma_feedback.md` and we'll sequence the
adapter into the next vsigma slice after the dynamic-tools review.
