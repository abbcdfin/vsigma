# Proposal: Tool Sources — Unique Names and Bounded MCP Calls

**Status:** **GRADUATED** 2026-11-03 as Phase 4.19; ships in `v0.4.0`.
**Amends:** `architecture_design.md` §7. Every source ends as a vsigma `Tool` in one list, so
the agent loop can't tell sources apart. Two checks belong where the list is built.

## A. Problem

1. **Duplicate tool names are accepted silently.** `react_node` keys its tools by name
   (`react.py:30`), so the later of two same-named tools wins, and the model is offered both
   specs under one name. Reproduced on `131376c`: a local `search` and an MCP `search` both
   reach the model, and every call runs the MCP one. MCP servers choose their own names, so an
   app that mixes sources meets this without making a mistake of its own.
2. **MCP calls have no timeout.** `_wrap` calls `bridge.run(session.call_tool(...))` with none,
   and `mcp`'s `ClientSession` defaults to none. A hung server holds the turn, and with it the
   session's thread lock, so every later turn on that session raises `SessionBusy`. Async tools
   already get `bridge.wrap(fn, timeout=120.0)`.

## B. Design

1. **`react_node` refuses duplicate names**, beside its type check, with a `ValueError` that names
   the tool and says to rename one. All four agent builders pass through it, so the check runs
   when the agent is built. `Tool` is a frozen dataclass, so `dataclasses.replace(t, name=...)`
   renames one; a renamed MCP tool still calls the server's tool, whose name the wrapper keeps.
2. **`make_mcp_tools(connect, *, timeout=120.0)`** bounds each call, the same default as
   `bridge.wrap`. On expiry the bridge cancels the call, and the tool returns
   `error: MCP tool '<name>' timed out after <timeout>s` for the model to read. The connection
   stays open for the next call. `timeout=None` waits as before.

## C. Limits

- **MCP tools can't tell which user is asking.** A tool receives only its arguments, over one
  connection shared by every session on the agent; `run_context()` objects never reach the
  server. In a multi-user app, MCP suits shared tools, and a tool that acts as a particular user
  stays the app's own `Tool`. `architecture_design.md` §7 and `getting_started.md` say so.
- **Nothing reconnects** after a server dies; §7 lists it as still open.

## D. Upgrading

Breaking, released in `v0.4.0`:

- an agent given two tools with one name now fails to build; rename one;
- an MCP call slower than 120 s now returns a timeout error; pass `timeout=` to `make_mcp_tools`,
  or `None` for no limit.

## E. Graduation

- Each agent builder refuses two tools with one name, naming it. Renaming one with
  `dataclasses.replace` builds, and the renamed MCP tool still reaches its server tool.
- An MCP tool that outlasts `timeout` returns the timeout error, the turn finishes, the session
  isn't left busy, and the next call on the same toolset succeeds. `timeout=None` waits.
  `tests/test_mcp.py` covers both, on both `mcp` majors.
- The documents carry the identity limit (§C).

Met 2026-11-03 by `tests/test_tooling.py` and `tests/test_mcp.py`, on `mcp` 1.28.1 and 2.3.0. A
server reply that arrives after its call timed out is dropped, and the connection keeps working.
