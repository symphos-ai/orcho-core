"""Claude Code-compatible runtime that executes the ``claude-glm`` wrapper."""

from __future__ import annotations

from agents.runtimes.claude import ClaudeAgent
from core.infra import config


class ClaudeGlmAgent(ClaudeAgent):
    """Run a Claude Code-compatible GLM wrapper with its own runtime identity."""

    runtime: str = "claude-glm"
    identity_provider: str = "z.ai"

    @staticmethod
    def _resolve_cli_binary() -> str:
        return config.get_claude_glm_bin()
