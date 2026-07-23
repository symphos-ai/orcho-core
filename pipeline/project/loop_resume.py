"""Resolve durable checkpoint rows into safe loop continuation cursors."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.checkpoint import (
    CheckpointStore,
    LoopCursorRecord,
    PhaseCheckpointRecord,
)
from pipeline.runtime.profile import LoopStep, Profile
from pipeline.runtime.resume import LoopResumeBlockedError, LoopResumeCursor


@dataclass(frozen=True, slots=True)
class LoopResumeResolution:
    """Pure capability result consumed by dispatch and CLI preflight."""

    cursors: tuple[LoopResumeCursor, ...] = ()
    migrated: tuple[LoopCursorRecord, ...] = ()

    @property
    def by_loop_key(self) -> dict[str, LoopResumeCursor]:
        return {cursor.loop_key: cursor for cursor in self.cursors}


def _loop_steps(profile: Profile) -> tuple[LoopStep, ...]:
    return tuple(step for step in profile.steps if isinstance(step, LoopStep))


def _phase_names(loop: LoopStep) -> tuple[str, ...]:
    return tuple(step.phase for step in loop.steps)


def _round_from_data(data: dict[str, Any] | list[Any]) -> int | None:
    """Read the latest committed attempt/round from a legacy phase payload."""
    candidate: Any = data
    if isinstance(data, list):
        if not data:
            return None
        candidate = data[-1]
    if not isinstance(candidate, dict):
        return None
    for key in ("attempt", "round"):
        value = candidate.get(key)
        if isinstance(value, int) and value >= 1:
            return value
    return None


def _cursor_from_boundary(
    loop: LoopStep,
    *,
    round_n: int,
    completed: tuple[str, ...],
    source: str,
) -> LoopResumeCursor:
    phases = _phase_names(loop)
    if not completed or phases[:len(completed)] != completed:
        raise LoopResumeBlockedError(
            "Checkpoint loop boundary is not an ordered prefix of profile "
            f"loop {loop.round_extras_key!r}: {list(completed)!r}."
        )
    if len(completed) >= len(phases):
        raise LoopResumeBlockedError(
            "Checkpoint stops at the end of loop round "
            f"{round_n} for {loop.round_extras_key!r}; the durable round "
            "disposition is missing, so replay would be ambiguous."
        )
    return LoopResumeCursor(
        loop_key=loop.round_extras_key,
        loop_phases=phases,
        round_n=round_n,
        completed_phases=completed,
        next_phase=phases[len(completed)],
        source=source,
    )


def _from_explicit(
    loop: LoopStep,
    records: tuple[LoopCursorRecord, ...],
) -> LoopResumeCursor | None:
    matching = [r for r in records if r.loop_key == loop.round_extras_key]
    if not matching:
        return None
    latest_round = matching[-1].round_n
    current = [r for r in matching if r.round_n == latest_round]
    phases = _phase_names(loop)
    if any(r.loop_phases != phases for r in current):
        raise LoopResumeBlockedError(
            f"Checkpoint loop shape for {loop.round_extras_key!r} no longer "
            "matches the active profile."
        )
    completed = tuple(r.completed_phase for r in current)
    if completed == phases and current[-1].next_phase is None:
        return None
    cursor = _cursor_from_boundary(
        loop,
        round_n=latest_round,
        completed=completed,
        source="checkpoint_cursor",
    )
    if current[-1].next_phase != cursor.next_phase:
        raise LoopResumeBlockedError(
            f"Checkpoint next phase for {loop.round_extras_key!r} conflicts "
            "with the active profile."
        )
    return cursor


def _from_legacy(
    loop: LoopStep,
    records: tuple[PhaseCheckpointRecord, ...],
) -> tuple[LoopResumeCursor, tuple[LoopCursorRecord, ...]] | None:
    phases = _phase_names(loop)
    relevant: list[tuple[str, int]] = []
    for record in records:
        if record.phase not in phases:
            continue
        round_n = _round_from_data(record.data)
        if round_n is None:
            continue
        relevant.append((record.phase, round_n))
    if not relevant:
        return None
    latest_round = relevant[-1][1]
    completed = tuple(
        phase for phase, round_n in relevant if round_n == latest_round
    )
    if completed == phases:
        return None
    cursor = _cursor_from_boundary(
        loop,
        round_n=latest_round,
        completed=completed,
        source="legacy_checkpoint_migration",
    )
    migrated = tuple(
        LoopCursorRecord(
            loop_key=cursor.loop_key,
            loop_phases=cursor.loop_phases,
            round_n=cursor.round_n,
            completed_phase=phase,
            next_phase=(
                cursor.loop_phases[index + 1]
                if index + 1 < len(cursor.loop_phases)
                else None
            ),
        )
        for index, phase in enumerate(cursor.completed_phases)
    )
    return cursor, migrated


def resolve_loop_resume(
    profile: Profile,
    *,
    completed_phases: frozenset[str],
    phase_records: tuple[PhaseCheckpointRecord, ...],
    cursor_records: tuple[LoopCursorRecord, ...],
) -> LoopResumeResolution:
    """Resolve every partial loop checkpoint or raise a typed refusal.

    Explicit cursor rows take precedence. Runs created before cursor support
    may migrate only from an unambiguous ordered phase prefix carrying an
    attempt/round identity. Event logs and presentation state are never read.
    """
    cursors: list[LoopResumeCursor] = []
    migrated: list[LoopCursorRecord] = []
    for loop in _loop_steps(profile):
        phases = frozenset(_phase_names(loop))
        if not (phases & completed_phases):
            continue
        explicit = _from_explicit(loop, cursor_records)
        if explicit is not None:
            cursors.append(explicit)
            continue
        legacy = _from_legacy(loop, phase_records)
        if legacy is not None:
            cursor, records = legacy
            cursors.append(cursor)
            migrated.extend(records)
            continue
        if phases <= completed_phases:
            # Historical handoff resumes may mark a whole loop complete without
            # per-member attempt rows. Preserve the established skip contract.
            continue
        raise LoopResumeBlockedError(
            "Checkpoint contains completed members of loop "
            f"{loop.round_extras_key!r}, but no durable round boundary can "
            "identify the next phase."
        )
    return LoopResumeResolution(
        cursors=tuple(cursors),
        migrated=tuple(migrated),
    )


def resolve_loop_resume_from_store(
    profile: Profile,
    *,
    store: CheckpointStore,
    run_id: str,
    run_dir: Path | None = None,
    completed_phases: frozenset[str] | None = None,
) -> LoopResumeResolution:
    """Resolve a store-backed capability shared by preflight and dispatch."""
    checkpoint = store.load(run_id)
    effective_completed = (
        frozenset(checkpoint.completed)
        if completed_phases is None
        else completed_phases
    )
    resolution = resolve_loop_resume(
        profile,
        completed_phases=effective_completed,
        phase_records=store.get_phase_records(run_id),
        cursor_records=store.get_loop_cursors(run_id),
    )
    if (
        run_dir is not None
        and "plan" in effective_completed
        and any("plan" in cursor.completed_phases for cursor in resolution.cursors)
    ):
        try:
            from pipeline.plan_artifacts import load_parsed_plan_artifact

            load_parsed_plan_artifact(run_dir)
        except Exception as exc:  # noqa: BLE001 — translated to typed preflight
            raise LoopResumeBlockedError(
                "The completed plan requires a valid parsed_plan.json, but "
                "the durable artifact is missing or corrupt."
            ) from exc
    return resolution


def inspect_checkpoint_resume(
    profile: Profile,
    *,
    run_dir: Path,
    run_id: str,
) -> LoopResumeResolution:
    """Read-only-style CLI preflight using the same resolver as dispatch."""
    db_path = run_dir / "checkpoints.db"
    if not db_path.is_file():
        raise LoopResumeBlockedError(
            f"Checkpoint database is missing for run {run_id!r}."
        )
    with CheckpointStore(db_path, run_id=run_id) as store:
        return resolve_loop_resume_from_store(
            profile,
            store=store,
            run_id=run_id,
            run_dir=run_dir,
        )


__all__ = [
    "LoopResumeResolution",
    "inspect_checkpoint_resume",
    "resolve_loop_resume",
    "resolve_loop_resume_from_store",
]
