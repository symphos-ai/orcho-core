"""pipeline/control/resume_prompt.py — CLI text adapter for ``--resume`` intent.

This module is the *only* place that owns terminal stdin/stdout for
resume-mode selection. Other frontends (TUI, MCP, IDE plugins) MUST
NOT import it; they consume the pure intent model from
``resume_context`` (``ResumeIntentOptions``, ``classify_resume_mode``,
``resolve_latest_run``) and implement their own input loop.

Keeping the prompt boundary thin lets the orchestrators stay
non-interactive: they pass the parent meta through
``get_resume_intent_options`` and call this prompt only when stdin is
a TTY and ``--no-interactive`` is unset. The returned
:class:`PromptedResumeIntent` is a plain data object — callers apply
it explicitly (set ``args.task``, pick the output dir, etc.) so the
prompt never mutates argparse state.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TextIO

from core.io.journey_prompt import (
    bold,
    green_bold,
    grey,
    is_color_active,
)
from core.io.terminal_input import drain_paste_burst
from pipeline.control.resume_context import (
    ActiveFollowupChild,
    ResumeIntentOptions,
    ResumeMode,
)


@dataclass(frozen=True)
class PromptedResumeIntent:
    """The user's choice after a resume-intent prompt.

    * ``mode is None`` — user picked "exit" (or aborted with Ctrl-C/Ctrl-D).
    * ``mode == CHECKPOINT`` — continue a run from checkpoint.
    * ``mode == FOLLOWUP`` — new run; ``task`` holds the follow-up task.

    ``resume_run_id`` is set only when the operator explicitly chose to
    resume an *active follow-up child* run instead of the parent they
    named; the caller switches the resume target to it (never silently).
    """

    mode: ResumeMode | None
    task: str | None = None
    resume_run_id: str | None = None


def _important(text: str, *, color: bool) -> str:
    return bold(text, color=color)


def _default(text: str, *, color: bool) -> str:
    return green_bold(text, color=color)


def _help(text: str, *, color: bool) -> str:
    return grey(text, color=color)


def _read_choice(
    *,
    prompt: str,
    valid: list[str],
    default: str,
    stdin: TextIO,
    stdout: TextIO,
) -> str | None:
    """Read a single-letter choice. Returns ``None`` on EOF/Ctrl-C.

    Empty input → ``default``. Unknown input re-prompts.
    """
    while True:
        stdout.write(prompt)
        stdout.flush()
        try:
            line = stdin.readline()
        except KeyboardInterrupt:
            stdout.write("\n")
            return None
        if not line:
            # EOF (Ctrl-D or closed stream).
            stdout.write("\n")
            return None
        choice = line.strip().lower()
        if not choice:
            return default
        if choice in valid:
            return choice
        stdout.write(f"  Please answer one of: {', '.join(valid)}\n")


def _read_followup_task(
    *,
    stdin: TextIO,
    stdout: TextIO,
    color: bool,
) -> str | None:
    """Read the follow-up task body. Reprompts on empty input.

    Returns ``None`` when the user aborts (EOF/Ctrl-C/empty after retry).

    The body is free text, so a pasted multi-line / multi-paragraph block must
    be captured whole — :func:`drain_paste_burst` consumes the rest of the
    paste burst that ``readline`` would otherwise truncate (and leak to the
    shell once the run exits).
    """
    attempts = 0
    while attempts < 2:
        stdout.write(_important("Follow-up task:", color=color))
        stdout.write("\n")
        stdout.write(_help("  empty line to cancel", color=color))
        stdout.write("\n> ")
        stdout.flush()
        try:
            line = stdin.readline()
        except KeyboardInterrupt:
            stdout.write("\n")
            return None
        if not line:
            stdout.write("\n")
            return None
        task = drain_paste_burst(line, stdin=stdin).strip()
        if task:
            return task
        attempts += 1
    return None


def _prompt_with_active_followup(
    *,
    run_id: str,
    options: ResumeIntentOptions,
    active: ActiveFollowupChild,
    si: TextIO,
    so: TextIO,
    color: bool,
) -> PromptedResumeIntent:
    """Menu that leads with a recommended 'Resume active follow-up' option.

    Dynamically numbered so the recommended child resume is always first
    (and the default), followed by the parent-checkpoint option (when
    available), starting a fresh follow-up, and exit. The chosen option is
    visible and confirmed; nothing auto-switches.
    """
    handoff_hint = (
        f", active handoff {active.active_handoff_id}"
        if active.active_handoff_id else ""
    )
    so.write(
        f"\n{_important(f'Run {run_id} has an in-progress follow-up: ', color=color)}"
        f"{_important(active.child_run_id, color=color)} "
        f"{_help(f'(status: {active.child_status}{handoff_hint})', color=color)}\n"
    )
    so.write(_important("What do you want to do?", color=color))
    so.write("\n")

    # (key, mode, resume_run_id, needs_task)
    entries: list[tuple[str, ResumeMode, str | None, bool]] = []
    n = 1
    so.write(
        f"  {_important(f'{n}) Resume active follow-up {active.child_run_id}', color=color)}  "
        f"{_default('[recommended]', color=color)}\n"
        f"{_help('     Continue the in-progress follow-up run from its checkpoint.', color=color)}\n"
    )
    entries.append((str(n), ResumeMode.CHECKPOINT, active.child_run_id, False))
    n += 1
    if options.can_checkpoint:
        so.write(
            f"  {_important(f'{n}) Resume parent {run_id} from checkpoint', color=color)}\n"
        )
        entries.append((str(n), ResumeMode.CHECKPOINT, None, False))
        n += 1
    so.write(
        f"  {_important(f'{n}) Start a new follow-up using {run_id} as context', color=color)}\n"
    )
    entries.append((str(n), ResumeMode.FOLLOWUP, None, True))
    n += 1
    exit_key = str(n)
    so.write(f"  {_important(f'{exit_key}) Exit', color=color)}\n")

    valid = [k for (k, *_rest) in entries] + [exit_key]
    choice = _read_choice(
        prompt=_important(f"Choice [{'/'.join(valid)}]: ", color=color),
        valid=valid,
        default="1",
        stdin=si, stdout=so,
    )
    if choice is None or choice == exit_key:
        return PromptedResumeIntent(mode=None)
    for (key, mode, resume_run_id, needs_task) in entries:
        if key != choice:
            continue
        if needs_task:
            task = _read_followup_task(stdin=si, stdout=so, color=color)
            if task is None:
                return PromptedResumeIntent(mode=None)
            return PromptedResumeIntent(mode=ResumeMode.FOLLOWUP, task=task)
        return PromptedResumeIntent(mode=mode, resume_run_id=resume_run_id)
    return PromptedResumeIntent(mode=None)


def prompt_resume_intent(
    *,
    run_id: str,
    options: ResumeIntentOptions,
    active_followup: ActiveFollowupChild | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> PromptedResumeIntent:
    """Ask the operator how to handle ``--resume RUN_ID`` with no task.

    Caller responsibility: only invoke when stdin is a TTY and
    ``--no-interactive`` is unset. This function does not check those
    conditions — keeps it test-friendly and frontend-agnostic.

    When ``active_followup`` is supplied (a newer, still-unfinished
    follow-up child of ``run_id``), a recommended "Resume active
    follow-up <id>" option is offered first. Choosing it returns
    ``resume_run_id`` set to the child id; the caller switches the resume
    target explicitly — there is no silent auto-switch.

    Returns a :class:`PromptedResumeIntent`. ``mode=None`` means the
    operator chose to exit (the caller should ``sys.exit(0)``).
    """
    si = stdin if stdin is not None else sys.stdin
    so = stdout if stdout is not None else sys.stdout
    color = is_color_active(so)

    if active_followup is not None:
        return _prompt_with_active_followup(
            run_id=run_id, options=options, active=active_followup,
            si=si, so=so, color=color,
        )

    # State-aware headline. Avoid saying "paused" for a done run (the
    # old wording read "Run X is paused (status: done). This run is
    # already done." — contradictory and duplicated).
    if options.reason == "terminal-success":
        so.write(
            f"\n{_important(f'Run {run_id} has already completed.', color=color)}\n"
        )
    elif options.reason == "terminal-halt":
        so.write(f"\n{_important(f'Run {run_id} was halted.', color=color)}\n")
    elif options.reason == "incomplete-parent":
        status_hint = (
            f" (status: {options.parent_status})"
            if options.parent_status else ""
        )
        so.write(
            f"\n{_important(f'Run {run_id} did not finish{status_hint}.', color=color)}\n"
        )
    else:
        so.write(f"\n{_important(f'Run {run_id}.', color=color)}\n")

    if not options.can_checkpoint and not options.can_followup:
        # No legitimate choice — caller already missed the early-error
        # path; bail out as if the user picked exit.
        so.write(
            _help(
                "  Nothing to resume: parent meta is missing or unusable.\n",
                color=color,
            )
        )
        return PromptedResumeIntent(mode=None)

    if options.can_checkpoint and options.can_followup:
        so.write(_important("What do you want to do?", color=color))
        so.write("\n")
        so.write(
            f"  {_important('1) Resume from checkpoint', color=color)}  "
            f"{_default('[default]', color=color)}\n"
            f"{_help('     Continue the same run from saved checkpoints; remaining ', color=color)}"
            f"{_help('phases start in fresh provider sessions with persisted run ', color=color)}"
            f"{_help('context.', color=color)}\n"
        )
        so.write(
            f"  {_important('2) Start a follow-up using this run as context', color=color)}\n"
            f"{_help('     Start a new run and resume parent provider sessions when ', color=color)}"
            f"{_help('the active session mode supports it.', color=color)}\n"
        )
        so.write(f"  {_important('3) Exit', color=color)}\n")
        choice = _read_choice(
            prompt=_important("Choice [1/2/3]: ", color=color),
            valid=["1", "2", "3"],
            default="1",
            stdin=si, stdout=so,
        )
        if choice is None or choice == "3":
            return PromptedResumeIntent(mode=None)
        if choice == "1":
            return PromptedResumeIntent(mode=ResumeMode.CHECKPOINT)
        # choice == "2"
        task = _read_followup_task(stdin=si, stdout=so, color=color)
        if task is None:
            return PromptedResumeIntent(mode=None)
        return PromptedResumeIntent(mode=ResumeMode.FOLLOWUP, task=task)

    # Terminal parents: only follow-up makes sense.
    if options.can_followup and not options.can_checkpoint:
        if options.checkpoint_blocked_reason:
            so.write(
                _help(
                    "  Checkpoint resume is unavailable: "
                    f"{options.checkpoint_blocked_reason}\n",
                    color=color,
                )
            )
        so.write(_important("What do you want to do?", color=color))
        so.write("\n")
        so.write(
            f"  {_important('1) Start a follow-up using this run as context', color=color)}  "
            f"{_default('[default]', color=color)}\n"
            f"{_help('     Start a new run and resume parent provider sessions when ', color=color)}"
            f"{_help('the active session mode supports it.', color=color)}\n"
        )
        so.write(f"  {_important('2) Exit', color=color)}\n")
        choice = _read_choice(
            prompt=_important("Choice [1/2]: ", color=color),
            valid=["1", "2"],
            default="1",
            stdin=si, stdout=so,
        )
        if choice is None or choice == "2":
            return PromptedResumeIntent(mode=None)
        task = _read_followup_task(stdin=si, stdout=so, color=color)
        if task is None:
            return PromptedResumeIntent(mode=None)
        return PromptedResumeIntent(mode=ResumeMode.FOLLOWUP, task=task)

    # Only checkpoint is offered (defensive — current logic always
    # offers follow-up alongside, but keep the branch sound).
    return PromptedResumeIntent(mode=ResumeMode.CHECKPOINT)


def should_prompt_for_resume_intent(
    *,
    resume: str | None,
    explicit_task: str | None,
    explicit_task_file: str | None,
    no_interactive: bool,
    stdin: TextIO | None = None,
) -> bool:
    """Caller-side guard: is it appropriate to invoke the prompt?

    True only when ``--resume`` is set, no task is supplied, the user
    did not pass ``--no-interactive``, and stdin is a TTY.
    """
    if not resume:
        return False
    if explicit_task or explicit_task_file:
        return False
    if no_interactive:
        return False
    si = stdin if stdin is not None else sys.stdin
    return bool(getattr(si, "isatty", lambda: False)())


__all__ = [
    "PromptedResumeIntent",
    "prompt_resume_intent",
    "should_prompt_for_resume_intent",
]
