"""
agents — Provider wrappers, Protocols, entities, and the agent registry.

Public surface (re-exported here so callers can ``from agents import X``):

    Protocol         IAgentRuntime, SessionMode
    Entities         SubTask, TestResult
    Registry         AgentRegistry, PhaseAgentConfig
    Runtimes         ClaudeAgent, CodexAgent
    Streaming        _stream_run, set_agent_log, set_stdout_echo
    subprocess       re-exported for tests that monkeypatch agents.subprocess
"""

import subprocess  # re-exported so tests can `monkeypatch.setattr(agents.subprocess, ...)`

from agents.entities import SubTask, TestResult
from agents.protocols import IAgentRuntime, SessionMode
from agents.registry import AgentRegistry, PhaseAgentConfig
from agents.runtimes import ClaudeAgent, CodexAgent
from agents.stream import StreamAbort, _stream_run, set_agent_log, set_stdout_echo

__all__ = [
    # Protocol
    "IAgentRuntime",
    "SessionMode",
    # Entities
    "SubTask",
    "TestResult",
    # Registry
    "AgentRegistry",
    "PhaseAgentConfig",
    # Providers
    "ClaudeAgent",
    "CodexAgent",
    # Streaming
    "_stream_run",
    "StreamAbort",
    "set_agent_log",
    "set_stdout_echo",
    "subprocess",
]
