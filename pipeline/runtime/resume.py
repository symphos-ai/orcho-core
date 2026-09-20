"""Typed runtime input for continuing inside a declarative loop."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LoopResumeCursor:
    """A committed boundary inside one ``LoopStep`` round.

    ``completed_phases`` is an ordered prefix of the loop's declared members.
    The runner skips that prefix only in ``round_n`` and begins at
    ``next_phase``. Later rounds execute the complete loop normally.

    ``done_phases`` covers the rounds that did **not** run their members in the
    declared order. A human-directed review retry repairs first and reviews
    after, so a boundary inside it can have a later member finished while an
    earlier one is still owed — a shape no prefix can express. Members named
    here are skipped in ``round_n`` wherever they sit, with no callbacks, and
    the round then ends at its last unfinished member. Empty for every ordinary
    boundary, where the prefix says everything.
    """

    loop_key: str
    loop_phases: tuple[str, ...]
    round_n: int
    completed_phases: tuple[str, ...]
    next_phase: str
    source: str = "checkpoint"
    done_phases: frozenset[str] = frozenset()


class LoopResumeBlockedError(RuntimeError):
    """Checkpoint state cannot identify one safe loop continuation."""


__all__ = ["LoopResumeBlockedError", "LoopResumeCursor"]
