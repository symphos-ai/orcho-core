"""Typed runtime input for continuing inside a declarative loop."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LoopResumeCursor:
    """A committed boundary inside one ``LoopStep`` round.

    ``completed_phases`` is an ordered prefix of the loop's declared members.
    The runner skips that prefix only in ``round_n`` and begins at
    ``next_phase``. Later rounds execute the complete loop normally.
    """

    loop_key: str
    loop_phases: tuple[str, ...]
    round_n: int
    completed_phases: tuple[str, ...]
    next_phase: str
    source: str = "checkpoint"


class LoopResumeBlockedError(RuntimeError):
    """Checkpoint state cannot identify one safe loop continuation."""


__all__ = ["LoopResumeBlockedError", "LoopResumeCursor"]
