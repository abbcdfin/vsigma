"""vsigma — Cognitive Architecture Toolkit for Autonomous Systems.

Toolkit Mode: modular, stateful building blocks for assembling
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
from .bridge import AsyncBridge
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
from .critique import CRITIQUE_MARKER, Validator, all_of, critic_node, llm_judge
from .estimation import estimate_effort
from .expiry import (
    ExpiredThread,
    ExpiryPolicy,
    ExpiryReport,
    ThreadExpiry,
    ThreadInfo,
)
from .interceptor import Interceptor, add_router, add_wrapped, wrap
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
from .maintenance import MaintenanceReport, run_maintenance, unconsolidated_episodes
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
from .retention import keep_latest, retain_window
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
from .sessions import PauseChanged, Session, SessionBusy, SessionPaused
from .state import BaseState, NodeFn, ReActState, State, Status
from .stores import SQLiteStore
from .tools import Tool, ToolFunc, normalize_tool_schema, tool

__version__ = "0.2.2"

__all__ = [
    "AGENT",
    "CRITIQUE_MARKER",
    "GLOBAL",
    "HUMAN_INTERRUPT",
    "LLM",
    "Agent",
    "AsyncBridge",
    "BaseState",
    "ConnectionFactory",
    "Consolidator",
    "ContextManager",
    "FakeLLM",
    "InMemoryStore",
    "Interceptor",
    "Kind",
    "LLMResponse",
    "LoggingLLM",
    "MCPToolset",
    "MaintenanceReport",
    "MemoryRecord",
    "MemoryStore",
    "NodeFn",
    "Observer",
    "OpenAICompatibleLLM",
    "Origin",
    "PassthroughContext",
    "PauseChanged",
    "Provenance",
    "ReActState",
    "RecallContext",
    "Router",
    "Run",
    "RunEndHook",
    "SCHEMA_VERSION",
    "SQLiteStore",
    "Scope",
    "ScopeLevel",
    "Session",
    "SessionBusy",
    "SessionPaused",
    "SlidingWindowContext",
    "State",
    "StaticLLM",
    "Status",
    "ThreadExpiry",
    "ThreadInfo",
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
    "ExpiredThread",
    "ExpiryPolicy",
    "ExpiryReport",
    "from_langchain",
    "from_langchain_tools",
    "human_interrupt_node",
    "ingest_bundle",
    "keep_latest",
    "llm_judge",
    "loop_until_done",
    "make_guarded_react_agent",
    "make_mcp_tools",
    "make_plan_execute_agent",
    "make_react_agent",
    "make_reflexion_agent",
    "normalize_tool_schema",
    "persona",
    "plan_node",
    "react_node",
    "read_bundle",
    "recall",
    "record_from_dict",
    "reflexive_consolidator",
    "retain_window",
    "run_maintenance",
    "say",
    "session",
    "stdio_connection",
    "tool",
    "unconsolidated_episodes",
    "validate_bundle",
    "visible_scopes",
    "wrap",
    "write_bundle",
]
