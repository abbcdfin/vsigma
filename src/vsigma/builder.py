"""The assembler — `GraphBuilder` and the compiled `Agent`.

`GraphBuilder` is vsigma's single composition tool: blocks are added as nodes,
wiring is edges, and the cross-cutting services (context, memory, observability)
are injected at `compile()` and woven around every node by the wrapper seam — so
node authors write only reasoning + action. `compile()` returns the loop; the
loop's `run()` executes one task.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .context import ContextManager, PassthroughContext
from .memory import MemoryStore
from .observability import Observer, TrajectoryEvent
from .resilience import Router
from .run import Run
from .state import NodeFn, State


def _namespace(state: State) -> str:
    return str(state.get("session_id") or state.get("task_id"))


def _summarize(node: str, update: State) -> str:
    return f"{node}: status={update.get('status')}"


class Agent:
    """A compiled loop: a reusable definition you execute via `run()`."""

    def __init__(
        self,
        graph: CompiledStateGraph[Any, Any, Any, Any],
        memory: MemoryStore,
        observer: Observer,
    ) -> None:
        self.graph = graph
        self.memory = memory
        self.observer = observer

    def run(
        self,
        task: str,
        *,
        session_id: str | None = None,
        system: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> Run:
        rid = run_id or uuid4().hex
        tid = task_id or rid
        messages: list[dict[str, object]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": task})
        initial: State = {
            "task_id": tid,
            "run_id": rid,
            "session_id": session_id,
            "parent_run_id": None,
            "attempt_count": 0,
            "status": "running",
            "messages": messages,
        }
        run = Run(self, rid, tid, session_id)
        output = self.graph.invoke(initial, cast("Any", run.config))
        run._record(cast("State", output))
        return run


class GraphBuilder:
    def __init__(self, state_schema: type) -> None:
        self._schema = state_schema
        self._nodes: dict[str, NodeFn] = {}
        self._edges: list[tuple[str, str]] = []
        self._conditionals: dict[str, Router] = {}
        self._entry: str | None = None

    def add(self, name: str, fn: NodeFn) -> GraphBuilder:
        self._nodes[name] = fn
        if self._entry is None:
            self._entry = name
        return self

    def set_entry(self, name: str) -> GraphBuilder:
        self._entry = name
        return self

    def add_edge(self, source: str, target: str) -> GraphBuilder:
        self._edges.append((source, target))
        return self

    def route_conditional(self, source: str, router: Router) -> GraphBuilder:
        self._conditionals[source] = router
        return self

    def compile(
        self,
        *,
        memory: MemoryStore,
        observer: Observer,
        context: ContextManager | None = None,
        checkpointer: object | None = None,
    ) -> Agent:
        if self._entry is None:
            raise ValueError("graph has no entry node; add() at least one node")
        ctx = context or PassthroughContext()
        graph: StateGraph[Any, Any, Any, Any] = StateGraph(self._schema)
        for name, fn in self._nodes.items():
            graph.add_node(name, cast("Any", self._wrap(name, fn, ctx, memory, observer)))
        graph.add_edge(START, self._entry)
        for source, target in self._edges:
            graph.add_edge(source, target)
        for source, router in self._conditionals.items():
            graph.add_conditional_edges(
                source, cast("Any", router.fn), cast("Any", router.mapping)
            )
        compiled = graph.compile(checkpointer=cast("Any", checkpointer or MemorySaver()))
        return Agent(compiled, memory, observer)

    @staticmethod
    def _wrap(
        name: str,
        fn: NodeFn,
        context: ContextManager,
        memory: MemoryStore,
        observer: Observer,
    ) -> NodeFn:
        """Weave the cross-cutting services around a node (the wrapper seam)."""

        def wrapped(state: State) -> State:
            window = context.assemble(state, memory)
            update = fn(window)
            attempt = int(str(state.get("attempt_count", 0)))
            session = state.get("session_id")
            memory.write(_namespace(state), f"{state.get('run_id')}:{name}:{attempt}", update)
            observer.emit(
                TrajectoryEvent(
                    run_id=str(state.get("run_id")),
                    task_id=str(state.get("task_id")),
                    session_id=None if session is None else str(session),
                    node=name,
                    attempt=attempt,
                    summary=_summarize(name, update),
                )
            )
            return update

        return wrapped
