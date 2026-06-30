"""pipeline/project/workspace_picker.py — bare-run project resolution.

When ``orcho run`` is invoked without ``--project`` (and not in resume
mode), the CLI used to fall back to ``Path.cwd()``. That silently
misfires for multi-repo workspaces: a cwd that contains git subrepos
but is itself not a git checkout produces a degraded worktree and a
later ``git diff`` that sees nothing.

This module resolves the bare-run case against the workspace project
map (``<workspace>/.orcho/config.local.json`` ``projects`` block):

1. cwd is inside one of the registered project paths → auto-pick that
   project (no prompt).
2. cwd is not inside any → interactive picker on TTY, hard-error in
   non-interactive transports (CI, MCP, ``--no-interactive``).
3. No workspace configured or empty project map → hard-error with a
   hint to (re)initialize via ``orcho workspace init``. No silent
   cwd fallback — empty map means the workspace is mis-configured,
   not that the user wants to run in an arbitrary directory.

The picker only owns terminal I/O; the helper accepts injected
``stdin``/``stdout`` for tests and stays frontend-agnostic.
"""
from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

from core.io.journey_prompt import bold, green_bold, grey, is_color_active
from pipeline.project.project_aliases import load_workspace_project_aliases


class WorkspaceProjectPickError(Exception):
    """Raised when bare-run project resolution cannot proceed.

    ``message`` is the one-line cause; ``hint`` is the actionable
    follow-up (multi-line allowed). The CLI prints them as a single
    block via ``print_error``; tests assert on either field.
    """

    def __init__(self, *, message: str, hint: str) -> None:
        super().__init__(f"{message}\n{hint}" if hint else message)
        self.message = message
        self.hint = hint


def pick_project_for_fresh_run(
    *,
    cwd: Path,
    workspace: str | Path | None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    no_interactive: bool = False,
) -> Path:
    """Resolve the project path for a fresh bare-run invocation.

    Raises :class:`WorkspaceProjectPickError` on any unresolved case;
    the CLI converts that into ``print_error`` + ``sys.exit(2)``.
    """
    si = stdin if stdin is not None else sys.stdin
    so = stdout if stdout is not None else sys.stdout

    aliases = load_workspace_project_aliases(workspace=workspace)
    if not aliases:
        raise WorkspaceProjectPickError(
            message=(
                "Workspace has no registered projects "
                "(missing or empty project map)."
            ),
            hint=(
                "Run `orcho workspace init <path>` to (re)initialize the "
                "project map, or pass --project <path> explicitly."
            ),
        )

    cwd_resolved = cwd.resolve()
    auto = _auto_pick_by_cwd(cwd_resolved, aliases)
    if auto is not None:
        return auto

    if no_interactive or not _is_tty(si):
        raise WorkspaceProjectPickError(
            message=(
                f"cwd ({cwd_resolved}) is not inside any registered project."
            ),
            hint=_format_known_projects_hint(aliases),
        )

    picked = _interactive_pick(aliases, stdin=si, stdout=so)
    if picked is None:
        # User aborted (Ctrl-C / Ctrl-D / explicit "exit"). Surface as
        # the same error class so the CLI exits with a non-zero rc and
        # a useful message instead of falling through.
        raise WorkspaceProjectPickError(
            message="No project selected.",
            hint=_format_known_projects_hint(aliases),
        )
    return picked


def _auto_pick_by_cwd(
    cwd: Path, aliases: Mapping[str, Path],
) -> Path | None:
    """Return the alias whose path equals or contains ``cwd``.

    Longest-match wins so nested layouts (``foo`` and ``foo/bar`` both
    registered) pick the inner project when cwd is inside it.
    """
    best: tuple[Path, int] | None = None
    for project_path in aliases.values():
        try:
            cwd.relative_to(project_path)
        except ValueError:
            continue
        depth = len(project_path.parts)
        if best is None or depth > best[1]:
            best = (project_path, depth)
    return None if best is None else best[0]


def _format_known_projects_hint(aliases: Mapping[str, Path]) -> str:
    lines = ["Pass --project <alias|path>. Known projects:"]
    for alias, path in sorted(aliases.items()):
        lines.append(f"  - {alias}: {path}")
    return "\n".join(lines)


def _is_tty(stream: TextIO) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)())


def _interactive_pick(
    aliases: Mapping[str, Path],
    *,
    stdin: TextIO,
    stdout: TextIO,
) -> Path | None:
    """Numbered picker over registered projects. Returns None on abort."""
    color = is_color_active(stdout)
    items = sorted(aliases.items())
    stdout.write(
        "\n"
        + bold(
            "Current directory is not inside a registered project.",
            color=color,
        )
        + "\n"
        + bold("Pick a project to run in:", color=color)
        + "\n"
    )
    for idx, (alias, path) in enumerate(items, start=1):
        marker = "  "
        line_no = bold(f"{marker}{idx}) {alias}", color=color)
        default_chip = (
            "  " + green_bold("[default]", color=color) if idx == 1 else ""
        )
        stdout.write(f"{line_no}{default_chip}\n")
        stdout.write(grey(f"     {path}", color=color) + "\n")
    exit_idx = len(items) + 1
    stdout.write(bold(f"  {exit_idx}) Exit", color=color) + "\n")

    valid = {str(i) for i in range(1, exit_idx + 1)}
    prompt = bold(f"Choice [1-{exit_idx}]: ", color=color)
    while True:
        stdout.write(prompt)
        stdout.flush()
        try:
            line = stdin.readline()
        except KeyboardInterrupt:
            stdout.write("\n")
            return None
        if not line:
            stdout.write("\n")
            return None
        choice = line.strip()
        if not choice:
            choice = "1"
        if choice not in valid:
            stdout.write(
                f"  Please answer one of: 1-{exit_idx}\n"
            )
            continue
        n = int(choice)
        if n == exit_idx:
            return None
        return items[n - 1][1]


__all__ = [
    "WorkspaceProjectPickError",
    "pick_project_for_fresh_run",
]
