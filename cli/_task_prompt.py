"""Interactive task-description prompt for the `orcho` CLI.

Asks the operator for a task on the keyboard when none was supplied,
while keeping the conservative no-op contract: it only prompts on a TTY
with ``--no-interactive`` unset, so CI / MCP / piped invocations keep
their existing non-blocking behaviour.
"""
from __future__ import annotations

import argparse
import sys

from core.io.ansi import C
from core.io.journey_prompt import paint
from core.io.terminal_input import drain_paste_burst


def prompt_for_task_if_needed(args: argparse.Namespace) -> None:
    """Ask the user for a task description when none was supplied.

    Mutates ``args.task`` in place when the user enters a non-empty
    string. Silent no-op when:

    * ``--task`` / ``--task-file`` / ``--resume`` / ``--from-run-plan``
      already covers the task source (``--resume`` and
      ``--from-run-plan`` both hydrate it from a persisted ``meta.json``);
    * ``--no-interactive`` is set;
    * stdin is not a TTY (CI, pipes, MCP transports);
    * the user submits an empty line or aborts with Ctrl-C / Ctrl-D.

    In the silent cases the downstream orchestrator surfaces its
    canonical ``task: provide --task or --task-file`` error, so the
    automated transports keep their existing exit contract.

    Styling routes through :func:`core.io.journey_prompt.paint` so
    the prompt obeys the shared color policy (explicit > override >
    auto-detect).
    """
    if (
        args.task
        or getattr(args, "task_file", None)
        or args.resume
        or getattr(args, "from_run_plan", None)
    ):
        return
    if getattr(args, "no_interactive", False):
        return
    if not sys.stdin.isatty():
        return
    print(
        f"{paint('No --task provided.', C.BOLD)} "
        f"{paint('Enter task description (empty line to abort):', C.GREY)}"
    )
    try:
        first = input(paint("Task: ", C.BOLD))
    except (EOFError, KeyboardInterrupt):
        print()  # newline after ^C / ^D for clean shell prompt
        return
    entered = drain_paste_burst(first, stdin=sys.stdin).strip()
    if entered:
        args.task = entered
