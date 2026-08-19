"""Claude Code-compatible GLM adapter with an adapter-owned launch env."""

from __future__ import annotations

from agents.runtimes.claude import ClaudeAgent
from core.infra import config


class ClaudeGlmAgent(ClaudeAgent):
    """Run Claude Code against z.ai without a wrapper executable."""

    runtime: str = "claude-glm"
    identity_provider: str = "z.ai"

    @staticmethod
    def _resolve_cli_binary() -> str:
        return config.get_claude_glm_bin()

    def _child_env_overrides(self) -> dict[str, str]:
        """Build the GLM launch environment before binary lookup."""
        import os

        token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "claude-glm requires ANTHROPIC_AUTH_TOKEN. Set it in the current "
                "environment (PowerShell: $env:ANTHROPIC_AUTH_TOKEN = '<GLM Coding Plan key>') "
                "then run the plain 'claude' executable again."
            )
        settings = config.AppConfig.load().claude_glm
        return {
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": str(settings["opus_model"]),
            "ANTHROPIC_DEFAULT_SONNET_MODEL": str(settings["sonnet_model"]),
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": str(settings["haiku_model"]),
            "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT": "1",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(settings["max_context_tokens"]),
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "API_TIMEOUT_MS": "3000000",
        }
