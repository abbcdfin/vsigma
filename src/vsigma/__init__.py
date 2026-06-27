"""vsigma — Cognitive Architecture Toolkit for Autonomous Systems.

Phase 1 (Toolkit Mode): modular, stateful building blocks for assembling
task-specific cognitive architectures on top of LangGraph.

The composition model behind this surface is documented in
docs/proposals/composition-model.md.
"""

from .builder import Agent, GraphBuilder
from .context import ContextManager, PassthroughContext
from .llm import (
    LLM,
    FakeLLM,
    LLMResponse,
    OpenAICompatibleLLM,
    ToolCall,
    default_llm,
)
from .memory import InMemoryStore, MemoryStore
from .observability import Observer, TrajectoryEvent, TrajectoryLog
from .react import react_node
from .resilience import HUMAN_INTERRUPT, Router, break_on_attempts, human_interrupt_node
from .run import Run
from .state import BaseState, NodeFn, ReActState, State, Status
from .tools import Tool, ToolFunc
from .webtools import Fetcher, make_web_tools

__version__ = "0.0.1"

__all__ = [
    "HUMAN_INTERRUPT",
    "LLM",
    "Agent",
    "BaseState",
    "ContextManager",
    "FakeLLM",
    "Fetcher",
    "GraphBuilder",
    "InMemoryStore",
    "LLMResponse",
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
    "human_interrupt_node",
    "make_web_tools",
    "react_node",
]
