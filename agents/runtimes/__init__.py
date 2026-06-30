"""Concrete agent providers (Claude Code, Codex CLI).

Also exposes the AgentProvider Strategy protocol and implementations
(RealAgentProvider, MockAgentProvider, FailingMockProvider) plus the
make_provider() factory.
"""

from agents.runtimes._strategy import (
    AgentProvider,
    FailingMockProvider,
    MockAgentProvider,
    RealAgentProvider,
    make_mock_phase_config,
    make_provider,
)
from agents.runtimes.claude import ClaudeAgent
from agents.runtimes.codex import CodexAgent
from agents.runtimes.identity import RuntimeIdentity, probe_runtime_identity

__all__ = [
    "ClaudeAgent",
    "CodexAgent",
    "AgentProvider",
    "RealAgentProvider",
    "MockAgentProvider",
    "FailingMockProvider",
    "make_provider",
    "make_mock_phase_config",
    "RuntimeIdentity",
    "probe_runtime_identity",
]

