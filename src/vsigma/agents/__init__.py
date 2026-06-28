"""Agent templates — one-line, opinionated, prebuilt topologies.

Templates are the "good start" tier: they compose the building blocks into a
known-good graph so you don't rebuild it by hand. Each returns an `Agent` whose
`.graph` is a real compiled LangGraph graph — inspect it, run it, or drop it into
a bigger graph as a node. Templates are built only on the public blocks, so
nothing here is hidden from you.

Naming: `make_*`, consistent with `make_mcp_tools` / `make_web_tools`.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph

from ..llm import LLM
from ..memory import InMemoryStore
from ..observability import TrajectoryLog
from ..planning import plan_node
from ..react import react_node
from ..resilience import HUMAN_INTERRUPT, break_on_attempts, human_interrupt_node
from ..run import Agent
from ..seam import Services, add_router, add_wrapped
from ..state import ReActState
from ..tools import Tool

__all__ = ["make_react_agent", "make_plan_execute_agent"]


def _services(services: Services | None) -> Services:
    return services or Services(memory=InMemoryStore(), observer=TrajectoryLog())


def make_react_agent(
    llm: LLM,
    tools: list[Tool],
    *,
    services: Services | None = None,
    limit: int = 6,
    system: str | None = None,
) -> Agent:
    """A ReAct agent: tool-calling loop with a human-interrupt loop-breaker."""
    svc = _services(services)
    sg: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    add_wrapped(sg, "act", react_node(llm, tools), svc)
    add_wrapped(sg, HUMAN_INTERRUPT, human_interrupt_node, svc)
    sg.add_edge(START, "act")
    add_router(sg, "act", break_on_attempts(retry="act", escalate=HUMAN_INTERRUPT, limit=limit))
    sg.add_edge(HUMAN_INTERRUPT, "act")
    return Agent(sg.compile(checkpointer=MemorySaver()), svc, system=system)


def make_plan_execute_agent(
    llm: LLM,
    tools: list[Tool],
    *,
    services: Services | None = None,
    limit: int = 6,
    system: str | None = None,
) -> Agent:
    """A Plan-and-Solve agent: draft a plan once, then run the tool-calling loop."""
    svc = _services(services)
    sg: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    add_wrapped(sg, "plan", plan_node(llm), svc)
    add_wrapped(sg, "act", react_node(llm, tools), svc)
    add_wrapped(sg, HUMAN_INTERRUPT, human_interrupt_node, svc)
    sg.add_edge(START, "plan")
    sg.add_edge("plan", "act")
    add_router(sg, "act", break_on_attempts(retry="act", escalate=HUMAN_INTERRUPT, limit=limit))
    sg.add_edge(HUMAN_INTERRUPT, "act")
    return Agent(sg.compile(checkpointer=MemorySaver()), svc, system=system)
