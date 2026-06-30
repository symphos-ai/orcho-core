"""
pipeline.control.handoff_prompt — TTY-interactive phase-handoff prompt.

When ``orcho run`` is attached to a real TTY and ``--no-interactive``
is not set, a fired phase handoff can be resolved **in-process** —
the operator picks ``continue`` / ``retry_feedback`` / ``halt`` /
``continue_with_waiver`` at the keyboard and the same subprocess
continues (or terminates) without exiting rc=4 and respawning under
``--resume``.

This module owns the *prompt* surface only — reading stdin, rendering
the action menu, validating input. The audit-trail invariant from
:adr:`0031` is preserved by the orchestrator: every interactive
decision is recorded through ``sdk.phase_handoff.phase_handoff_decide``
*before* the runner applies the action. The prompt helper itself
never touches the filesystem or calls SDK functions — keeping it pure
makes ``stdin`` mocking straightforward in unit tests.

The runner remains unaware of stdin/TTY per the contract laid out in
:doc:`/architecture/phase_lifecycle` — interactive routing lives one
layer up at the orchestrator. The default
:func:`pipeline.runtime.handoff.pause_resolver` still returns
``PAUSE`` for every transport; the orchestrator's post-dispatch loop
then chooses between persist+rc=4 (non-interactive) and prompt+decide
+ in-process resume (interactive).

Sentinel encoded as :data:`HANDOFF_PROMPT_ABORTED`: returned when the
operator presses Ctrl-D, Ctrl-C, or otherwise abandons the prompt —
the orchestrator treats that as "leave the run paused as if
non-interactive" so the operator can come back later via SDK / MCP /
Web. Never silently downgrades to a default action.
"""

from __future__ import annotations

import select
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TextIO

from core.io.ansi import C, is_color_active, paint
from pipeline.control.handoff_banners import render_advice_summary
from pipeline.control.handoff_labels import render_round_label
from pipeline.control.implement_handoff_digest import (
    classify_implement_incomplete,
    render_implement_incomplete_digest,
)

if TYPE_CHECKING:
    from pipeline.runtime.handoff import PhaseHandoffRequested


_MAX_INVALID_INPUT_RETRIES: int = 3
"""How many times to re-prompt on malformed input before giving up.

Three retries match the resume-intent prompter's discipline (see
``pipeline.control.resume_prompt``). Exceeded → return
``HANDOFF_PROMPT_ABORTED`` so the run stays paused rather than
silently picking a default action.
"""

# Single-byte action codes shown in the prompt. Mirrors the SDK
# action vocabulary verbatim so the operator never has to translate
# between UI label and persisted ``action`` field.
_ACTION_LABELS: dict[str, str] = {
    "continue":       "1) ✅ continue            — accept the verdict, run remaining phases",
    "retry_feedback": "2) 🔁 retry_feedback      — one extra retry round with human feedback",
    "halt":           "3) 🛑 halt                — terminate the run synchronously",
    "continue_with_waiver": "4) 📝 continue_with_waiver — accept the verdict with a durable operator waiver",
}

_ACTION_BY_KEY: dict[str, str] = {
    "1":              "continue",
    "c":              "continue",
    "continue":       "continue",
    "2":              "retry_feedback",
    "r":              "retry_feedback",
    "retry":          "retry_feedback",
    "retry_feedback": "retry_feedback",
    "3":              "halt",
    "h":              "halt",
    "halt":           "halt",
    "4":                    "continue_with_waiver",
    "w":                    "continue_with_waiver",
    "waiver":               "continue_with_waiver",
    "continue_with_waiver": "continue_with_waiver",
}

# Actions that require a mandatory operator verdict (free text) before
# the decision can be recorded. ``retry_feedback`` injects it into the
# next retry round; ``continue_with_waiver`` records it as the durable
# waiver verdict. Both reuse :func:`_read_feedback`.
_FEEDBACK_REQUIRED_ACTIONS: frozenset[str] = frozenset({
    "retry_feedback",
    "continue_with_waiver",
})

# Advisory pseudo-actions (5 / 6). These are UI-only: they are NOT in any
# ``signal.available_actions`` set and are NEVER passed to
# ``phase_handoff_decide`` — the orchestrator turns ``retry_with_advice`` into
# an ordinary ``retry_feedback`` decision after the advisor runs. They appear
# only when ``advisory_available`` is True, leaving the canonical menu, aliases
# and hint byte-for-byte unchanged otherwise.
_ADVISORY_ACTIONS: frozenset[str] = frozenset({"advice", "retry_with_advice"})

_ADVISORY_ACTION_BY_KEY: dict[str, str] = {
    "5":                 "advice",
    "a":                 "advice",
    "advice":            "advice",
    "6":                 "retry_with_advice",
    "ra":                "retry_with_advice",
    "retry_with_advice": "retry_with_advice",
}

#: Canonical (number, short-name) per advisory pseudo-action for the input
#: hint, appended only in advisory mode.
_ADVISORY_HINT: tuple[tuple[str, str], ...] = (
    ("5", "advice"),
    ("6", "retry_with_advice"),
)

# Advice follow-up sub-menu keys (rendered after the operator picks 5/advice).
_ADVICE_FOLLOWUP_BY_KEY: dict[str, str] = {
    "1":     "apply",
    "apply": "apply",
    "2":     "edit",
    "edit":  "edit",
    "3":     "back",
    "back":  "back",
    "4":     "halt",
    "halt":  "halt",
}

# Action-line input that contains internal whitespace, or runs much
# longer than the longest action token (``continue_with_waiver`` = 20
# chars), is almost certainly pasted feedback landing where an action
# was expected. A small margin above the longest token avoids false
# positives on the canonical keys.
_ACTION_INPUT_MAX_LEN: int = 24


class FeedbackFileError(Exception):
    """Raised when ``--feedback-file`` cannot supply valid operator feedback.

    Covers a missing / unreadable file and an effectively empty file —
    the same non-empty contract the interactive feedback reader enforces.
    """


# Process-global feedback-file override. A pipeline run is a process-wide
# singleton (one orchestrator per CLI invocation, mirroring the
# module-state model in ``core.observability.events``), so the
# single-project CLI can register a ``--feedback-file`` here and the
# in-process prompt — invoked several layers down in the orchestrator —
# consumes it without threading the path through every intermediate
# frame. ``None`` keeps the interactive multi-line reader as the default.
_feedback_file_override: str | None = None


def set_feedback_file_override(path: str | None) -> None:
    """Register (or clear) the file supplying feedback for the next prompt.

    When set, :func:`prompt_phase_handoff_action` reads the operator
    verdict for ``retry_feedback`` / ``continue_with_waiver`` from this
    file instead of the TTY — the safe non-interactive path for long
    feedback that an operator would otherwise have to paste.
    """
    global _feedback_file_override
    _feedback_file_override = path or None


def load_feedback_file(path: str) -> str:
    """Read + validate operator feedback from ``path``.

    Returns the stripped file contents. Raises :class:`FeedbackFileError`
    when the file is missing / unreadable or effectively empty, matching
    the non-empty contract the SDK enforces for the feedback-required
    actions.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise FeedbackFileError(
            f"--feedback-file {path!r} could not be read: {exc}"
        ) from exc
    feedback = text.strip()
    if not feedback:
        raise FeedbackFileError(
            f"--feedback-file {path!r} is empty; feedback must be non-empty."
        )
    return feedback


@dataclass(frozen=True, slots=True)
class HandoffDecisionInput:
    """Operator's prompt response, ready for ``phase_handoff_decide``.

    The orchestrator passes ``action`` / ``feedback`` / ``note``
    verbatim to the SDK; the prompt helper never writes the artifact
    itself.
    """

    action: str
    feedback: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class AdviceActionRequest:
    """Operator picked an advisory pseudo-action (5/advice or 6/retry_with_advice).

    Third return type of :func:`prompt_phase_handoff_action`. The orchestrator
    routes this to the read-only advisor (``advice``) or generates repair
    feedback and applies an ordinary ``retry_feedback`` decision
    (``retry_with_advice``). The prompt reads no feedback / note for these — the
    follow-up sub-menu (see :func:`prompt_advice_followup`) drives the rest.
    """

    kind: Literal["advice", "retry_with_advice"]


@dataclass(frozen=True, slots=True)
class AdviceFollowup:
    """Operator's choice in the advice follow-up sub-menu.

    ``action='apply'`` applies the advisor's retry: ``feedback`` is ``None`` to
    apply the advisor's own generated feedback verbatim, or the operator's
    edited replacement text. ``back`` returns to the main menu (no decision,
    no state change). ``halt`` requests a halt decision.
    """

    action: Literal["apply", "back", "halt"]
    feedback: str | None = None


class _Aborted:
    """Sentinel type for the "operator did not decide" outcome.

    Distinct from ``None`` so callers can tell apart "abandoned the
    prompt (leave paused)" from "no prompt was offered". The
    singleton :data:`HANDOFF_PROMPT_ABORTED` is the public
    instance.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover — trivial
        return "HANDOFF_PROMPT_ABORTED"


HANDOFF_PROMPT_ABORTED: _Aborted = _Aborted()


def should_prompt_for_phase_handoff(
    *,
    no_interactive: bool,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> bool:
    """Decide whether the interactive phase-handoff prompt should fire.

    Returns ``True`` only when:

    * ``no_interactive`` is ``False`` (CLI ``--no-interactive`` flag
      not set; matches the resume-intent prompter's gating).
    * Both ``stdin`` and ``stdout`` are attached to a TTY. The
      attached-stdout check matters because ``orcho run`` piped into
      a file is a non-interactive run by intent — silently popping
      a prompt into a non-interactive subprocess would deadlock the
      pipeline.

    The check is deliberately strict: any uncertainty (missing
    ``isatty`` attribute on a fake stream, ``OSError`` from a closed
    file descriptor) returns ``False`` so the run pauses + persists
    rather than dropping into a prompt the operator can't see.
    """
    if no_interactive:
        return False
    actual_stdin = stdin or sys.stdin
    actual_stdout = stdout or sys.stdout
    try:
        if not actual_stdin.isatty():
            return False
        if not actual_stdout.isatty():
            return False
    except (AttributeError, OSError, ValueError):
        return False
    return True


def prompt_phase_handoff_action(
    signal: PhaseHandoffRequested,
    *,
    advisory_available: bool = False,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> HandoffDecisionInput | AdviceActionRequest | _Aborted:
    """Render the action menu, read the operator's choice.

    Validates the chosen action against
    ``signal.available_actions`` — even if the operator types a
    canonical action, the prompt refuses any action the runtime did
    not publish as available for this pause. Action availability is
    runtime-decided (see :class:`pipeline.runtime.handoff.PhaseHandoffRequested`);
    the prompt is a thin reader, not a policy.

    ``advisory_available`` is computed by the orchestrator (via
    ``handoff_advice.advice_actions_available``) — the prompt never decides
    that policy and never imports ``pipeline.project``. When ``True`` the menu
    gains advisory items ``5) advice`` / ``6) retry_with_advice`` (and matching
    aliases + hint); selecting one returns an :class:`AdviceActionRequest`
    without reading feedback / note. When ``False`` the menu, aliases and hint
    are byte-for-byte unchanged.

    For ``retry_feedback`` and ``continue_with_waiver``, reads
    multi-line feedback (the operator verdict) until an empty line.
    Empty feedback is rejected: the SDK contract requires a non-empty
    string for both, and offering the prompt would be misleading if we
    accepted blank input.

    Returns :data:`HANDOFF_PROMPT_ABORTED` on Ctrl-C / Ctrl-D /
    exhausted retries; the orchestrator treats this as "leave the
    run paused" rather than picking a default.
    """
    actual_stdin = stdin or sys.stdin
    actual_stdout = stdout or sys.stdout

    _print_summary(signal, actual_stdout, advisory_available=advisory_available)

    available = set(signal.available_actions)
    action = _read_action(
        actual_stdin, actual_stdout, available,
        advisory_available=advisory_available,
    )
    if isinstance(action, _Aborted):
        return action
    if action in _ADVISORY_ACTIONS:
        # UI pseudo-action: hand back to the orchestrator for the advisor
        # flow. No feedback / note is read here.
        return AdviceActionRequest(kind=action)  # type: ignore[arg-type]

    feedback: str | None = None
    if action in _FEEDBACK_REQUIRED_ACTIONS:
        if _feedback_file_override is not None:
            # Non-interactive long-feedback path: take the operator
            # verdict from the registered file instead of the TTY.
            try:
                feedback = load_feedback_file(_feedback_file_override)
            except FeedbackFileError as exc:
                print(f"    {exc}", file=actual_stdout)
                return HANDOFF_PROMPT_ABORTED
            print(
                f"  Using {action} feedback from file "
                f"{_feedback_file_override!r} ({len(feedback)} chars).",
                file=actual_stdout,
            )
        else:
            feedback = _read_feedback(actual_stdin, actual_stdout, action)
            if isinstance(feedback, _Aborted):
                return feedback

    note = _read_note(actual_stdin, actual_stdout, action)
    if isinstance(note, _Aborted):
        return note

    return HandoffDecisionInput(action=action, feedback=feedback, note=note or None)


# ── private ─────────────────────────────────────────────────────────────────


def _print_summary(
    signal: PhaseHandoffRequested,
    out: TextIO,
    *,
    advisory_available: bool = False,
) -> None:
    """Render the handoff context before the menu."""
    label = render_round_label(
        phase=signal.phase,
        round=signal.round,
        loop_max_rounds=signal.loop_max_rounds,
        rejected_again=signal.round > signal.loop_max_rounds and not signal.approved,
    )
    print("", file=out)
    print("═" * 68, file=out)
    print(f"  Phase handoff — {label}", file=out)
    print("═" * 68, file=out)
    if signal.phase == "implement" and signal.trigger == "incomplete":
        # Decision-first digest: the unclosed subtasks / unmet criteria and the
        # recommended action come before the verbose raw transcript and the
        # demoted handoff metadata. Every other phase / trigger keeps the
        # byte-for-byte legacy layout below.
        _print_implement_incomplete_summary(signal, out)
    else:
        _print_handoff_metadata(signal, out)
    print("", file=out)
    print("  Choose action:", file=out)
    for action_name in (
        "continue", "retry_feedback", "halt", "continue_with_waiver",
    ):
        if action_name in signal.available_actions:
            print(f"    {_action_label(action_name, signal)}", file=out)
    if advisory_available:
        print(
            "    5) 💡 advice — explain the rejection and recommend "
            "continue / retry / halt / waiver",
            file=out,
        )
        print(
            "    6) 🤖 retry_with_advice — generate repair feedback from the "
            "findings and retry",
            file=out,
        )
    print("", file=out)


def _print_handoff_metadata(signal: PhaseHandoffRequested, out: TextIO) -> None:
    """Render the legacy metadata + raw-output block (every non-digest path)."""
    print(f"  handoff_id : {signal.handoff_id}", file=out)
    print(f"  policy     : {signal.type.value}", file=out)
    print(f"  trigger    : {signal.trigger}", file=out)
    print(f"  verdict    : {signal.verdict}", file=out)
    if signal.last_output:
        last = signal.last_output.strip()
        if len(last) > 320:
            last = last[:317] + "..."
        print("", file=out)
        print(f"  {_last_output_label(signal.phase)}:", file=out)
        for line in last.splitlines():
            print(f"    {line}", file=out)


#: Raw implementation transcript is secondary under the digest, so it is
#: truncated harder than the 320-char legacy budget.
_DIGEST_RAW_OUTPUT_MAX_LEN: int = 200


def _print_implement_incomplete_summary(
    signal: PhaseHandoffRequested, out: TextIO,
) -> None:
    """Render the implement+incomplete digest, then a demoted ``Details`` block.

    The digest (unclosed subtasks / unmet criteria + recommended action) is
    printed first; the handoff metadata and the raw implementation transcript
    follow under a secondary ``Details`` heading, truncated harder than the
    legacy raw-output budget so the decision stays at the top.
    """
    digest = classify_implement_incomplete(
        signal.artifacts, signal.last_output or "", signal.available_actions,
    )
    print("", file=out)
    print(
        render_implement_incomplete_digest(digest, color=is_color_active(out)),
        file=out,
    )
    print("", file=out)
    print("  Details:", file=out)
    print(f"    handoff_id : {signal.handoff_id}", file=out)
    print(f"    policy     : {signal.type.value}", file=out)
    print(f"    trigger    : {signal.trigger}", file=out)
    print(f"    verdict    : {signal.verdict}", file=out)
    if signal.last_output:
        last = signal.last_output.strip()
        if len(last) > _DIGEST_RAW_OUTPUT_MAX_LEN:
            last = last[: _DIGEST_RAW_OUTPUT_MAX_LEN - 3] + "..."
        print("", file=out)
        print(f"    {_last_output_label(signal.phase)}:", file=out)
        for line in last.splitlines():
            print(f"      {line}", file=out)


def _last_output_label(phase: str) -> str:
    if phase == "implement":
        return "Last implementation output"
    if phase in {"validate_plan", "review_changes", "final_acceptance"}:
        return "Last reviewer output"
    return "Last phase output"


def _action_label(action: str, signal: PhaseHandoffRequested) -> str:
    """Render action help with phase-specific retry semantics."""
    if action == "retry_feedback":
        if signal.phase == "implement":
            return (
                "2) 🔁 retry_feedback      — retry incomplete implementation "
                "subtasks with human feedback"
            )
        if signal.phase == "review_changes":
            return (
                "2) 🔁 retry_feedback      — run one repair_changes → "
                "review_changes retry with human feedback"
            )
        if signal.phase == "validate_plan":
            return (
                "2) 🔁 retry_feedback      — run one plan → validate_plan "
                "retry with human feedback"
            )
    if action == "continue_with_waiver":
        verdict = signal.verdict or "verdict"
        return (
            "4) 📝 continue_with_waiver — accept the "
            f"{verdict} verdict with a durable operator waiver"
        )
    return _ACTION_LABELS[action]


#: Canonical (number, short-name) per action for the input hint. The
#: prompt hint must reflect the LIVE ``available_actions`` — when an
#: action is narrowed out of the payload (e.g. ``retry_feedback``
#: before A2c) it must not appear as a phantom ``2/retry`` option.
_ACTION_HINT: dict[str, tuple[str, str]] = {
    "continue":       ("1", "continue"),
    "retry_feedback": ("2", "retry"),
    "halt":           ("3", "halt"),
    "continue_with_waiver": ("4", "waiver"),
}


def _action_hint(available: set[str], *, advisory_available: bool = False) -> str:
    """Render the ``Action [...]`` input hint from the available actions
    in canonical order — never advertising an action the payload did
    not offer. In advisory mode the advisory pseudo-actions (5/6) are
    appended; otherwise the hint is byte-for-byte unchanged."""
    nums = [
        n for a, (n, _) in _ACTION_HINT.items() if a in available
    ]
    names = [
        s for a, (_, s) in _ACTION_HINT.items() if a in available
    ]
    if advisory_available:
        nums += [n for n, _ in _ADVISORY_HINT]
        names += [s for _, s in _ADVISORY_HINT]
    return f"  Action [{'/'.join(nums)} or {'/'.join(names)}]: "


def _looks_like_pasted_feedback(key: str) -> bool:
    """Heuristic: the action line looks like pasted prose, not an action.

    An action is always a single short token (a number, a letter, or a
    canonical name). Internal whitespace or a length well past the
    longest action token signals the operator pasted multi-line feedback
    where an action was expected.
    """
    return any(ch.isspace() for ch in key) or len(key) > _ACTION_INPUT_MAX_LEN


def _read_action(
    stdin: TextIO, stdout: TextIO, available: set[str],
    *,
    advisory_available: bool = False,
) -> str | _Aborted:
    # In advisory mode an extended key map + valid set accept the advisory
    # pseudo-actions; otherwise both are the canonical objects, so the
    # non-advisory path stays byte-for-byte unchanged.
    key_map = (
        {**_ACTION_BY_KEY, **_ADVISORY_ACTION_BY_KEY}
        if advisory_available else _ACTION_BY_KEY
    )
    valid = available | _ADVISORY_ACTIONS if advisory_available else available
    _hint = _action_hint(available, advisory_available=advisory_available)
    for _attempt in range(_MAX_INVALID_INPUT_RETRIES):
        try:
            print(_hint, end="", file=stdout)
            stdout.flush()
            raw = stdin.readline()
        except (KeyboardInterrupt, EOFError):
            print("", file=stdout)
            return HANDOFF_PROMPT_ABORTED
        if raw == "":  # EOF without newline
            return HANDOFF_PROMPT_ABORTED
        # Read only this one action line; never concatenate it with a
        # following (possibly stale) multi-line feedback buffer. Feedback
        # is read separately, after a valid action is chosen.
        key = raw.strip().lower()
        if not key:
            print("    (empty input — please type a number or an action name)",
                  file=stdout)
            continue
        action = key_map.get(key)
        if action is None:
            if _looks_like_pasted_feedback(key):
                # Pasted feedback landed where an action was expected.
                # Show a targeted message instead of echoing the whole
                # pasted tail as ``Unknown action '<...long prose...>'``.
                print(
                    "    That looks like pasted feedback, not an action. "
                    "Pick an action by number or name first (you'll be "
                    "prompted for feedback right after). For a long verdict, "
                    "re-run non-interactively with --feedback-file.",
                    file=stdout,
                )
                continue
            print(f"    Unknown action {key!r}. {_hint.strip()}", file=stdout)
            continue
        if action not in valid:
            print(f"    Action {action!r} is not in this handoff's "
                  f"available_actions: {sorted(available)!r}.",
                  file=stdout)
            continue
        return action
    print("    Too many invalid attempts — leaving run paused.", file=stdout)
    return HANDOFF_PROMPT_ABORTED


def _read_feedback(
    stdin: TextIO, stdout: TextIO, action: str = "retry_feedback",
) -> str | _Aborted:
    print("", file=stdout)
    if action == "continue_with_waiver":
        print(
            "  Operator verdict for the waiver (required for "
            "continue_with_waiver). End with an empty line:", file=stdout,
        )
    else:
        print("  Feedback for the next plan round (required for "
              "retry_feedback). End with an empty line:", file=stdout)
    lines: list[str] = []
    try:
        while True:
            line = stdin.readline()
            if line == "":  # EOF — accept if we already have content
                break
            stripped = line.rstrip("\n")
            if stripped == "":
                if lines:
                    if _has_buffered_input(stdin):
                        lines.append("")
                        continue
                    break
                # First line empty on a fresh feedback prompt → abort
                # immediately. Looping on ``continue`` here would
                # silently swallow the Enter keypress in a live TTY
                # (no EOF arrives after a blank line, so the operator
                # sees the cursor wait with no feedback). The SDK
                # contract requires a non-empty string for
                # ``retry_feedback`` anyway, so there is no recovery
                # path that doesn't either (a) accept blank input —
                # which the SDK will reject — or (b) loop forever.
                # The honest move is to abort with a visible message
                # so the operator knows the run is paused and can
                # come back via SDK / MCP / Web.
                print(
                    f"    Feedback is empty — {action} requires a "
                    "non-empty string. Leaving run paused.",
                    file=stdout,
                )
                return HANDOFF_PROMPT_ABORTED
            lines.append(stripped)
    except (KeyboardInterrupt, EOFError):
        print("", file=stdout)
        return HANDOFF_PROMPT_ABORTED
    feedback = "\n".join(lines).strip()
    if not feedback:
        # Reachable when EOF (live TTY closed) lands before any
        # content. Mirrors the blank-first-line abort message so
        # operators see the same explanation regardless of which way
        # they exited the prompt.
        print(f"    Feedback is empty — {action} requires a non-empty "
              "string. Leaving run paused.", file=stdout)
        return HANDOFF_PROMPT_ABORTED
    return feedback


def _has_buffered_input(stdin: TextIO) -> bool:
    """Return whether more input is already buffered after a blank line.

    Interactive feedback uses a blank line as the terminator. A pasted
    multi-paragraph verdict also contains blank lines, though, and treating the
    first paragraph break as the terminator leaves the rest of the paste to be
    consumed by the audit-note prompt or even the operator's shell. If more
    input is already buffered, the blank line is part of the pasted feedback.
    """
    try:
        pos = stdin.tell()
        chunk = stdin.read(1)
        stdin.seek(pos)
        return chunk != ""
    except (AttributeError, OSError, ValueError):
        pass

    try:
        readable, _, _ = select.select([stdin], [], [], 0.03)
    except (OSError, ValueError, TypeError):
        return False
    return bool(readable)


def _read_note(
    stdin: TextIO, stdout: TextIO, action: str,
) -> str | _Aborted:
    print("", file=stdout)
    print("  Audit note (optional, press Enter for default): ", end="", file=stdout)
    stdout.flush()
    try:
        raw = stdin.readline()
    except (KeyboardInterrupt, EOFError):
        print("", file=stdout)
        return HANDOFF_PROMPT_ABORTED
    if raw == "":  # EOF — accept default
        return f"orcho-cli tty {action}"
    note = raw.strip()
    if not note:
        return f"orcho-cli tty {action}"
    return note


def prompt_advice_followup(
    *,
    recommended_action: str,
    confidence: str,
    rationale: str,
    retry_feedback_preview: str,
    risks: Sequence[str] = (),
    expected_files: Sequence[str] = (),
    operator_note: str = "",
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> AdviceFollowup | _Aborted:
    """Render the advisor recommendation + sub-menu, read the operator's choice.

    Pure: every input is a primitive (the orchestrator passes the parsed
    advice fields, never the raw reviewer output, so this never duplicates the
    review transcript) and stdin/stdout are injectable. A compact summary
    (rationale / feedback truncated by :func:`render_advice_summary`) precedes
    the sub-menu ``1) apply advice and retry / 2) edit advice / 3) back /
    4) halt``.

    * ``1``/``apply`` → ``AdviceFollowup('apply', None)`` — apply the advisor's
      own generated feedback verbatim.
    * ``2``/``edit`` → print the generated feedback in full, then read a
      multi-line replacement via :func:`_read_feedback`; returns
      ``AdviceFollowup('apply', <edited>)``. Empty edited feedback aborts,
      identically to ``_read_feedback`` elsewhere.
    * ``3``/``back`` → ``AdviceFollowup('back', None)`` — no decision, no state
      change.
    * ``4``/``halt`` → ``AdviceFollowup('halt', None)``.

    Ctrl-C / Ctrl-D / EOF / too many invalid attempts → ``HANDOFF_PROMPT_ABORTED``.
    """
    actual_stdin = stdin or sys.stdin
    actual_stdout = stdout or sys.stdout
    color = is_color_active(actual_stdout)

    print("", file=actual_stdout)
    print(
        render_advice_summary(
            recommended_action=recommended_action,
            confidence=confidence,
            rationale=rationale,
            retry_feedback_preview=retry_feedback_preview,
            risks=risks,
            expected_files=expected_files,
            operator_note=operator_note,
            color=color,
        ),
        file=actual_stdout,
    )
    print("", file=actual_stdout)
    print(f"  {paint('Advice options:', C.CYAN, C.BOLD, color=color)}",
          file=actual_stdout)
    print(
        "    "
        + paint("1)", C.BOLD, color=color)
        + " apply advice and retry",
        file=actual_stdout,
    )
    print("    " + paint("2)", C.BOLD, color=color) + " edit advice",
          file=actual_stdout)
    print("    " + paint("3)", C.BOLD, color=color) + " back",
          file=actual_stdout)
    print("    " + paint("4)", C.BOLD, color=color) + " halt",
          file=actual_stdout)
    print("", file=actual_stdout)

    hint = paint(
        "  Advice [1/2/3/4 or apply/edit/back/halt]: ",
        C.BOLD,
        color=color,
    )
    for _attempt in range(_MAX_INVALID_INPUT_RETRIES):
        try:
            print(hint, end="", file=actual_stdout)
            actual_stdout.flush()
            raw = actual_stdin.readline()
        except (KeyboardInterrupt, EOFError):
            print("", file=actual_stdout)
            return HANDOFF_PROMPT_ABORTED
        if raw == "":  # EOF
            return HANDOFF_PROMPT_ABORTED
        key = raw.strip().lower()
        if not key:
            print("    (empty input — please type a number or a name)",
                  file=actual_stdout)
            continue
        choice = _ADVICE_FOLLOWUP_BY_KEY.get(key)
        if choice is None:
            print(f"    Unknown choice {key!r}. {hint.strip()}",
                  file=actual_stdout)
            continue
        if choice == "apply":
            return AdviceFollowup(action="apply", feedback=None)
        if choice == "edit":
            print("", file=actual_stdout)
            print("  Generated feedback:", file=actual_stdout)
            for line in (retry_feedback_preview or "").splitlines() or [""]:
                print(f"    {line}", file=actual_stdout)
            edited = _read_feedback(actual_stdin, actual_stdout, "retry_feedback")
            if isinstance(edited, _Aborted):
                return edited
            return AdviceFollowup(action="apply", feedback=edited)
        if choice == "back":
            return AdviceFollowup(action="back", feedback=None)
        return AdviceFollowup(action="halt", feedback=None)
    print("    Too many invalid attempts — leaving run paused.",
          file=actual_stdout)
    return HANDOFF_PROMPT_ABORTED


def prompt_confirm(
    question: str,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> bool | _Aborted:
    """Ask a yes/no confirmation; return the answer or the abort sentinel.

    Pure stdin/stdout reader for the low-confidence confirmation gate. ``y`` /
    ``yes`` → ``True``; ``n`` / ``no`` → ``False``. Ctrl-C / Ctrl-D / EOF / too
    many invalid attempts → ``HANDOFF_PROMPT_ABORTED`` so the orchestrator leaves
    the run paused rather than guessing.
    """
    actual_stdin = stdin or sys.stdin
    actual_stdout = stdout or sys.stdout
    for _attempt in range(_MAX_INVALID_INPUT_RETRIES):
        try:
            print(f"  {question} [y/n]: ", end="", file=actual_stdout)
            actual_stdout.flush()
            raw = actual_stdin.readline()
        except (KeyboardInterrupt, EOFError):
            print("", file=actual_stdout)
            return HANDOFF_PROMPT_ABORTED
        if raw == "":  # EOF
            return HANDOFF_PROMPT_ABORTED
        key = raw.strip().lower()
        if key in {"y", "yes"}:
            return True
        if key in {"n", "no"}:
            return False
        print("    Please answer 'y' or 'n'.", file=actual_stdout)
    return HANDOFF_PROMPT_ABORTED


__all__ = [
    "HANDOFF_PROMPT_ABORTED",
    "AdviceActionRequest",
    "AdviceFollowup",
    "FeedbackFileError",
    "HandoffDecisionInput",
    "load_feedback_file",
    "prompt_advice_followup",
    "prompt_confirm",
    "prompt_phase_handoff_action",
    "set_feedback_file_override",
    "should_prompt_for_phase_handoff",
]
