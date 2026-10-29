"""Interception points — `Interceptor` and `wrap`.

`wrap` is the core value as a composable primitive: it takes any graph node and
returns the same node with context-assembly (pre) and memory-persist (post)
woven around it at interception points. It works on a *raw* LangGraph
`StateGraph` — vsigma augments LangGraph rather than replacing it.

`Interceptor` bundles the injected memory/observability/context so `wrap` is not
repetitive.

The interceptor owns the episodic context row key, and every node firing must get its
own row. Three approaches failed before the current one, each for a different reason,
so the constraints are worth stating:

* **`attempt_count`** cannot be borrowed: `human_interrupt_node` resets it on
  resume so the agent gets a fresh budget (`resilience.py`), and a resettable
  counter in a storage key silently overwrites everything written before the
  reset.
* **A counter in graph state** breaks fan-out — LangGraph rejects two writes to
  an ordinary channel in one superstep even when the values match — and vanishes
  under a consumer's own schema, which filters out fields it does not declare.
  Graph state belongs to the consumer.
* **A counter in the store** still races: `Send` can fan out to the *same* node,
  and a read-then-write allocator hands both firings the same ordinal.

So the key carries no ordinal at all — just the label and a full `uuid4`, which
needs no coordination and cannot collide under any concurrency. The suffix is
not truncated: eight hex digits would be 32 bits, i.e. a ~1% birthday collision
across 9,300 firings of one node in one episode, and a collision here silently
overwrites a trajectory row — the exact failure this key exists to prevent.
**Sequence comes from `MemoryStore.query`, which returns rows in insertion order**
(a guarantee the protocol states, and which `episode_trajectory`'s trailing window
has always depended on). `wrap` returns the node's update minus any message the node was handed.
Memory is optional (`memory=None` by default), in which case capture writes and
episode index writes are skipped.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

from langchain_core.messages import convert_to_openai_messages
from langgraph.graph.state import StateGraph

from .context import ContextManager, PassthroughContext
from .memory import MemoryStore
from .observability import Observer
from .routing import Router
from .scope import episode
from .state import NodeFn, State


@dataclass(kw_only=True)
class Interceptor:
    """The cross-cutting interceptor woven around nodes at interception points."""

    memory: MemoryStore | None = None
    observer: Observer | None = None
    context: ContextManager = field(default_factory=PassthroughContext)
    #: Optional run-end hook (e.g. `reflexive_consolidator(...)`), called at
    #: each terminal edge of a run. Requires memory to be configured.
    run_end: Callable[[State, MemoryStore], None] | None = None

    def __post_init__(self) -> None:
        if self.run_end is not None and self.memory is None:
            raise ValueError("Interceptor.run_end hook configured without a memory store")


def _namespace(state: State) -> str:
    return episode(str(state.get("run_id"))).namespace("context")


def wrap(node: NodeFn, interceptor: Interceptor, *, name: str | None = None) -> NodeFn:
    """Weave context assembly and memory capture around a node.

    `name` labels the node in memory writes; pass the same name you give
    `StateGraph.add_node`. Defaults to the node function's name.
    """
    label = name or cast("str", getattr(node, "__name__", "node"))

    def wrapped(state: State) -> State:
        window = interceptor.context.assemble(state, interceptor.memory)
        update = node(window)

        capture_payload = update
        if isinstance(update, dict):
            update = dict(update)
            window_messages = window.get("messages")
            update_messages = update.get("messages")
            if isinstance(window_messages, list) and isinstance(update_messages, list):
                received = {id(m) for m in window_messages}
                update["messages"] = [m for m in update_messages if id(m) not in received]

            capture_payload = dict(update)
            messages = capture_payload.get("messages")
            if isinstance(messages, Sequence):
                capture_payload["messages"] = cast(
                    "list[dict[str, object]]",
                    convert_to_openai_messages(messages),
                )

        if interceptor.memory is not None:
            interceptor.memory.write(
                _namespace(state), f"{label}:{uuid4().hex}", capture_payload
            )
        return update

    return wrapped


def add_wrapped(
    graph: StateGraph[Any, Any, Any, Any],
    name: str,
    node: NodeFn,
    interceptor: Interceptor,
) -> None:
    """Add a node wrapped with the interceptor to a `StateGraph`."""
    graph.add_node(name, cast("Any", wrap(node, interceptor, name=name)))


def add_router(graph: StateGraph[Any, Any, Any, Any], source: str, router: Router) -> None:
    """Add a conditional edge from a `Router` to a `StateGraph`."""
    graph.add_conditional_edges(source, cast("Any", router.fn), cast("Any", router.mapping))
