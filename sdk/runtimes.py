"""sdk/runtimes.py — CLI agentic runtime detection.

`orcho workspace init` leads its MCP-client setup guidance with the
runtimes the user actually has installed, and gates on availability
before writing config. This module owns that probe and nothing else:
the catalogue of known runtimes, the :class:`DetectedRuntime` record,
the PATH lookup, and the :class:`RuntimeAvailability` assessment of a
configured per-phase runtime map against what is actually installed.

It is presentation-free — rendering of the detection result lives in
``cli/_formatters.py``. Keeping the probe here (rather than inline in
``sdk/workspace.py``) means the runtime catalogue grows in one place
without enlarging the workspace-bootstrap module.
"""
from __future__ import annotations

import shutil
from collections.abc import Iterable
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


#: Configured runtime ids whose CLI executable differs from the id
#: itself. Any id absent here probes an executable of the same name,
#: so third-party runtime ids resolve without a core edit.
_RUNTIME_COMMAND_ALIASES: Final[dict[str, str]] = {
    "claude-glm": "claude",
}

#: Preferred replacement order when a configured runtime is missing
#: and the caller wants to offer an installed one instead. ``claude``
#: first — it is the engine-wide last-resort default runtime.
_FALLBACK_PREFERENCE: Final[tuple[str, ...]] = ("claude", "codex", "gemini")


def runtime_command(runtime: str) -> str:
    """Name of the CLI executable a configured runtime id needs on PATH."""
    return _RUNTIME_COMMAND_ALIASES.get(runtime, runtime)


def runtime_installed(runtime: str) -> bool:
    """True when the executable behind ``runtime`` is on PATH."""
    return shutil.which(runtime_command(runtime)) is not None


@dataclass(frozen=True, slots=True)
class RuntimeAvailability:
    """PATH availability of the runtime ids a config actually relies on.

    ``installed_runtimes`` lists catalogue runtimes found on PATH (by
    runtime id). ``missing_runtimes`` lists configured ids whose
    executable was not found, in first-seen configuration order.
    ``fallback_runtime`` is the installed runtime to offer as a
    replacement, or ``None`` when nothing is installed.
    """

    installed_runtimes: tuple[str, ...]
    missing_runtimes: tuple[str, ...]
    fallback_runtime: str | None

    @property
    def any_installed(self) -> bool:
        return bool(self.installed_runtimes)


def assess_runtime_availability(configured: Iterable[str]) -> RuntimeAvailability:
    """Check configured runtime ids against the executables on PATH.

    Pure read of the environment, same as :func:`detect_cli_runtimes`.
    Ids outside the catalogue are probed by their own executable name
    (via :func:`runtime_command`), so plugin-registered runtimes are
    assessed with the same rule as built-ins.
    """
    installed = tuple(r.command for r in detect_cli_runtimes() if r.installed)
    missing: list[str] = []
    for runtime in configured:
        if runtime not in missing and not runtime_installed(runtime):
            missing.append(runtime)
    fallback = next((r for r in _FALLBACK_PREFERENCE if r in installed), None)
    if fallback is None and installed:
        fallback = installed[0]
    return RuntimeAvailability(
        installed_runtimes=installed,
        missing_runtimes=tuple(missing),
        fallback_runtime=fallback,
    )


__all__ = [
    "CLI_RUNTIMES",
    "DetectedRuntime",
    "RuntimeAvailability",
    "assess_runtime_availability",
    "detect_cli_runtimes",
    "runtime_command",
    "runtime_installed",
]
