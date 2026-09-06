"""The cross-cutting seam — `Services` and `wrap`.

`wrap` is the core value as a composable primitive: it takes any graph node and
returns the same node with context-assembly (pre), memory-persist and
trajectory-emit (post) woven around it. It works on a *raw* LangGraph
`StateGraph` — vsigma augments LangGraph rather than replacing it.

`Services` bundles the injected memory/observability/context so `wrap` is not
repetitive.

The seam owns the trajectory row key, and every node firing must get its own
row. Three approaches failed before the current one, each for a different
reason, so the constraints are worth stating:

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

So the key carries no ordinal at all — just the label and a random suffix, which
needs no coordination and cannot collide under any concurrency. **Sequence comes
from `MemoryStore.query`, which returns rows in insertion order** (a guarantee
the protocol now states, and which `episode_trajectory`'s trailing window has
always depended on). `wrap` returns the node's update exactly as produced.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

from langgraph.graph.state import StateGraph

from .context import ContextManager, PassthroughContext
from .memory import MemoryStore
from .observability import Observer, TrajectoryEvent
from .routing import Router
from .scope import episode
from .state import NodeFn, State


@dataclass
class Services:
    """The cross-cutting services woven around nodes by `wrap`."""

    memory: MemoryStore
    observer: Observer
    context: ContextManager = field(default_factory=PassthroughContext)
    #: Optional run-end hook (e.g. `reflexive_consolidator(...)`), called at
    #: each terminal edge of a run: on escalation (state's `status` is the
    #: derived "escalated", possibly more than once across resumes) and once
    #: on completion with status "done". Per-status policy lives in the hook.
    run_end: Callable[[State, MemoryStore], None] | None = None


def _namespace(state: State) -> str:
    # Node updates are episodic context (the mechanical trajectory capture that
    # consolidation later reads); scoped per the running-context model.
    return episode(str(state.get("run_id"))).namespace("context")


def _summarize(node: str, update: State) -> str:
    return f"{node}: status={update.get('status')}"


def wrap(node: NodeFn, services: Services, *, name: str | None = None) -> NodeFn:
    """Weave the cross-cutting services around a node (the seam).

    `name` labels the node in memory writes and trajectory events; pass the same
    name you give `StateGraph.add_node`. Defaults to the node function's name.
    """
    label = name or cast("str", getattr(node, "__name__", "node"))

    def wrapped(state: State) -> State:
        window = services.context.assemble(state, services.memory)
        update = node(window)
        attempt = int(str(state.get("attempt_count", 0)))
        session = state.get("session_id")
        services.memory.write(_namespace(state), f"{label}:{uuid4().hex[:8]}", update)
        services.observer.emit(
            TrajectoryEvent(
                run_id=str(state.get("run_id")),
                task_id=str(state.get("task_id")),
                session_id=None if session is None else str(session),
                node=label,
                attempt=attempt,
                summary=_summarize(label, update),
            )
        )
        return update

    return wrapped


def add_wrapped(
    graph: StateGraph[Any, Any, Any, Any], name: str, node: NodeFn, services: Services
) -> None:
    """Add a seam-wrapped node to a `StateGraph` you own (augments, doesn't replace).

    Convenience over ``graph.add_node(name, wrap(node, services, name=name))`` that also
    smooths LangGraph's strict node typing.
    """
    graph.add_node(name, cast("Any", wrap(node, services, name=name)))


def add_router(graph: StateGraph[Any, Any, Any, Any], source: str, router: Router) -> None:
    """Add a `Router` (e.g. from `break_on_attempts`) as conditional edges from `source`."""
    graph.add_conditional_edges(source, cast("Any", router.fn), cast("Any", router.mapping))
