# Proposal: Promote the Async Bridge to Public API

**Status:** ACCEPTED 2026-07-19 — reviewed same day. Amended in review: `run()` cancels
the coroutine on timeout; `wrap()` defaults to a finite timeout (`run()` stays `None`).
**Prompted by:** bumblebee's Playwright skills. Playwright's sync API refuses to run
where an asyncio loop is live, and its async API binds browser state to the loop that
created it — so bumblebee built a dedicated background thread running a private event
loop, with sync tool functions submitting coroutines via
`asyncio.run_coroutine_threadsafe`. That is, line for line, the architecture of
`_AsyncBridge` in `mcp.py` — the private class vsigma already uses to serve the async
MCP protocol through the sync `Tool` contract. The pattern has now been implemented
twice, independently, against two different async libraries. Two consumers is the
promotion trigger.

## A. Problem

vsigma's execution path is deliberately synchronous: `Tool.func` is `(args) -> str`,
`react_node` calls it inline, and nothing in the agent loop touches asyncio. That
contract is right (see §C), but async-only libraries exist — MCP, Playwright, most
modern HTTP clients — and every consumer that meets one faces the same three-way trap:

1. **`asyncio.run()` per call** — the obvious wrapper, and a trap for stateful
   libraries: each call gets a fresh loop, and objects bound to the previous loop
   (a browser connection, an MCP session) die between calls. Works once, breaks
   on the second invocation.
2. **`async def` tools** — vsigma rejects them by contract, and rightly; accepting
   them would smuggle variant 1 in behind the adapter.
3. **The dedicated loop thread** — the correct answer: one long-lived loop in its own
   thread owns all the library's state; sync callers submit coroutines and block on
   the future. Isolated by construction — no caller's loop (or absence of one) can
   conflict with it.

vsigma knows the correct answer — `_AsyncBridge` is variant 3 — but keeps it private
inside `mcp.py`, so every consumer rediscovers it from scratch. Bumblebee's rediscovery
was sound, but hand-rolled versions predictably miss the two hard details: a timeout on
the blocking `.result()` (a hung coroutine otherwise freezes the agent run with no
recourse) and an explicit shutdown (a never-stopped daemon thread means the wrapped
library dies rudely at process exit — invisible in a CLI run, real in a long-running
service).

## B. Design

### 1. `AsyncBridge` moves to its own stdlib-only module

New module `src/vsigma/bridge.py`; `mcp.py` imports from it. The move matters beyond
naming: `mcp.py` imports the `mcp` package, and a consumer bridging Playwright should
not drag MCP dependencies in to get the loop thread. The bridge is pure stdlib
(`asyncio`, `threading`, `concurrent.futures`) and stays that way.

The class is today's `_AsyncBridge` with two additions:

```python
class AsyncBridge:
    """A dedicated background event loop for running coroutines from sync code.

    One bridge = one loop = one thread. Objects created on the loop (browser
    connections, client sessions) live as long as the bridge does — this is the
    supported way to hold async-library state behind sync vsigma tools.
    """

    def run(self, coro, *, timeout: float | None = None): ...   # NEW: timeout, cancels on expiry
    def submit(self, coro) -> Future: ...
    def call_soon(self, fn) -> None: ...
    def shutdown(self) -> None: ...
    def __enter__(self) -> AsyncBridge: ...                     # NEW: context manager
    def __exit__(self, *exc) -> None: ...                       # calls shutdown()
```

- **`timeout` on `run`** — `run_coroutine_threadsafe(...).result(timeout)`, and on
  expiry the bridge **cancels the coroutine** (`future.cancel()`, which cancels the
  underlying asyncio task) before re-raising. The contract is "the operation was
  abandoned", not "we stopped waiting" — without the cancel, a hung coroutine keeps
  running on the loop and leaves the wrapped library in an unknown state for the next
  call. Default `None` preserves current behavior (and MCP's semantics, unchanged);
  `run` is the low-level primitive — the tool-facing default lives on `wrap` (§B.2).
- **Context-manager support** — `with AsyncBridge() as bridge:` makes the shutdown
  story structural instead of remembered, mirroring `MCPToolset`'s existing
  `with make_mcp_tools(...) as ts:` shape.

### 2. `wrap` — the five lines every consumer was going to write

The consumer's actual need is "async function in, sync `Tool.func`-shaped callable
out, all sharing one loop":

```python
def wrap(self, fn, *, timeout: float | None = 120.0):
    """Return a sync callable that runs `fn(...)` on this bridge's loop."""
    def sync_fn(*args, **kwargs):
        return self.run(fn(*args, **kwargs), timeout=timeout)
    return sync_fn
```

`wrap` defaults to a **finite** timeout (120s) where `run` defaults to `None`: `wrap`
is by definition the tool-facing path, and a tool step that blocks forever is strictly
worse than a spurious timeout — the agent can retry an error observation; a frozen run
cannot recover (the same soft-failure philosophy as `react_node`'s unknown-tool
handling). Legitimately unbounded operations opt out with `timeout=None`.

With it, bumblebee's Playwright skill collapses to the intended shape:

```python
from vsigma import AsyncBridge, tool

bridge = AsyncBridge()

@tool
def browser_navigate(url: str) -> str:
    """Navigate the browser to a URL."""
    return bridge.wrap(_navigate_async, timeout=60.0)(url)
```

(or `func=bridge.wrap(...)` when building `Tool` objects directly). No decorator
magic beyond what exists; `wrap` composes with `@tool`, `from_langchain`, and the
catalog exactly because it produces an ordinary sync callable.

### 3. `mcp.py` refactors onto the public class

`_AsyncBridge` is deleted; `MCPToolset` and `make_mcp_tools` take `AsyncBridge` from
`bridge.py`. Behavior is byte-identical (MCP passes no timeouts today — its blocking
points already have their own: `ready.result(timeout=30.0)`, `serve.result(timeout=5.0)`).
This is the refactor that proves the public surface is sufficient: if MCP needs a
private hook the public class lacks, the design is wrong.

### 4. Exported, documented as the pattern

`AsyncBridge` joins `__init__.py` exports. `getting_started.md` gains a short
"async libraries behind sync tools" recipe naming the trap explicitly: *never*
`asyncio.run()` inside a tool function; one bridge per async library (or per isolation
domain), created at construction time next to the tools that use it, shut down when
the agent host exits.

## C. Non-goals / hard lines

- **A full async execution path** — `acomplete` on the LLM protocol, an async
  `react_node`, `ainvoke` through LangGraph, async `Session.send`. That is a parallel
  surface through every seam of the library, and its honest trigger is *concurrency*
  (the daemon serving simultaneous sessions, parallel tool fan-out) — the same trigger
  cluster as the SQLiteStore WAL slice, and like it, deferred until bumblebee actually
  goes concurrent. Notably, the bridge stays correct in that future: a stateful
  single-threaded library like Playwright wants its own loop even inside an
  async-native host.
- **`async def` accepted by `@tool` or `Tool.func`** — the contract stays sync. An
  async-looking convenience that secretly does per-call `asyncio.run()` is the trap
  from §A dressed as a feature.
- **A global or implicit bridge** — vsigma never creates one behind the consumer's
  back. Loop-thread lifetime is resource ownership; it stays explicit and app-side,
  like every other lifecycle in the library.
- **Thread-pool execution of sync tools** — unrelated concern, out of scope.

## D. Graduation criteria

- `mcp.py` runs on the public `AsyncBridge` with the existing MCP test suite green
  and no semantic change.
- A test proves the statefulness guarantee: an object created in one `bridge.run`
  call is usable from a later call (the property `asyncio.run()`-per-call breaks).
- A timeout test: a coroutine that never completes raises out of `run(timeout=...)`,
  the abandoned coroutine is actually cancelled (observable via a cancellation flag
  set in the test coroutine's `except CancelledError`), and the bridge remains usable
  for subsequent calls.
- Lifecycle: `with AsyncBridge() as b:` shuts the loop thread down on exit; `shutdown`
  is idempotent.
- The live proof: bumblebee's `skills/web_browsing.py` (or an equivalent Playwright
  demo) rebuilt on `AsyncBridge.wrap`, driving a real navigation through a vsigma
  agent — the hand-rolled bridge deleted in favor of the import.

When validated, this graduates into architecture_design's tool-adapter section as the
third adapter idiom: `@tool` for plain functions, `from_langchain`/`make_mcp_tools`
for ecosystems, `AsyncBridge.wrap` for async libraries.
