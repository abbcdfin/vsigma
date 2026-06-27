# Checkpoint

> Volatile session-handoff memory (per `.vibe/vibe.md`). For the durable roadmap see
> [`plan.md`](./plan.md); for settled design see `docs/`. Last updated: **2026-06-27**.

## Where we are

- **Branch:** `dev` (all work lives here). Tags: `phase-0`, `phase-1-mvp`, `phase-2`.
- **Status:** **Phase 2 complete.** Routing/planning/tool-extensibility blocks landed:
  `estimate_effort` (conditional-edge complexity routing), `plan_node` (Plan-and-Solve), and
  the **MCP tool adapter** (`make_mcp_tools` → `MCPToolset`, async→sync bridge, stdio +
  in-memory transports). `make check` green (17 tests).
- **Latest commits:** … `ce37e46` Phase 1 MVP → `867bc70` examples refactor + MCP proposal
  → `c880030` Phase 2 (estimation + planning) → `<phase-2 completion commit>`.

## Key context the next session needs

- **LLM is OpenAI-compatible, auth still pending.** Agent depends only on the `LLM` protocol;
  offline default `FakeLLM`; real model via `VSIGMA_LLM_BASE_URL` / `_API_KEY` / `_MODEL`.
  **Not yet smoke-tested against a live model/web.**
- **Layering:** core `vsigma/` is domain-agnostic (engine + generic `Tool` + framework infra
  like the MCP adapter). Domain tools + demo live in `examples/web_browsing/`.
- **`Tool` is the spine; MCP is a source** adapted into `Tool`s — no engine change.
  `Router` lives in `routing.py` (shared by resilience + estimation).
- **Proposals (`docs/proposals/`):** `composition-model` **VALIDATED**, `mcp-tool-adapter`
  **VALIDATED** (both ready to graduate), `memory-construction` (Phase 4, not started).
- **Conventions:** `uv`; mypy strict (LangGraph + async-bridge casts isolated); `make check`.

## Open / pending

- **Graduate** the VALIDATED `composition-model` and `mcp-tool-adapter` proposals into
  `docs/architecture_design.md` (consolidation pass).
- **Live smoke test** against a real OpenAI-compatible model + web once auth is provided.

## Next Steps

1. **Start Phase 3** — Critique/Reflexion block (`critic_node` + deterministic validator) and
   the software-engineering worked example end-to-end. Open `vdesign/active_plan.md` for the
   JIT breakdown.
2. **(When auth arrives)** smoke-test `OpenAICompatibleLLM` + web tools live.
3. **Consolidate** the two validated proposals into `architecture_design.md`.
