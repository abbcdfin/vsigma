"""Agent templates — one-line, opinionated, prebuilt topologies.

Templates are the "good start" tier: they compose the building blocks into a
known-good graph so you don't rebuild it by hand. Each returns an `Agent` whose
`.graph` is a real compiled LangGraph graph — inspect it, run it, or drop it into
a bigger graph as a node. Templates are built only on the public blocks, so
nothing here is hidden from you.

Naming: `make_*`, consistent with `make_mcp_tools` / `make_web_tools`.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Durability

from ..critique import Validator, critic_node
from ..interceptor import Interceptor, add_router, add_wrapped
from ..llm import LLM
from ..planning import plan_node
from ..react import react_node
from ..resilience import HUMAN_INTERRUPT, break_on_attempts, human_interrupt_node
from ..retention import retain_window, validate_retain_messages
from ..routing import loop_until_done
from ..run import Agent
from ..state import ReActState
from ..tools import Tool

__all__ = [
    "make_guarded_react_agent",
    "make_plan_execute_agent",
    "make_react_agent",
    "make_reflexion_agent",
]


def _interceptor(interceptor: Interceptor | None) -> Interceptor:
    return interceptor or Interceptor()


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
    interceptor: Interceptor | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    system: str | None = None,
    recursion_limit: int = 25,
    durability: Durability | None = "exit",
    checkpoints: Literal["all", "latest"] = "all",
    retain_messages: int | None = None,
) -> Agent:
    """A flat ReAct agent — the baseline ("less structure, more intelligence").

    A tool-calling loop that runs until the model finishes: no forced planning,
    critique, or human interrupt. `recursion_limit` is LangGraph's hard backstop
    and the *only* bound on this template, so raise it for tasks that legitimately
    need more steps. Reach for `make_guarded_react_agent` or
    `make_plan_execute_agent` when constraints demand more structure (graceful
    degradation).
    """
    itc = _interceptor(interceptor)
    if retain_messages is not None:
        validate_retain_messages(itc.context, retain_messages)

    sg: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    add_wrapped(sg, "act", react_node(llm, tools), itc)
    sg.add_edge(START, "act")
    done_target = "retain" if retain_messages is not None else END
    add_router(sg, "act", loop_until_done(retry="act", done=done_target))
    if retain_messages is not None:
        sg.add_node("retain", cast("Any", retain_window(retain_messages)))
        sg.add_edge("retain", END)
    chk = checkpointer if checkpointer is not None else MemorySaver()
    return Agent(
        sg.compile(checkpointer=chk),
        itc,
        system=system,
        recursion_limit=recursion_limit,
        durability=durability,
        checkpoints=checkpoints,
    )


def make_guarded_react_agent(
    llm: LLM,
    tools: list[Tool],
    *,
    interceptor: Interceptor | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    limit: int = 6,
    system: str | None = None,
    recursion_limit: int | None = None,
    durability: Durability | None = "exit",
    checkpoints: Literal["all", "latest"] = "all",
    retain_messages: int | None = None,
) -> Agent:
    """ReAct with a loop-breaker that escalates to a human after `limit` attempts.

    The guarded specialization of `make_react_agent` for higher-stakes runs.
    `recursion_limit` defaults to a value coupled to `limit` (see `_backstop`) so
    the human-interrupt escalation always trips before LangGraph's hard backstop;
    pass an explicit value to override.
    """
    itc = _interceptor(interceptor)
    if retain_messages is not None:
        validate_retain_messages(itc.context, retain_messages)

    sg: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    add_wrapped(sg, "act", react_node(llm, tools), itc)
    add_wrapped(sg, HUMAN_INTERRUPT, human_interrupt_node, itc)
    sg.add_edge(START, "act")
    done_target = "retain" if retain_messages is not None else END
    add_router(
        sg,
        "act",
        break_on_attempts(retry="act", escalate=HUMAN_INTERRUPT, limit=limit, done=done_target),
    )
    sg.add_edge(HUMAN_INTERRUPT, "act")
    if retain_messages is not None:
        sg.add_node("retain", cast("Any", retain_window(retain_messages)))
        sg.add_edge("retain", END)
    backstop = recursion_limit if recursion_limit is not None else _backstop(limit)
    chk = checkpointer if checkpointer is not None else MemorySaver()
    return Agent(
        sg.compile(checkpointer=chk),
        itc,
        system=system,
        recursion_limit=backstop,
        durability=durability,
        checkpoints=checkpoints,
    )


def make_reflexion_agent(
    llm: LLM,
    tools: list[Tool],
    validator: Validator,
    *,
    interceptor: Interceptor | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    limit: int = 6,
    system: str | None = None,
    recursion_limit: int | None = None,
    durability: Durability | None = "exit",
    checkpoints: Literal["all", "latest"] = "all",
    retain_messages: int | None = None,
) -> Agent:
    """Generate → validate → revise: ReAct with a deterministic critic.

    Every candidate answer passes through `critic_node(validator)` before the
    run counts as done; rejections are fed back as critique and the agent
    revises. Both loops share one attempt budget (`limit`) and both routers
    are `break_on_attempts`: a candidate that always *finishes* but never
    *validates* escalates to the human-interrupt node instead of spinning.
    """
    itc = _interceptor(interceptor)
    if retain_messages is not None:
        validate_retain_messages(itc.context, retain_messages)

    sg: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    add_wrapped(sg, "act", react_node(llm, tools), itc)
    add_wrapped(sg, "critic", critic_node(validator), itc)
    add_wrapped(sg, HUMAN_INTERRUPT, human_interrupt_node, itc)
    sg.add_edge(START, "act")
    # act's "done" is only a *candidate* — route it to the critic, not END.
    to_critic = break_on_attempts(
        retry="act", escalate=HUMAN_INTERRUPT, limit=limit, done="critic"
    )
    add_router(sg, "act", to_critic)
    done_target = "retain" if retain_messages is not None else END
    add_router(
        sg,
        "critic",
        break_on_attempts(retry="act", escalate=HUMAN_INTERRUPT, limit=limit, done=done_target),
    )
    sg.add_edge(HUMAN_INTERRUPT, "act")
    if retain_messages is not None:
        sg.add_node("retain", cast("Any", retain_window(retain_messages)))
        sg.add_edge("retain", END)
    # One attempt ≈ act + critic = 2 super-steps, so size the backstop for 2×limit.
    backstop = recursion_limit if recursion_limit is not None else _backstop(2 * limit)
    chk = checkpointer if checkpointer is not None else MemorySaver()
    return Agent(
        sg.compile(checkpointer=chk),
        itc,
        system=system,
        recursion_limit=backstop,
        durability=durability,
        checkpoints=checkpoints,
    )


def make_plan_execute_agent(
    llm: LLM,
    tools: list[Tool],
    *,
    interceptor: Interceptor | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    limit: int = 6,
    system: str | None = None,
    recursion_limit: int | None = None,
    durability: Durability | None = "exit",
    checkpoints: Literal["all", "latest"] = "all",
    retain_messages: int | None = None,
) -> Agent:
    """A Plan-and-Solve agent: draft a plan once, then run the tool-calling loop.

    `recursion_limit` defaults to a value coupled to `limit` (see `_backstop`);
    pass an explicit value to override.
    """
    itc = _interceptor(interceptor)
    if retain_messages is not None:
        validate_retain_messages(itc.context, retain_messages)

    sg: StateGraph[Any, Any, Any, Any] = StateGraph(ReActState)
    add_wrapped(sg, "plan", plan_node(llm), itc)
    add_wrapped(sg, "act", react_node(llm, tools), itc)
    add_wrapped(sg, HUMAN_INTERRUPT, human_interrupt_node, itc)
    sg.add_edge(START, "plan")
    sg.add_edge("plan", "act")
    done_target = "retain" if retain_messages is not None else END
    add_router(
        sg,
        "act",
        break_on_attempts(retry="act", escalate=HUMAN_INTERRUPT, limit=limit, done=done_target),
    )
    sg.add_edge(HUMAN_INTERRUPT, "act")
    if retain_messages is not None:
        sg.add_node("retain", cast("Any", retain_window(retain_messages)))
        sg.add_edge("retain", END)
    backstop = recursion_limit if recursion_limit is not None else _backstop(limit)
    chk = checkpointer if checkpointer is not None else MemorySaver()
    return Agent(
        sg.compile(checkpointer=chk),
        itc,
        system=system,
        recursion_limit=backstop,
        durability=durability,
        checkpoints=checkpoints,
    )
