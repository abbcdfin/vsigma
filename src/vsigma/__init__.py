"""vsigma — Cognitive Architecture Toolkit for Autonomous Systems.

Phase 1 (Toolkit Mode): modular, stateful building blocks for assembling
task-specific cognitive architectures on top of LangGraph.

The composition model behind this surface is documented in
docs/proposals/composition-model.md.
"""

from .agents import (
    make_guarded_react_agent,
    make_plan_execute_agent,
    make_react_agent,
    make_reflexion_agent,
)
from .bundle import (
    SCHEMA_VERSION,
    ingest_bundle,
    read_bundle,
    validate_bundle,
    write_bundle,
)
from .consolidation import (
    RunEndHook,
    deep_consolidate,
    episode_trajectory,
    reflexive_consolidator,
)
from .construction import distill
from .context import (
    ContextManager,
    PassthroughContext,
    RecallContext,
    SlidingWindowContext,
)
from .critique import (
    CRITIQUE_MARKER,
    Validator,
    all_of,
    critic_node,
    expect_json,
    llm_judge,
)
from .estimation import estimate_effort
from .langchain import from_langchain, from_langchain_tools
from .llm import (
    LLM,
    FakeLLM,
    LLMResponse,
    LoggingLLM,
    OpenAICompatibleLLM,
    StaticLLM,
    ToolCall,
    call,
    default_llm,
    disable_llm_logging,
    enable_llm_logging,
    say,
)
from .mcp import ConnectionFactory, MCPToolset, make_mcp_tools, stdio_connection
from .memory import InMemoryStore, MemoryStore
from .observability import Observer, TrajectoryEvent, TrajectoryLog
from .planning import plan_node
from .react import react_node
from .records import (
    Consolidator,
    MemoryRecord,
    Origin,
    Provenance,
    record_from_dict,
)
from .resilience import HUMAN_INTERRUPT, break_on_attempts, human_interrupt_node
from .routing import Router, loop_until_done
from .run import Agent, Run
from .scope import (
    AGENT,
    GLOBAL,
    Kind,
    Scope,
    ScopeLevel,
    episode,
    persona,
    recall,
    session,
    visible_scopes,
)
from .seam import Services, add_router, add_wrapped, wrap
from .state import BaseState, NodeFn, ReActState, State, Status
from .stores import SQLiteStore
from .tools import Tool, ToolFunc, tool

__version__ = "0.0.1"

__all__ = [
    "AGENT",
    "CRITIQUE_MARKER",
    "GLOBAL",
    "HUMAN_INTERRUPT",
    "LLM",
    "Agent",
    "BaseState",
    "ConnectionFactory",
    "Consolidator",
    "ContextManager",
    "FakeLLM",
    "InMemoryStore",
    "Kind",
    "LLMResponse",
    "LoggingLLM",
    "MCPToolset",
    "MemoryRecord",
    "MemoryStore",
    "NodeFn",
    "Origin",
    "Provenance",
    "Observer",
    "OpenAICompatibleLLM",
    "PassthroughContext",
    "ReActState",
    "RecallContext",
    "Router",
    "Run",
    "RunEndHook",
    "SCHEMA_VERSION",
    "SQLiteStore",
    "Scope",
    "ScopeLevel",
    "Services",
    "SlidingWindowContext",
    "State",
    "StaticLLM",
    "Status",
    "Tool",
    "ToolCall",
    "ToolFunc",
    "TrajectoryEvent",
    "TrajectoryLog",
    "Validator",
    "__version__",
    "add_router",
    "add_wrapped",
    "all_of",
    "break_on_attempts",
    "call",
    "critic_node",
    "deep_consolidate",
    "default_llm",
    "disable_llm_logging",
    "distill",
    "enable_llm_logging",
    "episode",
    "episode_trajectory",
    "estimate_effort",
    "expect_json",
    "from_langchain",
    "from_langchain_tools",
    "human_interrupt_node",
    "ingest_bundle",
    "llm_judge",
    "loop_until_done",
    "make_guarded_react_agent",
    "make_mcp_tools",
    "make_plan_execute_agent",
    "make_react_agent",
    "make_reflexion_agent",
    "persona",
    "plan_node",
    "react_node",
    "read_bundle",
    "recall",
    "record_from_dict",
    "reflexive_consolidator",
    "say",
    "session",
    "stdio_connection",
    "tool",
    "validate_bundle",
    "visible_scopes",
    "wrap",
    "write_bundle",
]
