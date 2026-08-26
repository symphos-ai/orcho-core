"""Claude Code-compatible GLM adapter with an adapter-owned launch env."""

from __future__ import annotations

import os
from pathlib import Path

from agents.runtimes.claude import ClaudeAgent
from core.infra import config
from core.infra.paths import user_config_dir


class ClaudeGlmAgent(ClaudeAgent):
    """Run Claude Code against z.ai without a wrapper executable."""

    runtime: str = "claude-glm"
    identity_provider: str = "z.ai"

    @staticmethod
    def _resolve_cli_binary() -> str:
        return config.get_claude_glm_bin()

    def _child_env_overrides(self) -> dict[str, str]:
        """Build the GLM launch environment before binary lookup."""
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
            "CLAUDE_CONFIG_DIR": _config_dir(settings.get("config_dir")),
            "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT": "1",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(settings["max_context_tokens"]),
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "API_TIMEOUT_MS": "3000000",
        }


def _config_dir(configured: object) -> str:
    """Return the CLI config directory this adapter owns, creating it once.

    The adapter must supply this itself. The CLI resolves credentials from
    its config directory in preference to the environment, so a child that
    inherits the operator's ordinary directory authenticates as whoever is
    logged in there — not as the token this adapter just set. The operator
    cannot fix that from outside either: ``CLAUDE_CONFIG_DIR`` is
    process-global, so isolating this runtime by hand also strips the
    credentials of every other Claude-family runtime in the same pipeline.

    The directory holds onboarding and trust state as well as credentials,
    so it is deliberately per-user and stable rather than per-run: a
    location inside a run's worktree would be discarded between runs and
    would dirty the tree the run is judged on.
    """
    raw = str(configured or "").strip()
    path = Path(raw).expanduser() if raw else user_config_dir() / "claude-glm-config"
    # A credential store: keep it private, and never fail the launch over a
    # directory that already exists with the operator's own permissions.
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return str(path)
