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

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph

from ..critique import Validator, critic_node
from ..llm import LLM
from ..memory import InMemoryStore
from ..observability import TrajectoryLog
from ..planning import plan_node
from ..react import react_node
from ..resilience import HUMAN_INTERRUPT, break_on_attempts, human_interrupt_node
from ..routing import loop_until_done
from ..run import Agent
from ..seam import Services, add_router, add_wrapped
from ..state import ReActState
from ..tools import Tool

__all__ = [
    "make_guarded_react_agent",
    "make_plan_execute_agent",
    "make_react_agent",
    "make_reflexion_agent",
]


def _services(services: Services | None) -> Services:
    return services or Services(memory=InMemoryStore(), observer=TrajectoryLog())


# How many graph super-steps a guarded loop can take before LangGraph's hard
# backstop trips. The loop-breaker already guarantees termination by escalating
# at `limit` attempts; this just keeps the backstop comfortably *above* the
# breaker's reach so the clean human-interrupt escalation always wins the race
# (otherwise a high `limit` would raise GraphRecursionError before escalating).
# The budget is per `.invoke()`: a resume is a fresh invocation with its own
# counter, so we only size for one escalation cycle — `limit` attempts (~1
# super-step each) plus fixed slack for the plan node and human-interrupt step.
_BACKSTOP_SLACK = 20


def _backstop(limit: int) -> int:
    """Default `recursion_limit` for a guarded loop with attempt budget `limit`."""
    return limit + _BACKSTOP_SLACK


def make_react_agent(
    llm: LLM,
    tools: list[Tool],
    *,
    services: Services | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    system: str | None = None,
    recursion_limit: int = 25,
) -> Agent:
    """A flat ReAct agent — the baseline ("less structure, more intelligence").

    A tool-calling loop that runs until the model finishes: no forced planning,
    critique, or human interrupt. `recursion_limit` is LangGraph's hard backstop
    and the *only* bound on this template, so raise it for tasks that legitimately
    need more steps. Reach for `make_guarded_react_agent` or
    `make_plan_execute_agent` when constraints demand more structure (graceful
    degradation).
    """
    svc = _services(services)
    sg: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    add_wrapped(sg, "act", react_node(llm, tools), svc)
    sg.add_edge(START, "act")
    add_router(sg, "act", loop_until_done(retry="act"))
    chk = checkpointer if checkpointer is not None else MemorySaver()
    return Agent(
        sg.compile(checkpointer=chk),
        svc,
        system=system,
        recursion_limit=recursion_limit,
    )


def make_guarded_react_agent(
    llm: LLM,
    tools: list[Tool],
    *,
    services: Services | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    limit: int = 6,
    system: str | None = None,
    recursion_limit: int | None = None,
) -> Agent:
    """ReAct with a loop-breaker that escalates to a human after `limit` attempts.

    The guarded specialization of `make_react_agent` for higher-stakes runs.
    `recursion_limit` defaults to a value coupled to `limit` (see `_backstop`) so
    the human-interrupt escalation always trips before LangGraph's hard backstop;
    pass an explicit value to override.
    """
    svc = _services(services)
    sg: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    add_wrapped(sg, "act", react_node(llm, tools), svc)
    add_wrapped(sg, HUMAN_INTERRUPT, human_interrupt_node, svc)
    sg.add_edge(START, "act")
    add_router(sg, "act", break_on_attempts(retry="act", escalate=HUMAN_INTERRUPT, limit=limit))
    sg.add_edge(HUMAN_INTERRUPT, "act")
    backstop = recursion_limit if recursion_limit is not None else _backstop(limit)
    chk = checkpointer if checkpointer is not None else MemorySaver()
    return Agent(
        sg.compile(checkpointer=chk),
        svc,
        system=system,
        recursion_limit=backstop,
    )


def make_reflexion_agent(
    llm: LLM,
    tools: list[Tool],
    validator: Validator,
    *,
    services: Services | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    limit: int = 6,
    system: str | None = None,
    recursion_limit: int | None = None,
) -> Agent:
    """Generate → validate → revise: ReAct with a deterministic critic.

    Every candidate answer passes through `critic_node(validator)` before the
    run counts as done; rejections are fed back as critique and the agent
    revises. Both loops share one attempt budget (`limit`) and both routers
    are `break_on_attempts`: a candidate that always *finishes* but never
    *validates* escalates to the human-interrupt node instead of spinning.
    """
    svc = _services(services)
    sg: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    add_wrapped(sg, "act", react_node(llm, tools), svc)
    add_wrapped(sg, "critic", critic_node(validator), svc)
    add_wrapped(sg, HUMAN_INTERRUPT, human_interrupt_node, svc)
    sg.add_edge(START, "act")
    # act's "done" is only a *candidate* — route it to the critic, not END.
    to_critic = break_on_attempts(
        retry="act", escalate=HUMAN_INTERRUPT, limit=limit, done="critic"
    )
    add_router(sg, "act", to_critic)
    add_router(sg, "critic", break_on_attempts(retry="act", escalate=HUMAN_INTERRUPT, limit=limit))
    sg.add_edge(HUMAN_INTERRUPT, "act")
    # One attempt ≈ act + critic = 2 super-steps, so size the backstop for 2×limit.
    backstop = recursion_limit if recursion_limit is not None else _backstop(2 * limit)
    chk = checkpointer if checkpointer is not None else MemorySaver()
    return Agent(
        sg.compile(checkpointer=chk),
        svc,
        system=system,
        recursion_limit=backstop,
    )


def make_plan_execute_agent(
    llm: LLM,
    tools: list[Tool],
    *,
    services: Services | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    limit: int = 6,
    system: str | None = None,
    recursion_limit: int | None = None,
) -> Agent:
    """A Plan-and-Solve agent: draft a plan once, then run the tool-calling loop.

    `recursion_limit` defaults to a value coupled to `limit` (see `_backstop`);
    pass an explicit value to override.
    """
    svc = _services(services)
    sg: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    add_wrapped(sg, "plan", plan_node(llm), svc)
    add_wrapped(sg, "act", react_node(llm, tools), svc)
    add_wrapped(sg, HUMAN_INTERRUPT, human_interrupt_node, svc)
    sg.add_edge(START, "plan")
    sg.add_edge("plan", "act")
    add_router(sg, "act", break_on_attempts(retry="act", escalate=HUMAN_INTERRUPT, limit=limit))
    sg.add_edge(HUMAN_INTERRUPT, "act")
    backstop = recursion_limit if recursion_limit is not None else _backstop(limit)
    chk = checkpointer if checkpointer is not None else MemorySaver()
    return Agent(
        sg.compile(checkpointer=chk),
        svc,
        system=system,
        recursion_limit=backstop,
    )
