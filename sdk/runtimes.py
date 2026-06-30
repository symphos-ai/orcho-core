"""sdk/runtimes.py — CLI agentic runtime detection.

`orcho workspace init` leads its MCP-client setup guidance with the
runtimes the user actually has installed. This module owns that probe
and nothing else: the catalogue of known runtimes, the
:class:`DetectedRuntime` record, and the PATH lookup.

It is presentation-free — rendering of the detection result lives in
``cli/_formatters.py``. Keeping the probe here (rather than inline in
``sdk/workspace.py``) means the runtime catalogue grows in one place
without enlarging the workspace-bootstrap module.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class DetectedRuntime:
    """One CLI agentic runtime probed for on the user's PATH.

    ``path`` is the resolved executable location when the runtime is
    installed, or ``None`` when :func:`shutil.which` found nothing.
    """

    client: str
    command: str
    path: str | None

    @property
    def installed(self) -> bool:
        return self.path is not None


#: Known CLI agentic runtimes, in display order. Each pair is
#: ``(display name, executable probed via shutil.which)``. Keep this
#: aligned with the per-client setup blocks rendered in
#: ``cli/_formatters.format_workspace_init``.
CLI_RUNTIMES: Final[tuple[tuple[str, str], ...]] = (
    ("Codex CLI / Codex app", "codex"),
    ("Claude Code", "claude"),
    ("Gemini CLI", "gemini"),
)


def detect_cli_runtimes() -> tuple[DetectedRuntime, ...]:
    """Probe PATH for every known CLI agentic runtime.

    Pure read of the environment — no subprocess is spawned, so this
    stays cheap and side-effect-free even when several runtimes are
    installed. Always returns one entry per known runtime; ``path`` is
    ``None`` for the ones not found.
    """
    return tuple(
        DetectedRuntime(
            client=client,
            command=command,
            path=shutil.which(command),
        )
        for client, command in CLI_RUNTIMES
    )


__all__ = [
    "CLI_RUNTIMES",
    "DetectedRuntime",
    "detect_cli_runtimes",
]
