"""AsyncBridge — the public loop-thread bridge (async-bridge proposal §D)."""

from __future__ import annotations

import asyncio

import pytest

from vsigma import AsyncBridge


def test_run_returns_coroutine_result() -> None:
    async def add(a: int, b: int) -> int:
        return a + b

    with AsyncBridge() as bridge:
        assert bridge.run(add(2, 3)) == 5


def test_state_survives_across_calls() -> None:
    # The property per-call asyncio.run() breaks: an object created on the
    # loop in one call must be usable from a later call.
    with AsyncBridge() as bridge:
        queue = bridge.run(_make_queue())

        async def put() -> None:
            await queue.put("hello")

        async def get() -> str:
            return await queue.get()

        bridge.run(put())
        assert bridge.run(get()) == "hello"


async def _make_queue() -> asyncio.Queue[str]:
    return asyncio.Queue()


def test_timeout_raises_cancels_and_bridge_stays_usable() -> None:
    cancelled = asyncio.Event()

    async def hang() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def was_cancelled() -> bool:
        await asyncio.wait_for(cancelled.wait(), timeout=5.0)
        return True

    async def quick() -> str:
        return "still alive"

    with AsyncBridge() as bridge:
        with pytest.raises(TimeoutError):
            bridge.run(hang(), timeout=0.05)
        # The abandoned coroutine was actually cancelled, not left running.
        assert bridge.run(was_cancelled())
        assert bridge.run(quick()) == "still alive"


def test_wrap_produces_sync_callable() -> None:
    async def greet(name: str, *, punct: str = "!") -> str:
        return f"hello {name}{punct}"

    with AsyncBridge() as bridge:
        sync_greet = bridge.wrap(greet)
        assert sync_greet("world", punct="?") == "hello world?"


def test_wrap_default_timeout_is_finite() -> None:
    async def hang() -> None:
        await asyncio.sleep(60)

    with AsyncBridge() as bridge:
        hung = bridge.wrap(hang, timeout=0.05)
        with pytest.raises(TimeoutError):
            hung()


def test_shutdown_is_idempotent_and_context_manager_shuts_down() -> None:
    bridge = AsyncBridge()
    with bridge:
        pass
    bridge.shutdown()  # second shutdown must be a no-op, not an error
    assert bridge._loop.is_closed()
    assert not bridge._thread.is_alive()
