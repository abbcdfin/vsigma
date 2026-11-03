# Active Plan — Phase 4.18: `mcp` 1 and 2

*Design: [`docs/proposals/open-source-groundwork.md`](../docs/proposals/open-source-groundwork.md) §B.6.*

## Steps

- [x] **1. Widen `mcp` dependency and re-lock (`pyproject.toml`, `uv.lock`)**
  - Update `mcp>=1.28.1,<3` in `pyproject.toml` and run `uv sync` / `uv lock` so the lock resolves `mcp` 2.x.

- [x] **2. Support `mcp` 1 and 2 in the adapter (`src/vsigma/mcp.py`)**
  - Read `mcp_tool.input_schema` falling back to `mcp_tool.inputSchema` in `_wrap`.
  - Read `result.is_error` falling back to `result.isError` in `_result_to_text`.

- [x] **3. Dual-major test server and `make check` (`tests/test_mcp.py`, `Makefile`)**
  - Build the sample server in `tests/test_mcp.py` on either major (`MCPServer` from `mcp.server.mcpserver` with `Client(server).session` on 2.x; `FastMCP` with `create_connected_server_and_client_session` on 1.x).
  - Add a tool that raises and assert its result starts with `"error:"`.
  - Update `Makefile` (`test` / `check`) so `tests/test_mcp.py` also runs on the floor (`uv run --with 'mcp==1.28.1' pytest tests/test_mcp.py`).

- [x] **4. Docs (`docs/architecture_design.md` §7, `CHANGELOG.md`, `vdesign/plan.md`, `vdesign/checkpoint.md`)**
  - Update `architecture_design.md` §7 and `CHANGELOG.md`.
  - Run `make check` and `uv lock --locked`, verify that dropping either field spelling fails one of the two runs.
