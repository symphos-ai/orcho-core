"""pipeline/control/resume_context.py — resolve resume-time run context
from persisted ``meta.json``.

On ``--resume RUN_ID`` the orchestrator's required fresh-run inputs
(task, project / projects) become optional: explicit CLI args win,
else fall back to whatever the run wrote into ``meta.json`` when it
first started. This module owns that resolution + hydration so both
the single-project and cross orchestrators consume the same logic.

It also owns the *resume intent* model — the pure (no-I/O) view of
"what should ``--resume`` do this invocation" — so every frontend
(CLI wrapper, direct entrypoints, MCP, future TUI) reaches the same
classification, latest-run detection, and terminal-status helpers.
Text prompting is a separate adapter in ``resume_prompt``.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pipeline.run_state.status_vocab import TERMINAL_SUCCESS_STATUSES


class ResumeContextError(ValueError):
    """Raised when a resume run_dir cannot be hydrated (missing file /
    unreadable / malformed)."""


class ResumeMode(StrEnum):
    """How ``--resume`` should be interpreted this invocation.

    * ``FRESH`` — no ``--resume`` on the command line; start a brand new run.
    * ``CHECKPOINT`` — continue an incomplete parent run in its existing
      run directory; the engine skips phases that already have a checkpoint.
    * ``FOLLOWUP`` — start a new run using the parent run as context.
      A new run directory is created; the parent's task and metadata are
      preserved as ``base_task`` / ``parent_run_id`` / ``parent_run_dir``.
    """

    FRESH = "fresh"
    CHECKPOINT = "checkpoint"
    FOLLOWUP = "followup"


@dataclass(frozen=True)
class ResumedMeta:
    """Snapshot of a paused run's ``meta.json`` plus the path on disk.

    ``meta`` is the raw session dict (the same shape that
    ``save_session`` writes). Callers should treat it as authoritative
    state for resume: phases that already completed live in
    ``meta["phases"]``; the runner must not rerun them.
    """
    path: Path
    meta: dict[str, Any]


@dataclass(frozen=True)
class ResumeIntentOptions:
    """What the user can choose when they hit ``--resume`` with no task.

    Built from the parent's ``meta.json`` (or absence thereof). Pure data:
    every frontend (CLI prompt, TUI, MCP) consumes this dataclass.
    """

    can_checkpoint: bool
    can_followup: bool
    default_mode: ResumeMode | None
    parent_status: str | None
    reason: str
    # True when the parent dead-ended on a correction-required terminal
    # (``commit_decision_fix`` / ``final_acceptance_rejected`` /
    # ``final_acceptance_no_diff``): a bare task-less resume is meaningless and
    # the actionable next step is a from_run_plan follow-up that carries the held
    # diff. The frontend must require a follow-up task; ``can_checkpoint`` is
    # always False in this state. Default False keeps every other intent shape
    # (success / halt / incomplete / awaiting) byte-identical.
    requires_followup_task: bool = False


@dataclass(frozen=True)
class ResolvedResumeContext:
    """Final resume decision after classification + interactive prompt."""

    mode: ResumeMode
    run_id: str | None
    run_dir: Path | None
    resumed: ResumedMeta | None
    parent_status: str | None = None


@dataclass(frozen=True)
class FollowupResumeFields:
    """The five parent-context slots a FOLLOWUP run carries forward.

    All ``None`` for FRESH / CHECKPOINT or when there is no parent meta;
    populated only when the parent meta is available for a FOLLOWUP run.
    """

    parent_run_id: str | None
    parent_run_dir: str | None
    parent_status: str | None
    base_task: str | None
    session_seeds: dict[str, str] | None


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _last_mapping(value: Any) -> Mapping[str, Any] | None:
    if not isinstance(value, list) or not value:
        return None
    item = value[-1]
    return item if isinstance(item, Mapping) else None


def _mapping_value(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _meta_session_id(value: Any) -> str | None:
    entry = _mapping_value(value)
    if entry is None:
        return None
    meta = _mapping_value(entry.get("meta"))
    if meta is not None:
        sid = _string_value(meta.get("session_id"))
        if sid:
            return sid
    return _string_value(entry.get("session_id"))


def extract_followup_session_seeds(parent_meta: Mapping[str, Any]) -> dict[str, str]:
    """Build a phase-role → parent session_id map for follow-up runs.

    Reads the persisted Step-0 session shape:
      * ``plan`` / ``validate_plan``: last list entry top-level ``session_id``.
      * ``implement`` / ``final_acceptance``: ``meta.session_id`` preferred,
        top-level ``session_id`` accepted for tolerance.
      * ``review_changes`` / ``repair_changes``: last round's split
        ``review_session_id`` / ``repair_session_id``.

    Pre-split round entries used one ``session_id`` that represented the
    repair side, so the compatibility fallback seeds ``repair_changes`` only.
    Missing, malformed, or non-string values are skipped silently.
    """
    phases = _mapping_value(parent_meta.get("phases"))
    if phases is None:
        return {}

    seeds: dict[str, str] = {}

    plan_entry = _last_mapping(phases.get("plan"))
    if plan_entry is not None:
        sid = _string_value(plan_entry.get("session_id"))
        if sid:
            seeds["plan"] = sid

    validate_entry = _last_mapping(phases.get("validate_plan"))
    if validate_entry is not None:
        sid = _string_value(validate_entry.get("session_id"))
        if sid:
            seeds["validate_plan"] = sid

    implement_sid = _meta_session_id(phases.get("implement"))
    if implement_sid:
        seeds["implement"] = implement_sid

    final_sid = _meta_session_id(phases.get("final_acceptance"))
    if final_sid:
        seeds["final_acceptance"] = final_sid

    round_entry = _last_mapping(phases.get("rounds"))
    if round_entry is not None:
        review_sid = _string_value(round_entry.get("review_session_id"))
        if review_sid:
            seeds["review_changes"] = review_sid
        repair_sid = _string_value(round_entry.get("repair_session_id"))
        if not repair_sid:
            repair_sid = _string_value(round_entry.get("session_id"))
        if repair_sid:
            seeds["repair_changes"] = repair_sid

    return seeds


def build_followup_resume_fields(
    *,
    resume_mode: ResumeMode,
    resume_run_id: str | None,
    resumed: ResumedMeta | None,
) -> FollowupResumeFields:
    """Resolve the parent-context slots a FOLLOWUP run carries forward.

    Pure (no I/O): mirrors the historical inline CLI block. Only a
    FOLLOWUP mode with parent meta populates the slots; every other
    combination yields all-``None``. ``parent_status`` / ``base_task``
    come from ``resumed.meta`` only when they are strings.
    """
    if resume_mode != ResumeMode.FOLLOWUP or resumed is None:
        return FollowupResumeFields(
            parent_run_id=None,
            parent_run_dir=None,
            parent_status=None,
            base_task=None,
            session_seeds=None,
        )
    parent_status_val = resumed.meta.get("status")
    parent_task_val = resumed.meta.get("task")
    return FollowupResumeFields(
        parent_run_id=resume_run_id,
        parent_run_dir=str(resumed.path.parent.resolve()),
        parent_status=(
            parent_status_val if isinstance(parent_status_val, str) else None
        ),
        base_task=(
            parent_task_val if isinstance(parent_task_val, str) else None
        ),
        session_seeds=extract_followup_session_seeds(resumed.meta),
    )


@dataclass(frozen=True)
class CheckpointFollowupLineage:
    """Lineage of a follow-up child being resumed from its own checkpoint.

    Distinct from :class:`FollowupResumeFields` (which carries parent
    context FORWARD into a brand-new FOLLOWUP run): this is read FROM an
    existing follow-up child's own ``meta.json`` so a CHECKPOINT resume of
    that child can render the parent/child lineage header. Display only —
    it never seeds provider sessions (the child resumes its own).
    """

    parent_run_id: str
    parent_run_dir: str | None
    parent_status: str | None
    base_task: str | None
    child_status: str | None
    active_handoff_id: str | None


def build_checkpoint_followup_lineage(
    resumed: ResumedMeta | None,
) -> CheckpointFollowupLineage | None:
    """Extract follow-up lineage from a checkpoint-resumed child's meta.

    Returns ``None`` unless the resumed run is itself a follow-up child
    (``meta.resume_mode == "followup"`` with a non-empty
    ``parent_run_id``). On a hit, surfaces parent id / dir / status, the
    base task, the child's own status, and the active handoff id (if any)
    so the run header makes the parent ↔ child relationship explicit even
    on a plain ``--resume <child>``.
    """
    if resumed is None:
        return None
    meta = resumed.meta
    if not isinstance(meta, Mapping):
        return None
    if meta.get("resume_mode") != ResumeMode.FOLLOWUP.value:
        return None
    parent_run_id = meta.get("parent_run_id")
    if not isinstance(parent_run_id, str) or not parent_run_id:
        return None
    active = meta.get("phase_handoff")
    handoff_id = active.get("id") if isinstance(active, dict) else None
    return CheckpointFollowupLineage(
        parent_run_id=parent_run_id,
        parent_run_dir=_string_value(meta.get("parent_run_dir")),
        parent_status=_string_value(meta.get("parent_status")),
        base_task=_string_value(meta.get("base_task")),
        child_status=_string_value(meta.get("status")),
        active_handoff_id=_string_value(handoff_id),
    )


def extract_cross_followup_session_seeds(
    parent_cross_run_dir: Path,
    aliases: Iterable[str],
) -> dict[str, dict[str, str]]:
    """Build a per-alias ``{alias → {phase_role → parent_session_id}}`` map
    for cross-project follow-up runs.

    Each child sub-pipeline writes its own ``meta.json`` under
    ``<cross_run_dir>/<alias>/meta.json`` (see
    ``pipeline/cross_project/orchestrator.py``). For a cross follow-up,
    we want each alias's child agents to resume from THEIR alias's
    parent session ids — not a flattened cross-level map.

    Implementation: load each alias's child ``meta.json`` and run the
    existing single-project :func:`extract_followup_session_seeds`
    against it. Reuses the same Step-0 schema reads, so cross is
    fully symmetric with single-project per alias.

    Missing alias directory, missing/malformed ``meta.json``, or empty
    seed map (parent had no resumable phases for that alias) is skipped
    silently. The caller passes ``aliases`` (the active follow-up's
    project aliases) so we only attempt aliases the follow-up actually
    cares about — aliases present in the parent but absent in the
    follow-up are quietly ignored.

    Returns an empty dict when nothing seedable was found (caller treats
    that as a no-op fall-through to fresh agent contexts).
    """
    if not parent_cross_run_dir.is_dir():
        return {}

    seeds: dict[str, dict[str, str]] = {}
    for alias in aliases:
        if not isinstance(alias, str) or not alias.strip():
            continue
        alias_dir = parent_cross_run_dir / alias
        if not alias_dir.is_dir():
            continue
        try:
            resumed = load_resume_meta(alias_dir)
        except ResumeContextError:
            # One bad alias should not poison the whole map.
            continue
        if resumed is None:
            continue
        per_alias = extract_followup_session_seeds(resumed.meta)
        if per_alias:
            seeds[alias] = per_alias
    return seeds


# ── meta.json I/O ──────────────────────────────────────────────────────────

def load_resume_meta(run_dir: Path) -> ResumedMeta | None:
    """Read ``meta.json`` from a resume run_dir.

    Returns a :class:`ResumedMeta` when ``meta.json`` exists and parses,
    or ``None`` when the file is absent (a run that paused before it
    wrote any meta — older runs, mid-Phase-0 crashes). Raises
    :class:`ResumeContextError` only when the file exists but is
    unreadable / malformed; that's a real corruption signal worth
    surfacing.
    """
    meta_file = run_dir / "meta.json"
    if not meta_file.is_file():
        return None
    try:
        raw = meta_file.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        raise ResumeContextError(
            f"--resume: cannot read meta.json in {run_dir}: {e}"
        ) from e
    if not isinstance(data, dict):
        raise ResumeContextError(
            f"--resume: meta.json in {run_dir} is not a JSON object"
        )
    return ResumedMeta(path=meta_file, meta=data)


# ── Task / project / projects resolution ───────────────────────────────────

_TASK_FILES_DIR = Path(".orcho") / ".task-files"


def _task_file_lookup_dirs(
    *, explicit_project: str | None = None,
) -> tuple[Path, ...]:
    dirs: list[Path] = []
    if explicit_project:
        dirs.append(Path(explicit_project).expanduser() / _TASK_FILES_DIR)
    dirs.extend(
        ancestor / _TASK_FILES_DIR for ancestor in [Path.cwd(), *Path.cwd().parents]
    )
    return tuple(dict.fromkeys(dirs))


def _resolve_task_file_path(
    explicit_task_file: str, *, explicit_project: str | None = None,
) -> Path:
    path = Path(explicit_task_file).expanduser()
    if path.is_absolute() or path.parent != Path(".") or path.suffix.lower() != ".md":
        return path
    for task_dir in _task_file_lookup_dirs(explicit_project=explicit_project):
        candidate = task_dir / path.name
        if candidate.is_file():
            return candidate
    return path


def _task_file_missing_message(
    explicit_task_file: str,
    task_file_path: Path,
    *,
    explicit_project: str | None = None,
) -> str:
    path = Path(explicit_task_file).expanduser()
    if path.is_absolute() or path.parent != Path(".") or path.suffix.lower() != ".md":
        return (
            f"--task-file not found: {task_file_path}. "
            f"Pass an existing file path, or use a short .md name stored under "
            f"{_TASK_FILES_DIR}."
        )
    search_dirs = "\n".join(
        f"  - {task_dir}" for task_dir in _task_file_lookup_dirs(
            explicit_project=explicit_project,
        )
    )
    return (
        f"--task-file short name not found: {explicit_task_file}\n"
        f"Orcho treated {path.name!r} as a short task-file name (a bare *.md "
        f"name with no path), so it looked for it only in the reserved "
        f"{_TASK_FILES_DIR} directories:\n{search_dirs}\n"
        f"To fix this, either drop {path.name!r} into one of those "
        f"{_TASK_FILES_DIR} directories, or pass a direct relative/absolute "
        f"path to the task file, for example: --task-file ./{path.name}"
    )


def resolve_task(
    *,
    explicit_task: str | None,
    explicit_task_file: str | None,
    explicit_project: str | None = None,
    resumed: ResumedMeta | None,
) -> str:
    """Resolve the effective task string.

    Order: ``--task-file`` → ``--task`` → ``meta["task"]`` → error.
    """
    if explicit_task_file:
        task_file_path = _resolve_task_file_path(
            explicit_task_file, explicit_project=explicit_project,
        )
        if not task_file_path.is_file():
            raise ResumeContextError(
                _task_file_missing_message(
                    explicit_task_file,
                    task_file_path,
                    explicit_project=explicit_project,
                )
            )
        return task_file_path.read_text(encoding="utf-8").strip()
    if explicit_task:
        return explicit_task
    if resumed is not None:
        meta_task = resumed.meta.get("task")
        if isinstance(meta_task, str) and meta_task.strip():
            return meta_task
    raise ResumeContextError(
        "task: provide --task or --task-file "
        "(no persisted task in meta.json)"
    )


def resolve_project(
    *,
    explicit_project: str | None,
    resumed: ResumedMeta | None,
) -> str:
    """Resolve the effective project path for ``orcho run``.

    Order: ``--project`` → ``meta["project"]`` → error.
    """
    if explicit_project:
        return explicit_project
    if resumed is not None:
        meta_project = resumed.meta.get("project")
        if isinstance(meta_project, str) and meta_project.strip():
            return meta_project
    raise ResumeContextError(
        "project: provide --project (no persisted project in meta.json)"
    )


def resolve_resume_profile(
    *,
    explicit_profile: str | None,
    resumed: ResumedMeta | None,
    fresh_default: str | None = None,
) -> str:
    """Resolve the effective pipeline profile for a fresh run or resume.

    Order: ``--profile`` (explicit) → ``meta["profile"]`` (inherit) →
    ``fresh_default`` (legacy fallback).

    ``fresh_default`` defaults to the canonical
    :data:`pipeline.project.constants.DEFAULT_PROFILE_NAME` (single
    source of truth — no duplicated string literal here). The cross
    orchestrator passes ``CROSS_DEFAULT_PROFILE`` explicitly so the
    cross fresh-run fallback stays under its own constant. The import is
    lazy so this control-layer module keeps the project ``constants``
    module's stdlib-only / no-cycle property.

    Why the project default (``feature``) for the fresh / fallback case
    and not ``task``: ``feature`` is a strict superset of ``task`` for
    the resume case — the engine skips already-checkpointed phases
    regardless of profile, so the only difference is which prompt-
    envelope parts (most importantly the ``artifact:validate_plan``
    part) are loaded into review_changes / final_acceptance. Over-
    including stable context is cheap with cache hits; under-including
    silently drops audit quality. Legacy runs whose ``meta.json``
    predates ``profile`` capture (very old, partial corruption) thus
    inherit ``feature``, not ``task``.
    """
    if explicit_profile and explicit_profile.strip():
        return explicit_profile
    if resumed is not None:
        meta_profile = resumed.meta.get("profile")
        if isinstance(meta_profile, str) and meta_profile.strip():
            return meta_profile
    if fresh_default is None:
        # Lazy import: keep the canonical default in one place without
        # eagerly pulling the project package (and its agent/runtime
        # imports) into this control-layer module at import time.
        from pipeline.project.constants import DEFAULT_PROFILE_NAME

        return DEFAULT_PROFILE_NAME
    return fresh_default


def resolve_projects_argv(
    *,
    explicit_projects: list[str] | None,
    resumed: ResumedMeta | None,
) -> list[str]:
    """Resolve the effective ``--projects`` argv for ``orcho cross``.

    The cross CLI accepts ``--projects alias:/path ...`` items. On
    resume, ``meta["projects"]`` is the persisted alias→path map; we
    reconstruct the argv form so the existing parser (``parse_projects``)
    continues to drive validation.
    """
    if explicit_projects:
        return list(explicit_projects)
    if resumed is not None:
        meta_projects = resumed.meta.get("projects")
        if isinstance(meta_projects, dict) and meta_projects:
            recovered: list[str] = []
            for alias, path in meta_projects.items():
                if not isinstance(alias, str) or not isinstance(path, str):
                    raise ResumeContextError(
                        f"--resume: meta.json has malformed projects "
                        f"entry: {alias!r}={path!r}"
                    )
                recovered.append(f"{alias}:{path}")
            return recovered
    raise ResumeContextError(
        "projects: provide --projects alias:/path ... "
        "(no persisted projects in meta.json)"
    )


# ── Resume mode classification ─────────────────────────────────────────────

_PHASE_HANDOFF_HALT_REASON = "phase_handoff_halt"
_COMMIT_DECISION_HALT_REASON = "commit_decision_halt"
_COMMIT_DECISION_FIX_REASON = "commit_decision_fix"
_COMMIT_DELIVERY_PENDING_REASON = "commit_delivery_pending"
# Stage C delivery-scope enforcement (T4): a strict-mono sibling-repo violation
# parks a reversible, decidable delivery gate (commit_delivery.status=pending,
# scope_blocker set). Like the ADR 0100 defer park, it awaits an out-of-band
# ``decide_delivery`` call, not a checkpoint continuation.
_COMMIT_DELIVERY_SCOPE_BLOCKED_REASON = "commit_delivery_scope_blocked"
_AWAITING_COMMIT_DECISION = "awaiting_commit_decision"
# Final-acceptance rejected dead-ends: the release verdict was ``reject`` and
# nothing was applied — no delivery committed and no correction gate parked.
# ``final_acceptance_rejected`` is the rejected-override terminal; the
# ``final_acceptance_no_diff`` variant fires when a rejecting run produced no
# applied diff at all. Both are written by ``pipeline/project/finalization.py``
# and represent a pure terminal dead-end with no operator decision pending: the
# only forward motion is a fresh follow-up, never a checkpoint continuation.
# They are intentionally NOT decision surfaces (unlike the ``commit_delivery_*``
# / ``commit_decision_*`` reasons), so checkpoint-resume must not select them.
_FINAL_ACCEPTANCE_REJECTED_REASON = "final_acceptance_rejected"
_FINAL_ACCEPTANCE_NO_DIFF_REASON = "final_acceptance_no_diff"


def is_terminal_success(meta: Mapping[str, Any]) -> bool:
    """True when the parent run finished cleanly.

    Statuses NOT counted as terminal-success and therefore considered
    resumable: ``running``, ``failed``, ``halted``, ``interrupted``,
    ``awaiting_human_review``, ``awaiting_phase_handoff``,
    ``awaiting_commit_decision``, ``awaiting_gate_decision``.
    """
    return meta.get("status") in TERMINAL_SUCCESS_STATUSES


def is_awaiting_commit_decision(meta: Mapping[str, Any]) -> bool:
    """True when the parent is paused on the post-release commit-decision gate."""
    return meta.get("status") == _AWAITING_COMMIT_DECISION


def is_terminal_phase_handoff_halt(meta: Mapping[str, Any]) -> bool:
    """True when the parent was terminated by a phase-handoff halt."""
    return (
        meta.get("status") == "halted"
        and meta.get("halt_reason") == _PHASE_HANDOFF_HALT_REASON
    )


def is_terminal_commit_decision_halt(meta: Mapping[str, Any]) -> bool:
    """True when the parent was terminated by a commit-decision halt."""
    return (
        meta.get("status") == "halted"
        and meta.get("halt_reason") == _COMMIT_DECISION_HALT_REASON
    )


def is_terminal_commit_decision_fix(meta: Mapping[str, Any]) -> bool:
    """True when the parent stopped so a correction follow-up can run."""
    return (
        meta.get("status") == "halted"
        and meta.get("halt_reason") == _COMMIT_DECISION_FIX_REASON
    )


def is_terminal_commit_delivery_pending(meta: Mapping[str, Any]) -> bool:
    """True when the parent parked its delivery decision (ADR 0100 defer mode).

    A parked run awaits an out-of-band ``decide_delivery`` call, not a
    checkpoint continuation — so checkpoint-resume must not auto-select it,
    exactly like the other commit-decision terminals.
    """
    return (
        meta.get("status") == "halted"
        and meta.get("halt_reason") == _COMMIT_DELIVERY_PENDING_REASON
    )


def is_terminal_commit_delivery_scope_blocked(meta: Mapping[str, Any]) -> bool:
    """True when the parent parked a strict-mono delivery-scope gate (T4).

    Like :func:`is_terminal_commit_delivery_pending`, this run awaits an
    out-of-band ``decide_delivery`` call (skip / halt / expanded re-run), not a
    checkpoint continuation — so checkpoint-resume must not auto-select it.
    """
    return (
        meta.get("status") == "halted"
        and meta.get("halt_reason") == _COMMIT_DELIVERY_SCOPE_BLOCKED_REASON
    )


def is_terminal_final_acceptance_rejected(meta: Mapping[str, Any]) -> bool:
    """True when the parent dead-ended on a rejected final acceptance.

    Covers both rejected-release terminals written by
    ``pipeline/project/finalization.py``: ``final_acceptance_rejected`` (the
    rejected-override terminal) and ``final_acceptance_no_diff`` (a rejecting
    run that applied no diff). Neither parks an operator decision — there is no
    delivery or correction gate to resolve — so a checkpoint-resume cannot
    advance the run; the only forward motion is a fresh follow-up.
    """
    return meta.get("status") == "halted" and meta.get("halt_reason") in (
        _FINAL_ACCEPTANCE_REJECTED_REASON,
        _FINAL_ACCEPTANCE_NO_DIFF_REASON,
    )


def is_terminal_resume_parent(meta: Mapping[str, Any]) -> bool:
    """True when checkpoint-resume should not select this run by default."""
    return (
        is_terminal_success(meta)
        or is_terminal_phase_handoff_halt(meta)
        or is_terminal_commit_decision_halt(meta)
        or is_terminal_commit_decision_fix(meta)
        or is_terminal_commit_delivery_pending(meta)
        or is_terminal_commit_delivery_scope_blocked(meta)
        or is_terminal_final_acceptance_rejected(meta)
    )


def classify_resume_mode(
    *,
    resume: str | None,
    explicit_task: str | None,
    explicit_task_file: str | None,
) -> ResumeMode:
    """Classify the requested resume mode from CLI inputs alone.

    Pure: depends only on the CLI surface, not on parent meta or
    interactive prompting. The orchestrator may upgrade
    ``CHECKPOINT`` → ``FOLLOWUP`` later when the user supplies a
    follow-up task via the prompt adapter.
    """
    if resume is None:
        return ResumeMode.FRESH
    if explicit_task or explicit_task_file:
        return ResumeMode.FOLLOWUP
    return ResumeMode.CHECKPOINT


def get_resume_intent_options(
    *,
    parent_meta: Mapping[str, Any] | None,
    has_new_task: bool,
) -> ResumeIntentOptions:
    """Compute the menu a frontend should offer for a bare ``--resume``.

    * Missing parent meta → both modes blocked; the orchestrator should
      surface its standard "cannot resume" error.
    * Already supplied a new task → caller should classify as FOLLOWUP
      directly; intent options would mislead.
    * Terminal-success parent → only FOLLOWUP is meaningful;
      ``default_mode`` is FOLLOWUP.
    * Otherwise → CHECKPOINT is the default, FOLLOWUP is available.
    """
    parent_status = (
        parent_meta.get("status") if isinstance(parent_meta, Mapping) else None
    )
    if parent_meta is None:
        return ResumeIntentOptions(
            can_checkpoint=False,
            can_followup=False,
            default_mode=None,
            parent_status=None,
            reason="missing-parent-meta",
        )
    if has_new_task:
        return ResumeIntentOptions(
            can_checkpoint=False,
            can_followup=True,
            default_mode=ResumeMode.FOLLOWUP,
            parent_status=parent_status,
            reason="explicit-task",
        )
    if is_terminal_phase_handoff_halt(parent_meta):
        return ResumeIntentOptions(
            can_checkpoint=False,
            can_followup=True,
            default_mode=ResumeMode.FOLLOWUP,
            parent_status=parent_status,
            reason="terminal-halt",
        )
    if is_terminal_commit_decision_halt(parent_meta):
        # ``halt`` = operator gave up. A follow-up (with a new task) is still
        # meaningful, but this is NOT a correction-required dead-end, so a bare
        # resume is merely not-checkpointable, not follow-up-mandated.
        return ResumeIntentOptions(
            can_checkpoint=False,
            can_followup=True,
            default_mode=ResumeMode.FOLLOWUP,
            parent_status=parent_status,
            reason="terminal-halt",
        )
    if (
        is_terminal_commit_decision_fix(parent_meta)
        or is_terminal_final_acceptance_rejected(parent_meta)
    ):
        # Correction requested (``commit_decision_fix``) or rejected dead-end
        # (``final_acceptance_rejected`` / ``final_acceptance_no_diff``): a bare
        # task-less resume is meaningless — neither a checkpoint continuation nor
        # an empty follow-up advances the run. The actionable next step is a
        # from_run_plan follow-up carrying the held diff, so signal that a
        # follow-up task is required.
        return ResumeIntentOptions(
            can_checkpoint=False,
            can_followup=True,
            default_mode=ResumeMode.FOLLOWUP,
            parent_status=parent_status,
            reason="correction-followup-required",
            requires_followup_task=True,
        )
    if is_awaiting_commit_decision(parent_meta):
        return ResumeIntentOptions(
            can_checkpoint=True,
            can_followup=True,
            default_mode=ResumeMode.CHECKPOINT,
            parent_status=parent_status,
            reason="awaiting-commit-decision",
        )
    if is_terminal_success(parent_meta):
        return ResumeIntentOptions(
            can_checkpoint=False,
            can_followup=True,
            default_mode=ResumeMode.FOLLOWUP,
            parent_status=parent_status,
            reason="terminal-success",
        )
    return ResumeIntentOptions(
        can_checkpoint=True,
        can_followup=True,
        default_mode=ResumeMode.CHECKPOINT,
        parent_status=parent_status,
        reason="incomplete-parent",
    )


# ── Latest-run detection ───────────────────────────────────────────────────

class LatestRunNotFound(ResumeContextError):
    """No run directory matches the requested ``kind`` (or status filter)."""


def _meta_run_kind(meta: Mapping[str, Any]) -> Literal["run", "cross"] | None:
    """Classify a meta.json by run kind.

    A run is "cross" when ``meta["projects"]`` is a non-empty dict.
    It is "run" (single-project) when ``meta["project"]`` is a non-empty
    string and there is no ``meta["projects"]`` dict. Anything else
    (legacy / malformed / mid-write meta) returns ``None`` and is skipped.
    """
    projects = meta.get("projects")
    if isinstance(projects, dict) and projects:
        return "cross"
    project = meta.get("project")
    if isinstance(project, str) and project.strip() and not projects:
        return "run"
    return None


def meta_run_kind(meta: Mapping[str, Any]) -> Literal["run", "cross"] | None:
    """Public classifier for a meta.json by run kind.

    A run is ``"cross"`` when ``meta["projects"]`` is a non-empty dict,
    ``"run"`` when ``meta["project"]`` is a non-empty string and there is
    no ``meta["projects"]`` dict, else ``None`` (legacy / malformed).
    Thin wrapper over :func:`_meta_run_kind` so frontends classify a
    resumed run before resolving project/task.
    """
    return _meta_run_kind(meta)


def resolve_latest_run(
    *,
    runs_dir: Path,
    kind: Literal["run", "cross"],
    prefer_incomplete: bool = False,
    include_terminal_success: bool = True,
    require_existing_project: bool = False,
) -> str:
    """Pick the newest run_id whose meta matches ``kind``.

    Sorts run directory names descending — run ids are timestamp-prefixed
    so lexicographic descending matches "newest first" without trusting
    filesystem mtime.

    * ``prefer_incomplete`` — when True, pick the newest run that can
      still be checkpoint-resumed if one exists; fall back to a terminal
      run otherwise (still subject to ``include_terminal_success``).
    * ``include_terminal_success`` — when False, terminal runs are not
      eligible; raises :class:`LatestRunNotFound` if no incomplete
      candidate exists.
    * ``require_existing_project`` — when True, skip stale run metadata whose
      project path (or cross-project paths) no longer exists. This keeps bare
      ``--resume latest`` from auto-selecting pytest/tmp runs or old retained
      runs whose source checkout has been deleted. Explicit ``--resume RUN_ID``
      remains allowed to surface the normal project-not-found diagnostic.

    Raises :class:`LatestRunNotFound` when no candidate matches.
    """
    if not runs_dir.is_dir():
        raise LatestRunNotFound(
            f"--resume latest: runs dir does not exist: {runs_dir}"
        )

    incomplete: list[str] = []
    terminal: list[str] = []
    candidates = sorted(
        (d for d in runs_dir.iterdir() if d.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    for d in candidates:
        try:
            resumed = load_resume_meta(d)
        except ResumeContextError:
            continue
        if resumed is None:
            continue
        if _meta_run_kind(resumed.meta) != kind:
            continue
        if require_existing_project and not _meta_projects_exist(
            resumed.meta, kind,
        ):
            continue
        if is_terminal_resume_parent(resumed.meta):
            terminal.append(d.name)
        else:
            incomplete.append(d.name)

    if prefer_incomplete and incomplete:
        return incomplete[0]
    if include_terminal_success and (incomplete or terminal):
        ordered = sorted(incomplete + terminal, reverse=True)
        return ordered[0]
    if incomplete:
        return incomplete[0]

    raise LatestRunNotFound(
        f"--resume latest: no {kind!r} runs found in {runs_dir}"
        + ("" if include_terminal_success else " (excluding terminal-success)")
    )


def _meta_projects_exist(
    meta: Mapping[str, Any],
    kind: Literal["run", "cross"],
) -> bool:
    """Return whether a run meta points at existing project checkout(s)."""
    if kind == "run":
        project = meta.get("project")
        if not isinstance(project, str) or not project.strip():
            return False
        return Path(project).expanduser().exists()

    projects = meta.get("projects")
    if not isinstance(projects, Mapping) or not projects:
        return False
    for path in projects.values():
        if not isinstance(path, str) or not path.strip():
            return False
        if not Path(path).expanduser().exists():
            return False
    return True


@dataclass(frozen=True)
class ActiveFollowupChild:
    """A newer, still-unfinished follow-up run spawned from a parent.

    Detected by scanning the runs dir for a run whose ``meta.parent_run_id``
    points at the parent and whose status is not terminal. Used to *offer*
    (never silently switch to) resuming the in-progress follow-up.
    """

    child_run_id: str
    child_status: str
    parent_run_id: str
    active_handoff_id: str | None


def detect_active_followup_child(
    *,
    parent_run_id: str | None,
    runs_dir: Path | None,
) -> ActiveFollowupChild | None:
    """Find the newest unfinished follow-up child of ``parent_run_id``.

    Pure detection (filesystem read only, no mutation). A child qualifies
    when its ``meta.json`` records ``resume_mode == "followup"`` and
    ``parent_run_id`` equal to ``parent_run_id``, it is not a cross
    sub-pipeline (no ``project_alias``), and it is not terminal
    (:func:`is_terminal_resume_parent`). Returns the newest match by run
    id (timestamp-sortable), or ``None`` when there is none.
    """
    if not parent_run_id or runs_dir is None or not runs_dir.is_dir():
        return None

    best: ActiveFollowupChild | None = None
    for entry in runs_dir.iterdir():
        if not entry.is_dir() or entry.name == parent_run_id:
            continue
        try:
            resumed = load_resume_meta(entry)
        except ResumeContextError:
            continue
        if resumed is None:
            continue
        meta = resumed.meta
        if meta.get("resume_mode") != "followup":
            continue
        if meta.get("parent_run_id") != parent_run_id:
            continue
        if meta.get("project_alias"):
            continue
        if is_terminal_resume_parent(meta):
            continue
        status = meta.get("status")
        active = meta.get("phase_handoff")
        handoff_id = active.get("id") if isinstance(active, dict) else None
        candidate = ActiveFollowupChild(
            child_run_id=entry.name,
            child_status=status if isinstance(status, str) else "unknown",
            parent_run_id=parent_run_id,
            active_handoff_id=handoff_id if isinstance(handoff_id, str) else None,
        )
        if best is None or candidate.child_run_id > best.child_run_id:
            best = candidate
    return best


__all__ = [
    "ActiveFollowupChild",
    "CheckpointFollowupLineage",
    "FollowupResumeFields",
    "LatestRunNotFound",
    "ResolvedResumeContext",
    "ResumeContextError",
    "ResumeIntentOptions",
    "ResumeMode",
    "ResumedMeta",
    "build_checkpoint_followup_lineage",
    "build_followup_resume_fields",
    "classify_resume_mode",
    "detect_active_followup_child",
    "get_resume_intent_options",
    "is_awaiting_commit_decision",
    "is_terminal_commit_decision_halt",
    "is_terminal_commit_decision_fix",
    "is_terminal_commit_delivery_pending",
    "is_terminal_commit_delivery_scope_blocked",
    "is_terminal_final_acceptance_rejected",
    "is_terminal_phase_handoff_halt",
    "is_terminal_resume_parent",
    "is_terminal_success",
    "load_resume_meta",
    "meta_run_kind",
    "resolve_latest_run",
    "resolve_project",
    "resolve_projects_argv",
    "resolve_resume_profile",
    "resolve_task",
]
