"""The async bridge — a dedicated background event loop for sync callers.

Design: docs/proposals/async-bridge.md. Async-only libraries (MCP, Playwright)
bind their state to the event loop that created it, so per-call ``asyncio.run``
breaks them on the second invocation. `AsyncBridge` owns one long-lived loop in
its own thread; sync code submits coroutines and blocks on the result — the
supported way to hold async-library state behind sync `Tool` functions. One
bridge = one loop = one thread; lifetime is explicit (`shutdown()`, or use it
as a context manager).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from types import TracebackType
from typing import Any, ParamSpec, TypeVar

_T = TypeVar("_T")
_P = ParamSpec("_P")


class AsyncBridge:
    """A background event loop that runs coroutines for sync callers.

    Objects created on the loop (browser connections, client sessions) live as
    long as the bridge does. `run` and `submit` are thread-safe; `shutdown` is
    idempotent.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def run(self, coro: Coroutine[Any, Any, _T], *, timeout: float | None = None) -> _T:
        """Run `coro` on the bridge loop and block for its result.

        On timeout the coroutine is *cancelled* before `TimeoutError` is
        re-raised — the operation is abandoned, not left running against the
        wrapped library's state.
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    def submit(self, coro: Coroutine[Any, Any, _T]) -> Future[_T]:
        """Schedule `coro` on the bridge loop; return its future without blocking."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def call_soon(self, fn: Callable[[], object]) -> None:
        """Schedule a plain callable on the bridge loop."""
        self._loop.call_soon_threadsafe(fn)

    def wrap(
        self,
        fn: Callable[_P, Coroutine[Any, Any, _T]],
        *,
        timeout: float | None = 120.0,
    ) -> Callable[_P, _T]:
        """Return a sync callable that runs ``fn(...)`` on this bridge's loop.

        The tool-facing path, so the timeout defaults *finite*: a tool step
        that blocks forever is worse than a spurious timeout error — the agent
        can retry an error observation, a frozen run cannot recover. Pass
        ``timeout=None`` for legitimately unbounded operations.
        """

        def sync_fn(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            return self.run(fn(*args, **kwargs), timeout=timeout)

        return sync_fn

    def shutdown(self) -> None:
        """Stop the loop and join the thread. Safe to call more than once."""
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        self._loop.close()

    def __enter__(self) -> AsyncBridge:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.shutdown()
