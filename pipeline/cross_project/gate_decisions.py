"""pipeline/cross_project/gate_decisions.py — transport-aware decision
resolver for runner-owned cross gates.

The runner consults this module to decide whether to run, skip, pause,
or abort a gate whose policy is ``manual_confirm``. Resolution order:

1. Explicit ``--decision`` override.
2. Inline TTY prompt when stdin/stdout are TTY and ``--no-interactive``
   is not set.
3. Pending-decision state for non-TTY / MCP / UI / CI.

The runner is responsible for the side effects (writing skipped
entries, persisting ``pending_gate`` state, exiting with the resumable
exit code) — this module is pure and returns the decision verb only.
"""
from __future__ import annotations

import sys
from collections.abc import Mapping
from enum import StrEnum

from core.io.ansi import C
from core.io.journey_prompt import paint
from pipeline.runtime import CrossGatePolicy, CrossGateRunPolicy


class GateDecision(StrEnum):
    """Outcome of resolving a single gate's run-policy at decision time.

    ``RUN``   — execute the gate normally.
    ``SKIP``  — write a skipped entry and continue per ``on_skip``.
    ``PAUSE`` — persist pending-decision state and exit resumably.
    ``ABORT`` — operator (or Ctrl-C) chose to cancel the run cleanly.
    """
    RUN = "run"
    SKIP = "skip"
    PAUSE = "pause"
    ABORT = "abort"


def resolve_gate_decision(
    *,
    gate_name: str,
    policy: CrossGatePolicy,
    cli_overrides: Mapping[str, str],
    interactive_allowed: bool,
    stdin_is_tty: bool,
    stdout_is_tty: bool,
) -> GateDecision:
    """Return the verb the runner should follow for ``gate_name``.

    ``policy.enabled is False`` and ``policy.run == NEVER`` are handled
    upstream (the runner writes a policy-skipped entry without calling
    here); this resolver assumes the gate is otherwise eligible to run.
    """
    override = cli_overrides.get(gate_name)
    if override is not None:
        if override == "run":
            return GateDecision.RUN
        if override == "skip":
            return GateDecision.SKIP
        # Decision-validation happens earlier (operator_decisions); if
        # an unknown value sneaks through here, fail loudly.
        raise ValueError(
            f"unsupported --decision value for {gate_name!r}: "
            f"{override!r}"
        )

    if policy.run in (CrossGateRunPolicy.ALWAYS, CrossGateRunPolicy.AUTO):
        return GateDecision.RUN
    if policy.run is CrossGateRunPolicy.NEVER:
        # Defensive: upstream should have short-circuited; treat as skip
        # rather than fall through.
        return GateDecision.SKIP
    if policy.run is not CrossGateRunPolicy.MANUAL_CONFIRM:
        raise ValueError(
            f"unexpected run policy {policy.run!r} for gate {gate_name!r}"
        )

    if interactive_allowed and stdin_is_tty and stdout_is_tty:
        return _prompt_inline(gate_name)

    return GateDecision.PAUSE


def _prompt_inline(gate_name: str) -> GateDecision:
    """Render the manual-confirm prompt and read one operator response.

    Empty input / ``y`` / ``Y`` → ``RUN``.
    ``s`` / ``S``               → ``SKIP``.
    ``a`` / ``A``               → ``ABORT``.
    Unknown input re-prompts up to three times, then ``ABORT``.
    Ctrl-C raises ``KeyboardInterrupt`` — caller maps to ``ABORT``.

    Styling routes through :func:`core.io.journey_prompt.paint` so
    the prompt obeys the shared color policy (explicit > override >
    auto-detect via TTY + NO_COLOR). The unrecognised-answer reminder
    writes to ``sys.stderr`` and therefore passes
    ``stream=sys.stderr`` per the Terminal color discipline rule
    (see orcho-core/CLAUDE.md).
    """
    prompt = (
        f"Run {paint(gate_name, C.BOLD)}? "
        f"[{paint('Y', C.GREEN, C.BOLD)}] run / "
        f"{paint('[s] skip / [a] abort:', C.GREY)} "
    )
    for _attempt in range(3):
        try:
            answer = input(prompt).strip()
        except EOFError:
            # No more input on a stream that claimed to be a TTY (rare,
            # e.g. piped-through pseudo-terminal). Don't hang; treat as
            # pause so the operator can decide later.
            return GateDecision.PAUSE
        if answer == "" or answer in ("y", "Y"):
            return GateDecision.RUN
        if answer in ("s", "S"):
            return GateDecision.SKIP
        if answer in ("a", "A"):
            return GateDecision.ABORT
        print(
            paint(
                f"  unrecognised answer {answer!r}; "
                f"press Enter / y / s / a.",
                C.GREY,
                stream=sys.stderr,
            ),
            file=sys.stderr,
        )
    return GateDecision.ABORT


__all__ = [
    "GateDecision",
    "resolve_gate_decision",
]
