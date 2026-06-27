# Checkpoint

> Volatile session-handoff memory (per `.vibe/vibe.md`). For the durable roadmap see
> [`plan.md`](./plan.md); for settled design see `docs/`. Last updated: **2026-06-27**.

## Where we are

- **Branch:** `dev` (all work lives here). Tags: `phase-0`, `phase-1-mvp`.
- **Status:** **Phase 1 MVP complete and committed.** A runnable web-browsing agent is
  assembled from the core blocks; quality gate (`make check` = ruff + mypy strict + 12 tests)
  is green; demo runs offline at `uv run python -m examples.web_browsing.demo`.
- **Latest commits:** `f7f1630` Phase 0 → `9049b85` memory proposal → `ce37e46` Phase 1 MVP
  → `867bc70` examples refactor + MCP proposal.

## Key context the next session needs

- **LLM is OpenAI-compatible, auth still pending.** The agent depends only on the `LLM`
  protocol. Offline default is `FakeLLM`; real model activates when
  `VSIGMA_LLM_BASE_URL` / `VSIGMA_LLM_API_KEY` / `VSIGMA_LLM_MODEL` are set (no code change).
  **The user will provide credentials later** — not yet smoke-tested against a live model/web.
- **Layering decided:** core `vsigma/` stays domain-agnostic (engine + generic `Tool`
  abstraction). Concrete domain tools + the demo live in `examples/web_browsing/`.
- **Tools vs MCP:** `Tool` is the uniform in-process contract; MCP is an external *source*
  adapted into `Tool`s (`make_mcp_tools`, no engine change). MCP not yet implemented.
- **Proposals in `docs/proposals/`:** `composition-model` (**VALIDATED — ready to graduate**),
  `memory-construction` (Phase 4), `mcp-tool-adapter` (Phase 2). Nothing has graduated into
  `architecture_design.md` yet.
- **Conventions:** Python via `uv`; mypy strict (LangGraph-boundary casts isolated to
  `builder.py`/`run.py`); `make check` is the gate.

## Open / pending

- Graduate the **composition-model** proposal into `docs/architecture_design.md`
  (consolidation pass; the seam was validated by the MVP).
- Smoke-test against a **live OpenAI-compatible model + real web** once auth is provided.

## Next Steps

1. **(When auth arrives)** set the `VSIGMA_LLM_*` env vars and run the demo against a live
   model + real site to validate `OpenAICompatibleLLM` and the web tools end-to-end.
2. **Graduate the composition model** into `architecture_design.md` (then flip the proposal
   status to GRADUATED).
3. **Start Phase 2** — effort-estimation routing, Plan-and-Solve, and the MCP tool adapter
   (`vsigma/mcp.py`). Open `vdesign/active_plan.md` for the JIT breakdown when starting.
