"""pipeline/project/project_discovery_prompt.py — interactive registration of
undetected child folders during ``orcho workspace init``.

For each :class:`sdk.workspace.UndetectedCandidate` the prompt asks the user
whether to treat the folder as a workspace project. When yes:

* 0 nested git repos → offer ``git init`` (create a local repo so worktree
  isolation works later).
* 1 nested git repo → confirm and record its relative path as ``git_dir``.
* >1 nested git repos → numbered sub-picker (shallowest first = default).

The module accepts injected ``stdin``/``stdout`` so tests can drive it without
a real TTY. It is deliberately side-effect free at import time.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TextIO

from core.io.journey_prompt import bold, green_bold, grey, is_color_active
from sdk.workspace import ExtraProject, UndetectedCandidate


def _is_tty(stream: TextIO) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)())


def _ask_yn(
    prompt: str,
    *,
    default_yes: bool,
    stdin: TextIO,
    stdout: TextIO,
    color: bool,
) -> bool | None:
    """Ask a yes/no question.  Returns None on EOF / Ctrl-C (abort)."""
    hint = "[Y/n]" if default_yes else "[y/N]"
    full_prompt = bold(f"{prompt} {hint} ", color=color)
    stdout.write(full_prompt)
    stdout.flush()
    try:
        line = stdin.readline()
    except KeyboardInterrupt:
        stdout.write("\n")
        return None
    if not line:
        stdout.write("\n")
        return None
    choice = line.strip().lower()
    if not choice:
        return default_yes
    return choice in ("y", "yes")


def _pick_git_dir(
    nested: tuple[str, ...],
    folder_name: str,
    *,
    stdin: TextIO,
    stdout: TextIO,
    color: bool,
) -> str | None:
    """Numbered picker for >1 nested git dirs.  Returns selected rel-path or None."""
    stdout.write(
        "\n"
        + bold(
            f"  Multiple git repos found inside '{folder_name}'. "
            "Pick the one to use:",
            color=color,
        )
        + "\n"
    )
    for idx, rel in enumerate(nested, start=1):
        default_chip = "  " + green_bold("[default]", color=color) if idx == 1 else ""
        stdout.write(
            bold(f"    {idx}) {rel}", color=color) + default_chip + "\n"
        )
    exit_idx = len(nested) + 1
    stdout.write(bold(f"    {exit_idx}) Skip this folder", color=color) + "\n")

    valid = {str(i) for i in range(1, exit_idx + 1)}
    prompt_str = bold(f"  Choice [1-{exit_idx}]: ", color=color)
    while True:
        stdout.write(prompt_str)
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
            stdout.write(f"    Please answer 1-{exit_idx}\n")
            continue
        n = int(choice)
        if n == exit_idx:
            return None
        return nested[n - 1]


def _run_git_init(folder: Path) -> bool:
    """Run ``git init`` in ``folder``.  Returns True on success."""
    result = subprocess.run(
        ["git", "init", "-q"],
        cwd=str(folder),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def prompt_for_extra_projects(
    candidates: list[UndetectedCandidate],
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> list[ExtraProject]:
    """Ask the user about each undetected candidate.

    Returns confirmed :class:`ExtraProject` instances. ``git init`` is
    invoked here (in the interactive path only) when the user consents.

    The caller must NOT call this from a non-interactive context; use
    :func:`_is_tty` / ``no_interactive`` to gate.
    """
    si = stdin if stdin is not None else sys.stdin
    so = stdout if stdout is not None else sys.stdout
    color = is_color_active(so)

    confirmed: list[ExtraProject] = []
    for candidate in candidates:
        folder = Path(candidate.path)
        so.write(
            "\n"
            + bold(
                f"Folder '{candidate.name}' was not auto-detected as a project.",
                color=color,
            )
            + "\n"
        )

        register = _ask_yn(
            f"  Treat '{candidate.name}' as a workspace project?",
            default_yes=False,
            stdin=si,
            stdout=so,
            color=color,
        )
        if not register:
            continue

        nested = candidate.nested_git_dirs
        if len(nested) == 0:
            # No git anywhere — offer git init.
            do_init = _ask_yn(
                "  No git repo found inside. Create a local git repo here?",
                default_yes=True,
                stdin=si,
                stdout=so,
                color=color,
            )
            if do_init is None:
                continue
            if do_init:
                if _run_git_init(folder):
                    so.write(
                        grey(
                            f"    Initialized git repository in {folder}\n",
                            color=color,
                        )
                    )
                else:
                    so.write(
                        bold(
                            f"    Warning: git init failed in {folder}; "
                            "registering as no-git project.\n",
                            color=color,
                        )
                    )
            else:
                so.write(
                    grey(
                        "    Registering as no-git project; worktree isolation "
                        "unavailable until it has a git repo.\n",
                        color=color,
                    )
                )
            confirmed.append(ExtraProject(name=candidate.name, path=candidate.path, git_dir=""))

        elif len(nested) == 1:
            git_dir = nested[0]
            use_it = _ask_yn(
                f"  Found nested git repo at '{git_dir}'. Use it as git root?",
                default_yes=True,
                stdin=si,
                stdout=so,
                color=color,
            )
            if use_it is None or not use_it:
                continue
            confirmed.append(
                ExtraProject(name=candidate.name, path=candidate.path, git_dir=git_dir)
            )

        else:
            # Multiple nested repos — sub-picker.
            picked = _pick_git_dir(
                nested,
                folder_name=candidate.name,
                stdin=si,
                stdout=so,
                color=color,
            )
            if picked is None:
                so.write(grey(f"    Skipping '{candidate.name}'.\n", color=color))
                continue
            confirmed.append(
                ExtraProject(name=candidate.name, path=candidate.path, git_dir=picked)
            )

    return confirmed


__all__ = ["prompt_for_extra_projects"]
