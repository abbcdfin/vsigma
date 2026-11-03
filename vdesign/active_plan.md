# Active Plan — Phase 4.19: Tool Sources (Unique Names & Bounded MCP Calls)

*Design: [`docs/proposals/tool-sources.md`](../docs/proposals/tool-sources.md).*

## Steps

- [x] **1. Unique tool names in `react_node` (`src/vsigma/react.py`)**
  - Beside the `isinstance(candidate, Tool)` check, reject duplicate tool names with a `ValueError` naming the clashing tool and instructing the caller to rename one with `dataclasses.replace(t, name=...)`.

- [x] **2. Bounded MCP calls (`src/vsigma/mcp.py`)**
  - Add `*, timeout: float | None = 120.0` to `make_mcp_tools(connect, *, timeout=120.0)` and forward to `_wrap`.
  - Pass `timeout=timeout` to `bridge.run(session.call_tool(name, arguments), timeout=timeout)`; on `TimeoutError`, return `f"error: MCP tool '{name}' timed out after {timeout}s"`.

- [x] **3. Tests (`tests/test_tooling.py`, `tests/test_mcp.py`)**
  - Test that `react_node` and all four builders (`make_react_agent`, `make_guarded_react_agent`, `make_plan_execute_agent`, `make_reflexion_agent`) refuse duplicate tool names with `ValueError` naming the tool, and that renaming an MCP tool with `dataclasses.replace(t, name=...)` works and still invokes the server's tool.
  - In `tests/test_mcp.py` (running on both `mcp` 1 and 2): test a server tool that sleeps past a short `timeout` returns `"error: MCP tool '<name>' timed out after <timeout>s"`, the turn finishes, the session is not busy afterwards, the next call on the same toolset succeeds, and `timeout=None` waits.

- [x] **4. Docs (`docs/architecture_design.md` §7, `docs/getting_started.md`, `docs/upgrading.md`, `CHANGELOG.md`, `vdesign/plan.md`, `vdesign/checkpoint.md`)**
  - Update docs and verify `make check` and `uv lock --locked`.
