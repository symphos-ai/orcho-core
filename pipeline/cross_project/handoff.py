"""pipeline/cross_project/handoff.py — per-child handoff artifacts.

After cross-level planning is approved, the cross orchestrator writes
``implementation_handoff.json`` (canonical) and
``implementation_handoff.md`` (derived audit view) under each child
alias's artifact directory. The cross runner passes the **JSON** file's
path to ``run_pipeline(handoff_path=...)``; the child ``run_pipeline``
loads + validates it, then renders the prompt body *from the typed
object* before any mutating phase.

ADR 0050: the JSON is the source of truth. The markdown is rendered
*from* the typed :class:`Handoff` as a human-readable / audit view — it
is no longer the authoritative data channel the runtime reads. The
runtime consumes the render derived from structured fields, so a stray
line can no longer become a misleading instruction.

The structured fields like ``global_interface_contract``,
``execution_order``, ``dependencies``, etc. remain deliberately omitted;
the cross-plan markdown grammar does not currently surface them. Agents
read ``full_cross_plan_markdown`` + ``project_subtask`` and infer
context.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

__all__ = [
    "Handoff",
    "load_handoff",
    "render_handoff_markdown",
    "resume_project_phase_handoff",
    "validate_handoff",
    "write_handoff",
]

# Fields that must be present + non-empty for the handoff to drive a
# child implement/repair run. ``project_path`` (source checkout) is
# deliberately NOT here: it is audit-only and never rendered into the
# prompt (see ADR 0050 + the leak guard below). ``interface_contract`` /
# ``implementation_order`` are NOT required either: they are best-effort
# structured slices of the plan (ADR 0052) and are absent whenever the
# plan does not follow the canonical three-section grammar — the render
# then falls back to ``full_cross_plan_markdown``.
_REQUIRED_NONEMPTY = (
    "parent_run_id",
    "profile",
    "alias",
    "project_subtask",
    "full_cross_plan_markdown",
)

# Fields ``load_handoff`` tolerates absent from an on-disk JSON sidecar
# (they carry dataclass defaults). Everything else must be present.
_OPTIONAL_ON_LOAD = (
    "sibling_aliases",
    "interface_contract",
    "implementation_order",
)


@dataclass(frozen=True)
class Handoff:
    """v1 cross-project handoff payload for one alias."""
    parent_run_id: str
    profile: str
    alias: str
    project_path: str
    approved_cross_plan_path: str
    full_cross_plan_path: str
    full_cross_plan_markdown: str
    cross_validation_summary: str
    cross_validation_verdict: dict
    project_subtask: str
    # Control-only child write declarations. They are deliberately absent from
    # the rendered audit/runtime markdown body.
    declared_files: tuple[str, ...] = field(default_factory=tuple)
    sibling_aliases: tuple[str, ...] = field(default_factory=tuple)
    # ADR 0052 — structured slices of the approved cross plan. Surfaced
    # in the rendered body in place of the full plan dump when present;
    # both empty → render falls back to ``full_cross_plan_markdown``.
    interface_contract: str = ""
    implementation_order: str = ""


def render_handoff_markdown(h: Handoff) -> str:
    """Render the prompt/audit body from the typed handoff.

    This is the **only** view the runtime consumes (ADR 0050): the child
    sub-pipeline renders it from the JSON source of truth rather than
    reading a hand-authored markdown blob.

    ADR 0052: when the cross plan parsed into structured slices, the body
    surfaces just the shared ``## Interface contract`` and the
    cross-alias ``## Implementation order`` alongside this alias's own
    ``## Project subtask`` — dropping the full-plan dump, which duplicated
    the subtask and carried every *sibling's* subtask block as noise. When
    neither slice is present (plan did not follow the canonical grammar —
    e.g. a lite/mock projection), the body falls back to the full-plan
    dump so non-conformant plans still hand off intact.

    NOTE: the body deliberately does NOT surface ``project_path`` (the
    SOURCE checkout). The child runs implement/repair in an isolated
    worktree, and its own project-context block authoritatively states
    that worktree path with "make task changes here, not in the source
    checkout". Echoing the source path here would contradict the child's
    cwd and the context block — a misleading signal that would point the
    runtime at the pristine source. Mono carries no such source path in
    its handoff for the same reason. ``project_path`` is preserved in the
    JSON sidecar for audit only.
    """
    siblings = ", ".join(h.sibling_aliases) if h.sibling_aliases else "(none)"
    head = (
        f"# Cross handoff for [{h.alias}]\n\n"
        f"- parent_run_id: `{h.parent_run_id}`\n"
        f"- profile: `{h.profile}`\n"
        f"- approved_cross_plan: `{h.approved_cross_plan_path}`\n"
        f"- siblings: {siblings}\n\n"
        f"## Cross validation\n\n"
        f"{h.cross_validation_summary or '(no summary)'}\n\n"
    )
    subtask_block = (
        f"## Project subtask\n\n"
        f"{h.project_subtask or '(no subtask extracted)'}\n"
    )
    if h.interface_contract.strip() or h.implementation_order.strip():
        return (
            head
            + f"## Interface contract\n\n"
              f"{h.interface_contract.strip() or '(none)'}\n\n"
            + subtask_block
            + f"\n## Implementation order\n\n"
              f"{h.implementation_order.strip() or '(none)'}\n"
        )
    return (
        head
        + subtask_block
        + f"\n## Full cross plan\n\n{h.full_cross_plan_markdown}\n"
    )


def validate_handoff(h: Handoff) -> None:
    """Fail at the cross level on a missing/contradictory handoff.

    ADR 0050 scope (3): a structurally bad handoff must raise here, when
    the cross orchestrator writes it, rather than silently degrading into
    a misleading prompt inside the child run.

    Two checks:

    * required runtime-consumed fields are present + non-empty;
    * the rendered body never leaks the source ``project_path`` — the
      exact regression class from the path-leak bug (``7300768``). The
      renderer omits it today, so this only fires if a future change
      reintroduces the leak.
    """
    missing = [
        name for name in _REQUIRED_NONEMPTY
        if not str(getattr(h, name)).strip()
    ]
    if missing:
        raise ValueError(
            f"cross handoff for alias {h.alias!r} is missing required "
            f"field(s): {', '.join(missing)}"
        )
    if not isinstance(h.declared_files, tuple) or any(
        not isinstance(path, str) for path in h.declared_files
    ):
        raise ValueError(
            f"cross handoff for alias {h.alias!r} has invalid declared_files; "
            "expected a tuple of strings"
        )
    rendered = render_handoff_markdown(h)
    src = h.project_path.strip()
    if src and src in rendered:
        raise ValueError(
            f"cross handoff for alias {h.alias!r} leaks the source "
            f"project_path {src!r} into the rendered runtime body; the "
            f"runtime must only ever see the child worktree path"
        )


def load_handoff(json_path: Path) -> Handoff:
    """Reconstruct + validate a :class:`Handoff` from its JSON sidecar.

    The JSON is the source of truth (ADR 0050). Raises ``ValueError`` for
    malformed JSON, an unexpected shape, or a handoff that fails
    :func:`validate_handoff`.
    """
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"cross handoff JSON is not parseable: {json_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"cross handoff JSON must be an object, got "
            f"{type(payload).__name__}: {json_path}"
        )
    known = {f.name for f in fields(Handoff)}
    unknown = set(payload) - known
    if unknown:
        raise ValueError(
            f"cross handoff JSON has unknown field(s) {sorted(unknown)}: "
            f"{json_path}"
        )
    missing_keys = {
        f.name for f in fields(Handoff)
        if f.name not in _OPTIONAL_ON_LOAD
    } - set(payload)
    if missing_keys:
        raise ValueError(
            f"cross handoff JSON is missing field(s) {sorted(missing_keys)}: "
            f"{json_path}"
        )
    declared_files = payload["declared_files"]
    if not isinstance(declared_files, list) or any(
        not isinstance(path, str) for path in declared_files
    ):
        raise ValueError(
            f"cross handoff JSON declared_files must be a list of strings: "
            f"{json_path}"
        )
    h = Handoff(
        parent_run_id=payload["parent_run_id"],
        profile=payload["profile"],
        alias=payload["alias"],
        project_path=payload["project_path"],
        approved_cross_plan_path=payload["approved_cross_plan_path"],
        full_cross_plan_path=payload["full_cross_plan_path"],
        full_cross_plan_markdown=payload["full_cross_plan_markdown"],
        cross_validation_summary=payload["cross_validation_summary"],
        cross_validation_verdict=dict(payload["cross_validation_verdict"] or {}),
        project_subtask=payload["project_subtask"],
        declared_files=tuple(declared_files),
        sibling_aliases=tuple(payload.get("sibling_aliases", ()) or ()),
        interface_contract=payload.get("interface_contract", "") or "",
        implementation_order=payload.get("implementation_order", "") or "",
    )
    validate_handoff(h)
    return h


def write_handoff(h: Handoff, alias_dir: Path) -> Path:
    """Write ``implementation_handoff.{json,md}`` under ``alias_dir``.

    The JSON is canonical and is validated before write (ADR 0050); the
    markdown is rendered from the same typed object as a derived audit /
    human view. Returns the path to the **JSON** file — the canonical
    handoff path passed to ``run_pipeline(handoff_path=...)``. Caller is
    responsible for ensuring ``alias_dir`` exists.
    """
    validate_handoff(h)
    json_path = alias_dir / "implementation_handoff.json"
    md_path = alias_dir / "implementation_handoff.md"
    payload = asdict(h)
    payload["declared_files"] = list(payload["declared_files"])
    payload["sibling_aliases"] = list(payload["sibling_aliases"])
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    md_path.write_text(render_handoff_markdown(h), encoding="utf-8")
    return json_path


def resume_project_phase_handoff(
    *,
    cross_ckpt: dict,
    run_dir: Path,
    output_dir,
    session: dict,
    success: Callable,
) -> bool:
    """Apply the operator's decision for a resumed project phase handoff.

    Extracted from the cross run body (ADR 0047 Phase F refactor). Called
    only when the checkpoint flags a pending ``project``-kind phase
    handoff on a resume. Loads the operator's decision artifact, then
    either finalizes the run as ``halted`` (returns ``True`` so the caller
    short-circuits) or dispatches the decision via
    :func:`sdk.phase_handoff.phase_handoff_decide` and clears the pending
    marker (returns ``False`` so the run continues). ``success`` is the
    presentation-gated renderer, threaded explicitly so SILENT stays
    stdout-free.
    """
    from pipeline.control import (
        HandoffDecisionContext,
        load_handoff_decision,
    )
    from pipeline.cross_project.checkpoint import (
        write_cross_checkpoint as _write_cross_checkpoint,
    )
    from pipeline.cross_project.terminal import (
        finalize_cross_terminal as _finalize_cross_terminal,
    )
    from pipeline.run_state.terminal import evict_cross_handoff_markers
    from sdk.phase_handoff import phase_handoff_decide

    _parent_handoff_id = str(cross_ckpt.get("phase_handoff_id") or "")
    _alias = str(cross_ckpt.get("phase_handoff_project_alias") or "")
    _child_handoff_id = str(cross_ckpt.get("phase_handoff_child_id") or "")
    if not _parent_handoff_id or not _alias or not _child_handoff_id:
        raise RuntimeError(
            f"Cannot resume cross run {run_dir.name!r}: checkpoint "
            "has an incomplete project handoff marker."
        )
    _decision = load_handoff_decision(
        HandoffDecisionContext(
            run_id=run_dir.name,
            handoff_id=_parent_handoff_id,
            runs_dir=run_dir.parent,
            cwd=None,
            missing_message=(
                f"Cannot resume cross run {run_dir.name!r}: checkpoint "
                f"flags handoff {_parent_handoff_id!r} as pending but "
                "no decision artifact was found."
            ),
            invalid_message_prefix=(
                f"Cannot resume cross run {run_dir.name!r}: decision "
                f"artifact for handoff {_parent_handoff_id!r} failed "
                "strict validation"
            ),
        ),
    )
    if _decision.action == "halt":
        _finalize_cross_terminal(
            run_dir=run_dir if output_dir else None,
            session=session,
            status="halted",
            halt_reason="phase_handoff_halt",
            cross_ckpt=cross_ckpt,
        )
        evict_cross_handoff_markers(cross_ckpt)
        _write_cross_checkpoint(run_dir, cross_ckpt)
        success("Cross run halted by operator")
        return True
    phase_handoff_decide(
        _alias,
        _child_handoff_id,
        _decision.action,
        feedback=_decision.feedback or None,
        note=_decision.note,
        runs_dir=run_dir,
        cwd=None,
    )
    evict_cross_handoff_markers(cross_ckpt)
    cross_ckpt.setdefault("sub_status", {})[_alias] = "awaiting_phase_handoff"
    _write_cross_checkpoint(run_dir, cross_ckpt)
    return False
