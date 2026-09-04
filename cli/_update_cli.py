"""The ``orcho update`` journey: report install provenance, then upgrade.

``sdk/self_update.py`` owns detection and decides whether an upgrade command
may run unattended. This module owns the two things a planner must not do:
rendering the plan for an operator, and spawning the upgrade process.

The command never re-implements a package manager. It resolves which manager
owns the environment and delegates to it, so upgrade semantics stay the
manager's responsibility.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys

from core.io.ansi import C, paint
from sdk.self_update import UpgradePlan, plan_upgrade

#: Human labels for the managers ``detect_provenance`` can report. Kept in
#: step with :data:`KNOWN_MANAGERS` by a test, so a manager added to the
#: planner cannot reach an operator as a bare internal token.
_MANAGER_LABELS: dict[str, str] = {
    "pipx": "pipx-managed venv",
    "uv-tool": "uv tool venv",
    "venv-pip": "virtual environment (pip)",
    "pip": "interpreter environment (pip)",
    "editable": "editable install",
    "source": "source checkout",
}


def _command_text(plan: UpgradePlan) -> str:
    """Return ``plan``'s command as a copy-pasteable shell line."""
    return " ".join(shlex.quote(part) for part in plan.command)


def format_update_plan(plan: UpgradePlan) -> str:
    """Render the resolved install and what Orcho intends to do about it."""
    provenance = plan.provenance
    label = _MANAGER_LABELS.get(provenance.manager, provenance.manager)
    lines = [
        paint("Orcho install", C.BOLD, C.CYAN),
        f"  detected   {label}",
        f"  location   {provenance.prefix}",
    ]
    if provenance.package:
        lines.append(f"  package    {provenance.package}")
    if provenance.editable_source:
        lines.append(f"  source     {provenance.editable_source}")
    elif provenance.local_source:
        lines.append(f"  built from {provenance.local_source}")

    if plan.command:
        lines.extend((
            "",
            paint("Upgrade command", C.BOLD, C.CYAN),
            f"  {_command_text(plan)}",
        ))
    if plan.blocked_reason:
        lines.append("")
        lines.append(f"{paint('Not run automatically:', C.YELLOW)} {plan.blocked_reason}.")
        if plan.hint:
            lines.append(paint(f"  {plan.hint}", C.GREY))
    return "\n".join(lines)


def _execute(plan: UpgradePlan) -> int:
    """Run the upgrade command, streaming its output to this terminal.

    The child writes straight to the inherited stdout file descriptor, so this
    process must flush first. Without that, Python's block buffering (any time
    stdout is a pipe — CI logs, `orcho update | tee`) emits the whole Orcho
    report *after* the manager's output, making the transcript read as though
    the upgrade ran before anything was decided.
    """
    print(f"{paint('Running:', C.BOLD)} {_command_text(plan)}")
    sys.stdout.flush()
    try:
        completed = subprocess.run(plan.command, check=False)
    except OSError as exc:
        print(
            f"{paint('Upgrade failed to start:', C.RED)} {exc}\n"
            f"Run it yourself with:\n  {_command_text(plan)}",
            file=sys.stderr,
        )
        return 1
    if completed.returncode != 0:
        print(
            f"{paint('Upgrade command failed', C.RED)} "
            f"(exit {completed.returncode}). Output above is from "
            f"{plan.command[0]}.",
            file=sys.stderr,
        )
    return completed.returncode


def cmd_update(args: argparse.Namespace) -> int:
    """Report install provenance and upgrade Orcho where that is safe.

    ``--dry-run`` reports without upgrading. Plans the planner marked
    print-only (a checkout, an editable install, a missing manager binary, or
    a locally built install an index upgrade would discard) print the command
    and exit ``0``: the report is the deliverable, not a failure.
    """
    plan = plan_upgrade()
    print(format_update_plan(plan))
    if args.dry_run or not plan.auto_runnable:
        return 0
    print("")
    return _execute(plan)


__all__ = ["cmd_update", "format_update_plan"]
