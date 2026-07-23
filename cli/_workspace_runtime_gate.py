"""cli/_workspace_runtime_gate.py — runtime availability gate for
``orcho workspace init``.

Runs after target preflight and before interactive project discovery:

* no CLI agent runtime on PATH at all → raise
  :class:`sdk.errors.WorkspaceInitError` recommending at least one
  install (``--force`` scaffolds anyway; ``--dry-run`` previews and
  only warns);
* some configured phase runtimes missing while another runtime is
  installed → offer (TTY only, honouring ``--no-interactive`` /
  ``--dry-run``) to switch those phases to the installed runtime in
  the workspace-local config.

The module accepts injected ``stdin``/``stdout`` so tests can drive it
without a real TTY, mirroring
:mod:`pipeline.project.project_discovery_prompt`.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TextIO

from core.io.journey_prompt import ask_yn, bold, grey, is_color_active
from sdk.errors import WorkspaceInitError
from sdk.runtimes import CLI_RUNTIMES, assess_runtime_availability
from sdk.workspace import planned_phase_runtimes


@dataclass(frozen=True, slots=True)
class RuntimeGateDecision:
    """Gate outcome: the runtime to switch the workspace config to, if any."""

    runtime_override: str | None = None


def workspace_runtime_gate(
    project_group_root: str,
    *,
    no_interactive: bool,
    dry_run: bool,
    force: bool,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> RuntimeGateDecision:
    """Assess runtime availability and, when apt, prompt for a switch.

    Raises :class:`WorkspaceInitError` when no runtime is installed,
    unless ``force`` (scaffold anyway) or ``dry_run`` (a preview must
    stay printable; the formatter surfaces the warning instead).
    """
    availability = assess_runtime_availability(
        planned_phase_runtimes(project_group_root).values()
    )

    if not availability.any_installed:
        if force or dry_run:
            return RuntimeGateDecision()
        probed = ", ".join(f"`{command}`" for _, command in CLI_RUNTIMES)
        clients = ", ".join(
            f"{client} (`{command}`)" for client, command in CLI_RUNTIMES
        )
        raise WorkspaceInitError(
            "no CLI agent runtime found on PATH "
            f"(probed: {probed}). Orcho phases need at least one "
            f"installed runtime — for example {clients}. Install one "
            "and re-run `orcho workspace init`, or pass --force to "
            "scaffold the workspace anyway."
        )

    fallback = availability.fallback_runtime
    if not availability.missing_runtimes or fallback is None:
        return RuntimeGateDecision()
    if no_interactive or dry_run:
        # The init output warns about the gap; changing config without
        # consent is out of bounds here.
        return RuntimeGateDecision()

    si = stdin if stdin is not None else sys.stdin
    so = stdout if stdout is not None else sys.stdout
    if not bool(getattr(si, "isatty", lambda: False)()):
        return RuntimeGateDecision()

    color = is_color_active(so)
    missing = ", ".join(f"'{r}'" for r in availability.missing_runtimes)
    so.write(
        "\n"
        + bold(
            f"Configured runtime(s) {missing} not found on PATH.",
            color=color,
        )
        + "\n"
    )
    answer = ask_yn(
        f"  Switch those phases to '{fallback}' in the workspace config?",
        default_yes=True,
        stdin=si,
        stdout=so,
        color=color,
    )
    if answer:
        return RuntimeGateDecision(runtime_override=fallback)
    so.write(
        grey(
            "    Keeping the configured runtimes; runs will fail until "
            "they are installed.\n",
            color=color,
        )
    )
    return RuntimeGateDecision()


__all__ = ["RuntimeGateDecision", "workspace_runtime_gate"]
