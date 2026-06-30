"""Run-level setup + header presentation for the cross-project pipeline.

The typed cross entry surface (``pipeline/cross_project/app.py``) turns a
resolved :class:`~pipeline.cross_project.profile_setup.CrossProfileSetup`
into a live run. That work splits in two because of execution order:

* :func:`setup_cross_run` is the **early** setup. It allocates the
  ``session_ts``, wires run logging, normalizes ``cross_mode``, emits the
  ``run.start`` event, builds the persisted ``session`` dict, materializes
  the run directory, and opens the cross checkpoint (re-reading it on a
  ``--resume``). It runs before cross-level agent setup so ``run.start``
  and logging fire up-front, and it returns a typed
  :class:`CrossRunSetup` so the coordinator wires the run off a single
  structured object.

* :func:`render_cross_pipeline_header` is the **late** presentation. It
  needs the per-phase agent / runtime metadata resolved by cross agent
  setup, so it is called *after* that step. It paints the cross run header,
  the projected pipeline sections, and the per-project plugin lines. All
  three are stdout courtesies gated by ``terminal`` — file + event sinks
  are never gated (ADR 0046 stop #9); the structural record lives in the
  ``run.start`` event and ``meta.json``.

This module is a leaf peer: it MUST NOT import from
:mod:`pipeline.cross_project.orchestrator`.
"""

import dataclasses
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from core.observability import events as _events
from pipeline.cross_project.checkpoint import read_cross_checkpoint
from pipeline.cross_project.profile_setup import CrossProfileSetup, _gate_will_run
from pipeline.cross_project.rendering import paint
from pipeline.engine import save_session, setup_run_logging
from pipeline.plugins import load_plugin


@dataclasses.dataclass(frozen=True)
class CrossRunSetup:
    """Resolved run-level state for one cross run.

    Built early (before cross agent setup) so ``run.start`` + logging fire
    up-front and the persisted ``session`` / checkpoint are ready for the
    planning loop and the resume phase-handoff path.

    ``participant_set`` is the run-scoped, IN-MEMORY
    :class:`pipeline.participants.ParticipantSet` seeded here with one PROVISIONAL
    participant per cross alias (ADR 0112 §1, increment B). Each provisional entry
    knows its alias / repo / delivery_target (the canonical project path) but NOT
    yet its ``editable_checkout`` — that is bound post-dispatch from the child's
    real isolated worktree in :mod:`pipeline.cross_project.project_dispatch`. It is
    a separate field, never folded into ``session`` (the set is not persisted; the
    durable form stays each child's ``meta.worktree``).
    """

    session_ts: str
    session: dict
    run_dir: Path
    cross_ckpt: dict
    cross_mode: str
    participant_set: Any = None


def setup_cross_run(
    *,
    task: str,
    projects: Mapping[str, Path],
    model: str,
    output_dir: Path,
    cross_mode: str,
    resume_from: str | None,
    resume_mode: str | None,
    followup_parent_run_id: str | None,
    followup_parent_run_dir: Any | None,
    followup_parent_status: str | None,
    followup_base_task: str | None,
    resumed_meta: Mapping[str, Any] | None,
    profile_setup: CrossProfileSetup,
    terminal: bool,
) -> CrossRunSetup:
    """Wire logging, emit ``run.start``, build the session + checkpoint.

    ``cross_mode`` is normalized to ``"full"`` unless it is ``"plan"``.
    The ``success`` resume line is gated by ``terminal``; the ``run.start``
    event and ``session`` shape are never gated.
    """
    from pipeline.cross_project.rendering import silent_renderers
    (_banner, success, _warn, _preview, _rcpp, _print, _C) = silent_renderers(
        terminal,
    )

    requested_profile = profile_setup.requested_profile
    projected_profile_name = profile_setup.projected_profile_name

    session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # ADR 0047 Phase H — thread ``terminal`` into ``setup_run_logging``
    # so the two grey ``📄 Live output`` / ``📡 Events`` courtesy chips
    # suppress under SILENT. File sinks
    # (``set_progress_log`` / ``init_event_store`` / ``set_agent_log``)
    # still fire regardless — ADR 0046 stop #9 invariant inherited.
    setup_run_logging(
        output_dir, session_ts,
        is_resume=bool(resume_from),
        terminal=terminal,
    )
    if cross_mode not in ("full", "plan"):
        cross_mode = "full"
    _events.emit("run.start",
                 task=task[:500],
                 run_kind="cross_project",
                 projects=[
                     {"alias": alias, "path": str(path.resolve())}
                     for alias, path in projects.items()
                 ],
                 cross_mode=cross_mode,
                 profile=requested_profile.name,
                 plan_source="cross",
                 projected_profile=projected_profile_name)

    session = {
        "task": task,
        "projects": {a: str(p) for a, p in projects.items()},
        "model": model,
        "cross_mode": cross_mode,
        "profile": requested_profile.name,
        "plan_source": "cross",
        "projected_profile": projected_profile_name,
        "timestamp": datetime.now().isoformat(),
        "status": "running",
        "phases": {}
    }
    if resume_mode:
        session["resume_mode"] = resume_mode
    if followup_parent_run_id:
        session["parent_run_id"] = followup_parent_run_id
    if followup_parent_run_dir:
        session["parent_run_dir"] = followup_parent_run_dir
    if followup_parent_status:
        session["parent_status"] = followup_parent_status
    if followup_base_task:
        session["base_task"] = followup_base_task
    if resumed_meta is not None:
        _resumed_phases = resumed_meta.get("phases")
        if isinstance(_resumed_phases, dict):
            session["phases"] = dict(_resumed_phases)
        _resumed_pending = resumed_meta.get("pending_gate")
        if isinstance(_resumed_pending, dict):
            session["pending_gate"] = dict(_resumed_pending)

    run_dir = output_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    if not resume_from:
        save_session(run_dir, session)

    cross_ckpt = read_cross_checkpoint(run_dir) if resume_from else {
        "phase0_done": False, "sub_status": {},
    }
    if resume_from:
        success(
            f"Resuming cross run {resume_from}: "
            f"phase0={'done' if cross_ckpt.get('phase0_done') else 'pending'}, "
            f"sub={cross_ckpt.get('sub_status', {})}"
        )

    # ADR 0112 §1 (increment B): seed the run-scoped, in-memory ParticipantSet
    # with one PROVISIONAL participant per cross alias. The canonical
    # alias→project_path map is known here; ``editable_checkout`` stays unbound
    # until project_dispatch binds it to the child's real isolated worktree
    # post-dispatch (no parent worktree is created — symmetric isolation reads the
    # child's own mono-isolation worktree). The set is never persisted to session.
    participant_set = _seed_cross_provisional_participants(projects)

    return CrossRunSetup(
        session_ts=session_ts,
        session=session,
        run_dir=run_dir,
        cross_ckpt=cross_ckpt,
        cross_mode=cross_mode,
        participant_set=participant_set,
    )


def _seed_cross_provisional_participants(
    projects: Mapping[str, Path],
) -> Any:
    """Build the cross run's :class:`ParticipantSet` with one provisional
    participant per alias (ADR 0112 §1).

    Each participant's ``repo`` and ``delivery_target`` are the canonical project
    path; its ``editable_checkout`` is left UNBOUND (provisional) — it is bound to
    the child's actual isolated worktree post-dispatch. Isolation mode is
    ``per_run`` so an unbound participant resolves to the fail-closed branch rather
    than the canonical sibling until its real checkout is bound.
    """
    from pipeline.participants import ParticipantSet

    participant_set = ParticipantSet(isolation="per_run")
    for alias, path in projects.items():
        participant_set.add_provisional(
            alias=alias,
            repo=str(path),
            delivery_target=str(path),
        )
    return participant_set


def render_cross_pipeline_header(
    *,
    terminal: bool,
    run_dir: Path | None,
    task: str,
    projects: Mapping[str, Path],
    agents_block: list,
    project_agents_block: list,
    pipeline_runtimes: dict[str, str],
    projection: Any,
    cross_mode: str,
    max_rounds: int,
    requested_profile_name: str,
    contract_gate_policy: Any,
    cfa_gate_policy: Any,
    resume_from: str | None,
    followup_parent_run_id: str | None,
    followup_base_task: str | None,
) -> None:
    """Paint the cross run header, pipeline sections, and plugin lines.

    Called after cross agent setup so ``agents_block`` /
    ``project_agents_block`` / ``pipeline_runtimes`` are resolved. Under
    SILENT every ``print`` resolves to a no-op (file + event sinks are
    never gated); ``load_plugin`` still runs so behaviour matches the
    pre-extraction body byte-for-byte.
    """
    from pipeline.cross_project.rendering import silent_renderers
    (
        _banner, _success, _warn, _preview,
        _rcpp, print, C,  # noqa: A001 — print shadow
    ) = silent_renderers(terminal)
    from core.io.pipeline_block import render_pipeline_sections
    from core.io.transcript import render_cross_run_header

    _run_dir_for_header = run_dir
    _projection_label = (
        "global + per-project" if projection.project_steps else "global only"
    )
    print(render_cross_run_header(
        run_id=_run_dir_for_header.name if _run_dir_for_header is not None else None,
        task=task,
        projects={a: str(p) for a, p in projects.items()},
        agents=agents_block,
        project_agents=project_agents_block,
        cross_mode=cross_mode,
        rounds=max_rounds,
        profile=requested_profile_name,
        plan_source="cross",
        projection=_projection_label,
        output_log=str(_run_dir_for_header / "output.log") if _run_dir_for_header else None,
        events_log=str(_run_dir_for_header / "events.jsonl") if _run_dir_for_header else None,
        resumed=bool(resume_from),
        followup_parent_run_id=followup_parent_run_id,
        followup_base_task=followup_base_task,
    ))
    # Cross-level pipeline visualization — the FULL projected shape, in
    # one block: the cross-level chain (Global), the per-project
    # sub-pipeline (Per project), and the terminal cross gates. Child
    # project runs dispatch under ``PresentationPolicy.SILENT`` and never
    # print their own pipeline header, so this block is the only place
    # the operator sees the project phases + cross gates — anything left
    # out here looks like the pipeline was lost.
    #
    # Resume highlighting on the cross surface is not wired here: the
    # cross checkpoint shape (``phase0_done`` boolean + per-project
    # ``sub_status``) does not map 1-to-1 onto the step lists, so
    # plumbing it through would need a deliberate decoder rather than a
    # peek. Mono runs do receive resume highlighting via
    # ``_peek_completed_phases``.
    _pipeline_sections: list[tuple[str, tuple]] = [
        ("Global", tuple(projection.global_steps)),
    ]
    if cross_mode == "full" and projection.project_steps:
        _pipeline_sections.append(
            (f"Per project (×{len(projects)})", tuple(projection.project_steps))
        )
    from pipeline.runtime import PhaseStep
    _gate_steps: list[Any] = []
    if cross_mode == "full" and _gate_will_run(contract_gate_policy):
        _gate_steps.append(PhaseStep(phase="contract_check"))
    if cross_mode == "full" and _gate_will_run(cfa_gate_policy):
        _gate_steps.append(PhaseStep(phase="cross_final_acceptance"))
    if _gate_steps:
        _pipeline_sections.append(("Cross gates", tuple(_gate_steps)))
    print(render_pipeline_sections(
        _pipeline_sections,
        phase_runtimes=pipeline_runtimes,
    ))
    print()
    for alias, path in projects.items():
        plugin = load_plugin(str(path))
        print(f"  {paint(f'[{alias}] plugin: {plugin.name}', C.GREY)}")


def _read_plan_file(
    plan_file: str | None, *, terminal: bool = True,
) -> str | None:
    """Прочитать pre-existing план из файла. None / отсутствующий файл → None.

    ADR 0047 Phase E — ``terminal=False`` suppresses the operator-
    facing warn lines when the file is missing or unreadable; the
    return contract (``str | None``) is unchanged so library callers
    still see the same logical signal."""
    from pipeline.cross_project.rendering import warn
    if not plan_file:
        return None
    p = Path(plan_file)
    if not p.exists():
        if terminal:
            warn(f"--plan-file: {plan_file} не существует, перегенерирую план")
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError as exc:
        if terminal:
            warn(f"--plan-file: чтение {plan_file} провалилось ({exc}), перегенерирую план")
        return None


__all__ = [
    "CrossRunSetup",
    "setup_cross_run",
    "render_cross_pipeline_header",
    "_read_plan_file",
]
