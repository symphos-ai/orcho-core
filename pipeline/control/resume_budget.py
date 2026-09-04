# SPDX-License-Identifier: Apache-2.0
"""Resume inheritance for the implement/review/repair round budget.

A resume continues an operator's existing run; it does not re-negotiate
it. ``mock`` / ``output_mode`` / profile are already inherited on the
resume paths, and ``max_rounds`` belongs to that same set: a run started
with ``--max-rounds 4`` that is resumed without the flag must keep four
rounds, not silently shrink to the frontend's own default.

The budget's persisted home is the run's own checkpoint store —
:func:`pipeline.project.bootstrap` writes the effective value into
``checkpoints.db`` ``run_meta.config_json`` at bootstrap. Reading it back
from there keeps a single owner: nothing re-derives the budget and no
second copy is introduced on ``meta.json`` / ``run_supervisor.json``.

This module is the one owner of *how* that persisted value is read and
normalised. Both resume frontends use it — the ``orcho-run`` CLI
(:mod:`pipeline.project.cli`) and the SDK launcher
(:mod:`sdk.run_control.launch`) — so the two cannot drift on what counts
as "nothing persisted to inherit".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline.checkpoint import read_run_config


@dataclass(frozen=True, slots=True)
class ResolvedMaxRounds:
    """The budget a run should execute with, and where it came from.

    ``inherited`` is True only when the persisted budget supplied
    ``value`` — the frontends surface that to the operator, because a
    budget that changes without being asked for is exactly the failure
    this resolution exists to close.
    """

    value: int
    inherited: bool


def persisted_max_rounds(run_dir: Path, run_id: str) -> int | None:
    """Return the ``max_rounds`` budget ``run_id`` was launched with, or None.

    None means "nothing persisted to inherit", and every degenerate
    input maps to it rather than raising: a missing / unreadable store, a
    run whose recorded config predates budget capture, and a
    non-positive or non-integer recorded value. Callers are launchers on
    a resume path, where an exception would turn a silently-shrunk
    budget into a failed resume.

    ``bool`` is rejected explicitly: it is an ``int`` subclass, so
    ``True`` would otherwise resolve to a one-round budget.
    """
    config = read_run_config(run_dir / "checkpoints.db", run_id)
    if not config:
        return None
    value = config.get("max_rounds")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 1 else None


def resolve_resume_max_rounds(
    *,
    explicit: int | None,
    run_dir: Path | None,
    run_id: str | None,
    default: int,
) -> ResolvedMaxRounds:
    """Resolve the effective round budget for a fresh run or a resume.

    Order: explicit flag → persisted budget (resume only) → ``default``.

    An explicit ``--max-rounds`` always wins. The persisted value is a
    fallback for the operator who did not restate the flag, not an
    override of the one who did — re-passing the flag on a resume is how
    an operator deliberately widens or narrows the remaining budget, and
    that must keep working.

    ``run_dir`` / ``run_id`` are None for a fresh run and for a
    follow-up, which mints a *new* run rather than continuing the parent;
    both then fall through to ``default``. ``default`` is passed in
    rather than defined here so this module does not become a second
    owner of the frontend's own default (see
    :func:`pipeline.project.app.run_project_pipeline`).
    """
    if explicit is not None:
        return ResolvedMaxRounds(value=explicit, inherited=False)
    if run_dir is not None and run_id:
        value = persisted_max_rounds(run_dir, run_id)
        if value is not None:
            return ResolvedMaxRounds(value=value, inherited=True)
    return ResolvedMaxRounds(value=default, inherited=False)


__all__ = [
    "ResolvedMaxRounds",
    "persisted_max_rounds",
    "resolve_resume_max_rounds",
]
