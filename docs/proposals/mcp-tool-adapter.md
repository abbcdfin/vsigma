# Proposal: MCP Tool Adapter

> **Status: GRADUATED (2026-07-04)** into
> [`architecture_design.md`](../architecture_design.md) §7 — kept as the historical record.
> Implemented in Phase 2 (`src/vsigma/mcp.py`) and proven by an in-memory integration test:
> MCP tools are wrapped as native `Tool`s and invoked with **no engine change**. Builds on
> the [Composition Model proposal](./composition-model.md) and the same adapter principle as
> the [memory adapter layer](./memory-construction.md). The **code is the source of truth**.
> Still open (tracked in the design doc): HTTP transport, pooling, MCP resources/prompts.
>
> **Implementation notes / refinements vs. this draft:**
> - Signature is `make_mcp_tools(connect: ConnectionFactory) -> MCPToolset` (not `-> list[Tool]`):
>   the toolset exposes `.tools` and a `close()`/context-manager for lifecycle.
> - `ConnectionFactory` abstracts the transport (stdio via `stdio_connection`, or in-memory for
>   tests), so the adapter and its tests share one path. HTTP transport remains future work.
> - Async→sync is bridged on a dedicated background event-loop thread; `Tool.func` stays sync.
> - Still open: HTTP transport, connection pooling, MCP resources/prompts.

## Motivation

vsigma commits to leaning on **MCP (Model Context Protocol)** as the primary path for tool
*transport and extensibility* (see the product/architecture docs). Today the framework only
supports in-process Python `Tool`s. This proposal adds MCP support **without changing the
engine**, by treating an MCP server as one more *source* behind the uniform `Tool` contract.

## The model

### `Tool` is the spine; MCP is a source

The loop (`react_node`, the LLM spec) depends only on the in-process `Tool` contract. MCP is
an external protocol/transport that *exposes* tools from a separate process. They sit at
different layers — MCP tools are **adapted into `Tool` objects**, never a parallel path.

```
hand-written Python fn ─┐
MCP server tools ───────┼─▶ [adapter] ─▶ Tool ─▶ react_node / LLM
OpenAPI spec (later) ───┘
        sources                uniform contract        the loop
```

This is the same adapter pattern as memory (one contract, many sources/backends): the engine
never learns about MCP.

### The adapter

A thin function that connects to an MCP server, lists its tools, and wraps each as a `Tool`
whose `func` performs the MCP RPC call and returns the result as an observation string:

```python
def make_mcp_tools(server: MCPServerConfig) -> list[Tool]:
    # 1. connect (stdio / HTTP)
    # 2. list_tools() -> for each, build a Tool:
    #      - name/description/parameters from the MCP tool schema
    #      - func = lambda args: call_tool(name, args) -> str
    ...
```

The resulting `Tool`s are attached and **scoped exactly like native tools** (context-management
tool-scoping), including for spawned subagents.

### Placement

The MCP adapter is **generic framework infrastructure** (part of vsigma's extensibility
promise), so it belongs in the **toolkit/core** — e.g. `vsigma/mcp.py` or, if a standard
tools package later exists, `vsigma/tools/mcp.py`. This is distinct from domain-specific tools
(e.g. the web-browsing tools), which are examples.

## Scope & boundary (Minimal Mechanism)

* vsigma owns the **adapter** (schema mapping + RPC-to-observation), not an MCP implementation
  — lean on an existing MCP client library for the protocol.
* Start read/call of MCP **tools**; MCP **resources/prompts** are out of scope for the first
  cut.
* Lifecycle (connection pooling, auth, retries) reuses existing infra where possible.

## Open questions (to settle during implementation)

* Sync vs. async tool calls (the engine is currently sync) — may need an async path.
* Connection lifecycle: per-agent, per-run, or pooled?
* How tool errors/timeouts surface as observations (graceful-recovery contract).
* Which MCP client library to depend on.

## Graduation criteria

Promote into [`architecture_design.md`](../architecture_design.md) only after:

* `make_mcp_tools` wraps a real MCP server's tools as `Tool`s and an agent calls them with
  **no engine change**.
* The wrapped tools scope and trace identically to native tools.
* Tool errors/timeouts surface cleanly as observations the agent can reason about.
