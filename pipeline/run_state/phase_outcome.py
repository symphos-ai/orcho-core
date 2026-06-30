"""Phase-outcome classification (Stage 0 brain).

A single pure predicate, :func:`is_phase_checkpoint_success`, answering one
question: does a ``phase.end`` outcome token mean the phase *successfully
completed* and may therefore be recorded/projected as a finished checkpoint?

Invariant: checkpoint completion equals a *successful* outcome, never the mere
presence of a ``phase.end`` event. A phase that halted, failed, was rejected,
errored, ended incomplete, produced no verdict, or paused for an operator
handoff has a ``phase.end`` record but is *not* a completed checkpoint. Treating
"a phase.end exists" as "the phase completed" is the bug this predicate closes:
it let a halted/handoff IMPLEMENT be skipped on resume and a run march on to
review against partial work.

Why a strict allowlist rather than a denylist: the set of *success* tokens is
small, closed, and audited (see below); the set of terminal/handoff/failure
tokens grows over time (new halt reasons, new operator-handoff variants, new
synthetic markers). A denylist silently misclassifies every *new* non-success
token as "completed". An allowlist fails safe — an unknown token is treated as
*not* a completed checkpoint until it is deliberately added here.

Audited success tokens (project pipeline ``phase.end`` writers):
  * ``pipeline.project.profile_dispatch.emit_phase_log_end`` emits exactly
    ``"ok"`` (completed), ``"skipped: <reason>"`` (intentionally empty banner),
    or ``"halted: <reason>"`` (halt). Every project checkpoint phase routes its
    ``phase.end`` through this writer (``pipeline.project.run._on_phase_end``).
  * The ``HYPOTHESIS`` prelude and the ``cross_*`` writers emit free-form
    outcome phrases (``"direction validated"``, ``"approved"``, a verdict
    string), but those are *not* project checkpoint phases — HYPOTHESIS is a
    plan-step prelude and the ``cross_*`` flow carries its own run-state. They
    are deliberately *not* in this allowlist.

If a future writer introduces a genuinely-completed checkpoint phase whose
success token is neither ``"ok"`` nor ``"skipped*"``, this allowlist (and its
tests) must be widened together with a refreshed emitter audit.

Pure: no I/O, no subprocess, no runtime imports.
"""
from __future__ import annotations


def is_phase_checkpoint_success(outcome: str | None) -> bool:
    """Return ``True`` only when ``outcome`` marks a *completed* checkpoint.

    The outcome is normalized (``None`` → ``""``, surrounding whitespace
    stripped, lower-cased). ``True`` is returned for exactly two shapes:

    * ``"ok"`` — the phase completed successfully.
    * any string starting with ``"skipped"`` — the phase was intentionally
      skipped (e.g. ``"skipped: review clean"``, ``"skipped: completed earlier
      in this run (resumed)"``); a skipped phase is a valid completed checkpoint.

    Everything else returns ``False``, including (non-exhaustively)
    ``"halted: <reason>"``, ``"failed"``, ``"rejected"``, ``"error"``,
    ``"incomplete"``, ``"no_verdict"``, any operator-handoff-required token,
    the synthetic ``"done"``/``"DONE"`` marker, any unknown string, ``None``,
    and ``""``.
    """
    normalized = (outcome or "").strip().lower()
    return normalized == "ok" or normalized.startswith("skipped")


__all__ = ["is_phase_checkpoint_success"]
