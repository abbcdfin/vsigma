"""vsigma — Cognitive Architecture Toolkit for Autonomous Systems.

Phase 1 (Toolkit Mode): modular, stateful building blocks for assembling
task-specific cognitive architectures on top of LangGraph.

The composition model behind this surface is documented in
docs/proposals/composition-model.md.
"""

from .agents import make_guarded_react_agent, make_plan_execute_agent, make_react_agent
from .context import ContextManager, PassthroughContext
from .estimation import estimate_effort
from .langchain import from_langchain, from_langchain_tools
from .llm import (
    LLM,
    FakeLLM,
    LLMResponse,
    OpenAICompatibleLLM,
    StaticLLM,
    ToolCall,
    call,
    default_llm,
    say,
)
from .mcp import ConnectionFactory, MCPToolset, make_mcp_tools, stdio_connection
from .memory import InMemoryStore, MemoryStore
from .observability import Observer, TrajectoryEvent, TrajectoryLog
from .planning import plan_node
from .react import react_node
from .resilience import HUMAN_INTERRUPT, break_on_attempts, human_interrupt_node
from .routing import Router, loop_until_done
from .run import Agent, Run
from .seam import Services, add_router, add_wrapped, wrap
from .state import BaseState, NodeFn, ReActState, State, Status
from .tools import Tool, ToolFunc, tool

__version__ = "0.0.1"

__all__ = [
    "HUMAN_INTERRUPT",
    "LLM",
    "Agent",
    "BaseState",
    "ConnectionFactory",
    "ContextManager",
    "FakeLLM",
    "InMemoryStore",
    "LLMResponse",
    "MCPToolset",
    "MemoryStore",
    "NodeFn",
    "Observer",
    "OpenAICompatibleLLM",
    "PassthroughContext",
    "ReActState",
    "Router",
    "Run",
    "Services",
    "State",
    "StaticLLM",
    "Status",
    "Tool",
    "ToolCall",
    "ToolFunc",
    "TrajectoryEvent",
    "TrajectoryLog",
    "__version__",
    "add_router",
    "add_wrapped",
    "break_on_attempts",
    "call",
    "default_llm",
    "estimate_effort",
    "from_langchain",
    "from_langchain_tools",
    "human_interrupt_node",
    "loop_until_done",
    "make_guarded_react_agent",
    "make_mcp_tools",
    "make_plan_execute_agent",
    "make_react_agent",
    "plan_node",
    "react_node",
    "say",
    "stdio_connection",
    "tool",
    "wrap",
]
