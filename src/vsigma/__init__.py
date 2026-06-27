"""vsigma — Cognitive Architecture Toolkit for Autonomous Systems.

Phase 1 (Toolkit Mode): modular, stateful building blocks for assembling
task-specific cognitive architectures on top of LangGraph.

The composition model behind this surface is documented in
docs/proposals/composition-model.md.
"""

from .builder import Agent, GraphBuilder
from .context import ContextManager, PassthroughContext
from .estimation import estimate_effort
from .llm import (
    LLM,
    FakeLLM,
    LLMResponse,
    OpenAICompatibleLLM,
    ToolCall,
    default_llm,
)
from .mcp import ConnectionFactory, MCPToolset, make_mcp_tools, stdio_connection
from .memory import InMemoryStore, MemoryStore
from .observability import Observer, TrajectoryEvent, TrajectoryLog
from .planning import plan_node
from .react import react_node
from .resilience import HUMAN_INTERRUPT, break_on_attempts, human_interrupt_node
from .routing import Router
from .run import Run
from .state import BaseState, NodeFn, ReActState, State, Status
from .tools import Tool, ToolFunc

__version__ = "0.0.1"

__all__ = [
    "HUMAN_INTERRUPT",
    "LLM",
    "Agent",
    "BaseState",
    "ConnectionFactory",
    "ContextManager",
    "FakeLLM",
    "GraphBuilder",
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
    "State",
    "Status",
    "Tool",
    "ToolCall",
    "ToolFunc",
    "TrajectoryEvent",
    "TrajectoryLog",
    "__version__",
    "break_on_attempts",
    "default_llm",
    "estimate_effort",
    "human_interrupt_node",
    "make_mcp_tools",
    "plan_node",
    "react_node",
    "stdio_connection",
]
