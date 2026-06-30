"""Cross-project per-alias dispatch.

Owns the ``for alias in projects`` loop that used to live inline in
:func:`pipeline.cross_project.orchestrator.run_cross_pipeline`. Each iteration:

* honours per-alias resume state from the cross checkpoint (``done`` is
  skipped, ``failed`` / ``awaiting_phase_handoff`` resume in-place);
* writes the per-project plan artifact and (when the cross level
  produced a plan) the implementation handoff;
* invokes the child :func:`pipeline.project.app.run_project_pipeline`
  with the projected child profile and
  ``presentation=PresentationPolicy.SILENT`` (ADR 0046 Phase D) so
  per-project banners / success chips / DONE block / phase headers no
  longer leak into the cross transcript; terminal cross runs opt back
  into parsed phase response blocks for mono-run parity;
* records the child's session under ``session["phases"]["projects"]
  [alias]``;
* proxies any child phase handoff up to the cross parent via
  :func:`pipeline.cross_project.handoff_payloads.apply_cross_phase_handoff_pause`
  (with the ``project:<alias>:<child_id>`` id prefix);
* survives child exceptions per ADR 0025 Phase 3 — the structured
  ``status="failed"`` sub-session lets the system release gate raise
  the missing/crashed child as the release blocker instead of the
  cross runner crashing before any gate runs.

Side-effects are delivered via :class:`DispatchPorts` so the embedder
(orchestrator, headless harness, future test fixtures) owns banner /
success / warn rendering — the driver itself does not reach back into
``cross_project.orchestrator``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agents.runtimes import AgentProvider, MockAgentProvider
from core.io.ansi import C
from pipeline.cross_project.checkpoint import write_cross_checkpoint
from pipeline.cross_project.handoff import Handoff, write_handoff
from pipeline.cross_project.handoff_payloads import (
    apply_cross_phase_handoff_pause,
    build_project_phase_handoff_payload,
)
from pipeline.cross_project.task_plan import CrossTaskPlan
from pipeline.project.app import run_project_pipeline
from pipeline.project.types import PresentationPolicy, ProjectRunRequest

# ``SessionMode`` stays on the stable shim surface (ADR 0042 Phase J —
# one of the four stable orchestrator-module re-exports). Importing it
# from ``pipeline.project_orchestrator`` is the canonical path.
from pipeline.project_orchestrator import SessionMode

#: Color escape for the ``▶ SUB-PIPELINE [alias]`` banner. Resolved at
#: import via :data:`core.io.ansi.C.BLUE` so the palette stays single-
#: sourced.
_SUB_PIPELINE_BANNER_COLOR = C.BLUE


@dataclass(slots=True, frozen=True)
class DispatchPorts:
    """Side-effect ports the embedder supplies to the dispatch driver.

    Decoupling the side-effect surface (rather than reaching back into
    ``cross_project.orchestrator`` via late imports) keeps
    ``project_dispatch.py`` physically AND semantically separable: tests
    and alternative embedders can pass silent or capturing
    implementations without monkey-patching the orchestrator module.
    """
    banner: Callable[..., None]   #: ``(phase: str, title: str, color: str, **kwargs) -> None``
    success: Callable[[str], None]
    warn:    Callable[[str], None]


@dataclass(slots=True)
class ProjectDispatchContext:
    """Input bundle for :func:`run_project_dispatch`.

    Carries immutable run inputs plus three pieces of mutable shared
    state — ``cross_ckpt``, ``session``, ``cross_phase_usage`` — that
    the driver updates in place so the caller observes the same
    side-effects the original inline block produced.
    """
    # Run identity / inputs
    task: str
    projects: dict[str, Path]
    task_plan: CrossTaskPlan | None
    resume_from: str | None
    dry_run: bool
    max_rounds: int
    code_model: str
    phase_config: Any                          # PhaseAgentConfig | None
    child_profile: Any | None                  # Profile | None
    requested_profile_name: str
    has_global_plan: bool
    provider: AgentProvider
    hypothesis_enabled: bool
    followup_session_seeds_per_alias: dict | None
    run_dir: Path
    output_dir: Any                            # truthy → persist to disk
    plan_output: str
    plan_review_dict: dict | None
    # Mutable shared state
    cross_ckpt: dict
    session: dict
    cross_phase_usage: dict[str, dict]
    # Ports
    ports: DispatchPorts
    # ADR 0047 Phase E — cross-level presentation flag. ``True`` (default)
    # = legacy CLI / SDK terminal output unchanged; ``False`` = banners
    # / chips suppressed but ``log_phase`` writes preserved. Threaded
    # from ``request.presentation`` via the cross app body. Used by
    # the ``apply_cross_phase_handoff_pause`` call below to suppress
    # the parent-side pause-banner warn under SILENT.
    terminal: bool = True
    # ADR 0112 §1 (increment B): the run-scoped, in-memory ParticipantSet seeded
    # provisionally in ``setup_cross_run``. After each child returns, this driver
    # binds the alias's ``editable_checkout`` to the child's REAL isolated worktree
    # (``session['worktree']['path']``) — symmetric isolation reads the child's own
    # worktree, it does NOT create a parent worktree. ``None`` when no set was
    # threaded (older embedders / some test fixtures) → the bind is a no-op.
    participant_set: Any = None


@dataclass(slots=True, frozen=True)
class ProjectDispatchResult:
    """Outcome of :func:`run_project_dispatch`.

    ``paused`` is the load-bearing signal: ``True`` means a child
    project paused on a phase handoff, the driver has already proxied
    the pause up to the parent meta + checkpoint, and the caller MUST
    return the session to the SDK immediately (do not run subsequent
    gates / aliases). ``False`` means the loop completed (every alias
    either done, skipped, or recorded as failed) and the caller may
    proceed to ``contract_check`` and beyond.
    """
    paused: bool


def _bind_child_editable_checkout(
    ctx: ProjectDispatchContext, alias: str, project_session: Any,
) -> None:
    """Bind ``alias``'s ``editable_checkout`` to the child's real isolated worktree.

    Reads ``project_session['worktree']['path']`` (the child's own per-run worktree,
    created by its mono ``isolation_setup``) and rebinds the provisional participant
    via :meth:`pipeline.participants.ParticipantSet.bind_editable_checkout`. The
    child's own ``worktree['isolation']`` regime is threaded through so a degraded
    isolation-off child (``path`` == canonical) stays off inside the per_run cross
    set — its participant collapses ``editable_checkout`` onto ``delivery_target``
    and resolves to no isolated source, preserving the in-place degraded contract
    rather than fail-closing on ``worktree == source``. A no-op when no set was
    threaded, the child carries no worktree path, or the alias has no provisional
    entry — never fails the dispatch loop (ADR 0112 §1)."""
    pset = getattr(ctx, "participant_set", None)
    if pset is None or not isinstance(project_session, dict):
        return
    worktree = project_session.get("worktree")
    path = worktree.get("path") if isinstance(worktree, dict) else None
    if not path:
        return
    child_isolation = worktree.get("isolation") if isinstance(worktree, dict) else None
    # No provisional participant for this alias is a defensive no-op (the set is
    # seeded per alias in run_setup) — never break the dispatch loop.
    with contextlib.suppress(KeyError):
        pset.bind_editable_checkout(
            alias, str(path),
            isolation=str(child_isolation) if child_isolation is not None else None,
        )


def run_project_dispatch(ctx: ProjectDispatchContext) -> ProjectDispatchResult:
    """Drive the per-alias child execution loop.

    Returns ``ProjectDispatchResult(paused=True)`` when a child handoff
    is proxied up to the parent (caller must short-circuit to
    ``return session``). Otherwise iterates all aliases and returns
    ``paused=False``.

    Phase A invariant 2 — child-session preservation on resume. Aliases
    that already finished in a prior run (``sub_status == "done"``)
    keep their previously-persisted ``session["phases"]["projects"]
    [alias]`` entry across the resume. Without this preservation, a
    CFA-pause resume would wipe completed children's worktree path,
    final_acceptance verdict, and metrics — Phase B's cross delivery
    reads those fields to know what to commit. Aliases that did NOT
    finish (``failed`` / ``running`` / ``awaiting_phase_handoff`` /
    absent) are cleared so the upcoming dispatch loop can re-attempt
    them.
    """
    existing_children = ctx.session.get("phases", {}).get("projects", {})
    if isinstance(existing_children, dict):
        sub_status_map = ctx.cross_ckpt.get("sub_status") or {}
        preserved = {
            alias: child_entry
            for alias, child_entry in existing_children.items()
            if sub_status_map.get(alias) == "done"
        }
    else:
        preserved = {}
    ctx.session["phases"]["projects"] = preserved

    # ADR 0112 §1 (increment B) — resume re-bind. The run-scoped ParticipantSet is
    # re-seeded PROVISIONAL in ``setup_cross_run`` on every (re)entry, so on a resume
    # the editable_checkout of aliases that already finished in a prior run is empty.
    # Their child session was never re-dispatched (the ``sub_status == "done"`` branch
    # short-circuits below), so the fresh-dispatch bind seam never runs for them.
    # Rebind each preserved alias from its durable child session
    # (``worktree['path']``) so any later Control / verification path reading this set
    # sees the child's REAL isolated checkout instead of an unbound participant —
    # closing the cross resume/cold-path rehydration gap.
    for alias, child_entry in preserved.items():
        _bind_child_editable_checkout(ctx, alias, child_entry)

    if ctx.child_profile is None:
        ctx.ports.success(
            f"Profile {ctx.requested_profile_name!r} has no project-scoped "
            "steps; skipping per-project sub-pipelines."
        )
        return ProjectDispatchResult(paused=False)

    approved_plan_path = str(ctx.run_dir / "cross_plan.md")
    cross_summary = (
        (ctx.plan_review_dict or {}).get("short_summary", "")
        if ctx.plan_review_dict else ""
    )

    # ADR 0054 — the approved cross plan arrives as a typed ``CrossTaskPlan``
    # (normalized once in app.py via ``normalize_cross_task_plan``); dispatch
    # consumes that typed view instead of re-parsing ``plan_output``.
    # ``interface_contract`` / ``implementation_order`` are identical for every
    # alias, so they are read out of the loop; only the per-alias spec differs.
    # Dry runs and review-only projections carry ``task_plan=None`` → empty
    # slices and the per-alias fallback to ``ctx.task``.
    interface_contract = ctx.task_plan.interface_contract if ctx.task_plan else ""
    implementation_order = (
        "\n".join(ctx.task_plan.implementation_order) if ctx.task_plan else ""
    )
    units_by_alias = ctx.task_plan.units_by_alias() if ctx.task_plan else {}
    # ADR 0054: the handoff's ``full_cross_plan_markdown`` field is MARKDOWN
    # (its companion ``full_cross_plan_path`` points at ``cross_plan.md``), NOT
    # the canonical JSON. Default to the persisted, rendered+aliasized
    # ``cross_plan.md`` (the audit render) so the fallback handoff body never
    # dumps raw JSON into the child prompt. Dry runs carry a sentinel, not a
    # plan, so they keep ``ctx.plan_output`` verbatim. Reading the rendered
    # artifact is an artifact read, not a plan parse.
    full_plan_markdown = ctx.plan_output
    if not ctx.dry_run and ctx.has_global_plan:
        _md_path = ctx.run_dir / "cross_plan.md"
        if _md_path.exists():
            full_plan_markdown = _md_path.read_text(encoding="utf-8")

    for alias, project_path in ctx.projects.items():
        outcome = _dispatch_one_alias(
            ctx,
            alias=alias,
            project_path=project_path,
            approved_plan_path=approved_plan_path,
            cross_summary=cross_summary,
            interface_contract=interface_contract,
            implementation_order=implementation_order,
            full_plan_markdown=full_plan_markdown,
            units_by_alias=units_by_alias,
        )
        if outcome is _DISPATCH_PAUSED:
            return ProjectDispatchResult(paused=True)
    return ProjectDispatchResult(paused=False)


# ── per-alias step ──────────────────────────────────────────────────────────

#: Sentinel returned by :func:`_dispatch_one_alias` when the child paused
#: on a phase handoff — the caller must short-circuit the outer loop.
_DISPATCH_PAUSED = object()
#: Sentinel for "finished this alias (done / skipped / failed); continue
#: to the next alias".
_DISPATCH_CONTINUE = object()


def _dispatch_one_alias(
    ctx: ProjectDispatchContext,
    *,
    alias: str,
    project_path: Path,
    approved_plan_path: str,
    cross_summary: str,
    interface_contract: str = "",
    implementation_order: str = "",
    full_plan_markdown: str = "",
    units_by_alias: dict[str, Any] | None = None,
) -> object:
    """Single per-alias iteration. Returns one of the two module sentinels."""
    unit = (units_by_alias or {}).get(alias)
    project_task = (unit.spec if unit else "") or ctx.task

    # Resume skip: alias finished previously → pass.
    # Failed mid-flight / paused on child handoff → forward
    # resume_from so the child's checkpoints.db picks up where it
    # left off.
    sub_status = ctx.cross_ckpt.get("sub_status", {}).get(alias)
    if sub_status == "done":
        ctx.ports.success(f"[{alias}] already done in previous run — skipping")
        return _DISPATCH_CONTINUE
    sub_resume = (
        alias
        if (
            sub_status in {"failed", "awaiting_phase_handoff"}
            and ctx.resume_from
        )
        else None
    )

    # ``▶`` arrow distinguishes the per-project sub-run header from
    # the cross-orchestrator's own ``═══`` banners. ``plan=`` wording
    # reflects what actually ran at the cross level: ``handoff`` when
    # the projected profile produced one, ``none`` when it didn't
    # (review-only projections).
    plan_marker = (
        "satisfied by approved cross handoff"
        if ctx.has_global_plan else "none (review-only projection)"
    )
    ctx.ports.banner(
        f"▶ SUB-PIPELINE [{alias}]",
        f"Project: {Path(project_path).name}"
        + (" (RESUME)" if sub_resume else "")
        + f"  profile={ctx.requested_profile_name}  "
          f"plan={plan_marker}",
        _SUB_PIPELINE_BANNER_COLOR,
    )

    if ctx.dry_run:
        ctx.ports.warn(
            f"[DRY RUN] Would run pipeline for [{alias}]: "
            f"{project_task[:80]}…"
        )
        ctx.session["phases"]["projects"][alias] = {"dry_run": True}
        return _DISPATCH_CONTINUE

    alias_artifacts = ctx.run_dir / alias
    alias_artifacts.mkdir(parents=True, exist_ok=True)

    # Per-project plan artifact (mirrors standalone PLAN phase).
    if not sub_resume:
        plan_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        plan_content = (
            f"# Plan — {alias}\n\n"
            f"Task: {project_task}\n\n"
            f"## Source\nExtracted from cross-plan for project `{alias}`.\n\n"
            f"## Subtask\n{project_task}\n"
        )
        (alias_artifacts / f"plan_{plan_ts}.md").write_text(
            plan_content, encoding="utf-8",
        )

    # Write the handoff artifact only when the cross level actually
    # produced a plan. Review-only projections have no plan to hand
    # off; the coherence rule already guarantees such projections
    # contain no implement/repair phases, so the child request built
    # below (``run_project_pipeline`` per ADR 0046 Phase D) leaves
    # ``handoff_path=None`` without surfacing a missing-handoff error.
    handoff_path: str | None = None
    if ctx.has_global_plan:
        siblings = tuple(a for a in ctx.projects if a != alias)
        handoff = Handoff(
            parent_run_id=ctx.run_dir.name,
            profile=ctx.requested_profile_name,
            alias=alias,
            project_path=str(project_path),
            approved_cross_plan_path=approved_plan_path,
            full_cross_plan_path=approved_plan_path,
            full_cross_plan_markdown=full_plan_markdown,
            cross_validation_summary=cross_summary,
            cross_validation_verdict=dict(ctx.plan_review_dict or {}),
            project_subtask=project_task,
            sibling_aliases=siblings,
            interface_contract=interface_contract,
            implementation_order=implementation_order,
        )
        # ADR 0050: ``write_handoff`` validates the typed handoff and
        # returns the canonical JSON path (source of truth). The child
        # renders the prompt body from it; the sibling .md is audit-only.
        handoff_path = str(write_handoff(handoff, alias_artifacts))

    ctx.cross_ckpt["sub_status"][alias] = "running"
    write_cross_checkpoint(ctx.run_dir, ctx.cross_ckpt)

    # Per-alias follow-up seeds: the cross extractor produces
    # ``{alias → {phase_role → parent_sid}}``; we hand each child its
    # own slice so alias A's session ids never leak into alias B.
    # ``None`` / missing alias falls through to a fresh agent context
    # for that child (same as single-project with no seeds).
    #
    # Disable hypothesis for children that have seeds (single-project
    # rationale: hypothesis fires on the plan agent before plan and
    # would consume the one-shot seed). Children without seeds keep
    # the cross-level ``hypothesis_enabled`` value.
    alias_seeds: dict[str, str] | None = (
        (ctx.followup_session_seeds_per_alias or {}).get(alias)
    )
    child_hypothesis = False if alias_seeds else ctx.hypothesis_enabled

    sub_failed = False
    sub_paused = False
    try:
        # ADR 0046 Phase D — the win condition.
        #
        # Build a typed ``ProjectRunRequest`` and route through
        # ``run_project_pipeline`` instead of the legacy 28-kwarg
        # ``run_pipeline`` wrapper. The two new fields beyond the
        # original kwarg surface are:
        #
        #   * ``presentation=PresentationPolicy.SILENT`` — suppresses
        #     per-project banners / success chips / DONE block /
        #     handoff warnings / phase START/END headers on the child
        #     run. The cross transcript keeps only its own
        #     ``▶ SUB-PIPELINE [alias]`` separator (rendered by the
        #     cross orchestrator, not the child); the child's
        #     ``progress.log`` + ``events.jsonl`` still receive every
        #     ``phase.start`` / ``phase.end`` / ``run.end`` event
        #     because ``log_phase(...)`` is unconditional under both
        #     presentations (ADR 0046 stop #9).
        #
        #   * ``no_interactive=True`` — required hard-invariant
        #     companion of ``SILENT`` (``ProjectRunRequest.__post_init__``
        #     raises ``ValueError`` if SILENT runs with interactive
        #     prompts enabled — they are terminal-by-definition).
        #     Cross-project runs were always non-interactive in
        #     practice; this just makes it explicit.
        #
        # The remaining 19 kwargs are byte-identical to the prior
        # ``run_pipeline(...)`` call. The return type is a
        # ``ProjectRunResult``; ``.session`` is the dict the surrounding
        # cross-orchestrator code expects.
        _project_result = run_project_pipeline(
            ProjectRunRequest(
                task=project_task,
                project_dir=str(project_path),
                max_rounds=ctx.max_rounds,
                model=ctx.code_model,
                profile_obj=ctx.child_profile,
                # Pass the requested profile name (e.g. "feature") so
                # children surface ``profile=feature`` in meta /
                # run.start / headers rather than the synthetic
                # ``feature#project`` projection name. The actual
                # execution shape is supplied via ``profile_obj``.
                profile_name=ctx.requested_profile_name,
                plan_source="cross",
                handoff_path=handoff_path,
                output_dir=alias_artifacts,
                dry_run=False,
                provider=ctx.provider,
                session_mode=(
                    SessionMode.STATELESS
                    if isinstance(ctx.provider, MockAgentProvider)
                    else SessionMode.AUTO
                ),
                phase_config=ctx.phase_config,
                ma_artifacts_dir_override=str(alias_artifacts),
                resume_from=alias,
                # REA-3.6: parent_run_id + project_alias let MCP /
                # evidence reconstruct the parent → children timeline.
                parent_run_id=ctx.run_dir.name,
                project_alias=alias,
                hypothesis_enabled=child_hypothesis,
                followup_session_seeds=alias_seeds,
                # ADR 0046 Phase D — silent child + non-interactive
                # invariant (see block comment above).
                presentation=PresentationPolicy.SILENT,
                render_phase_outputs=ctx.terminal,
                no_interactive=True,
            ),
        )
        project_session = _project_result.session
        ctx.session["phases"]["projects"][alias] = project_session
        # ADR 0112 §1 (increment B): bind the alias's editable_checkout to the
        # child's ACTUAL isolated worktree, read from the child session just saved
        # above. This is the F1 seam — the parent set now carries the real isolated
        # path, not a presumptive canonical one. No parent worktree is created
        # (the child's mono isolation_setup already made it); for a degraded
        # isolation-off child the path equals the canonical project, so
        # editable_checkout == delivery_target and the degraded contract holds.
        _bind_child_editable_checkout(ctx, alias, project_session)
        if (
            isinstance(project_session, dict)
            and project_session.get("status") == "awaiting_phase_handoff"
            and isinstance(project_session.get("phase_handoff"), dict)
        ):
            sub_paused = True
            child_payload = project_session["phase_handoff"]
            parent_payload = build_project_phase_handoff_payload(
                alias=alias, child_payload=child_payload,
            )
            ctx.cross_ckpt["sub_status"][alias] = "awaiting_phase_handoff"
            ctx.cross_ckpt["phase_handoff_kind"] = "project"
            ctx.cross_ckpt["phase_handoff_project_alias"] = alias
            ctx.cross_ckpt["phase_handoff_child_id"] = child_payload["id"]
            apply_cross_phase_handoff_pause(
                run_dir=ctx.run_dir if ctx.output_dir else None,
                session=ctx.session,
                cross_ckpt=ctx.cross_ckpt,
                payload=parent_payload,
                cross_phase_usage=ctx.cross_phase_usage,
                terminal=ctx.terminal,
            )
            return _DISPATCH_PAUSED
    except Exception as child_exc:
        # ADR 0025 Phase 3: catch child sub-pipeline exceptions into a
        # structured failed sub-session entry and continue. The cross
        # runner must reach contract_check and cross_final_acceptance
        # so the system release gate can surface the crash as a
        # release blocker (CFA_MISSING_CHILD_<alias>) rather than
        # re-raising before any gate runs. Without this, "missing /
        # crashed child" is a runner halt before the gate, not a
        # structured release-shape failure surface.
        sub_failed = True
        ctx.cross_ckpt["sub_status"][alias] = "failed"
        write_cross_checkpoint(ctx.run_dir, ctx.cross_ckpt)
        ctx.session["phases"]["projects"][alias] = {
            "status": "failed",
            "error": f"{type(child_exc).__name__}: {child_exc}",
            "phases": {},
        }
        ctx.ports.warn(
            f"[{alias}] sub-pipeline raised "
            f"{type(child_exc).__name__} — recorded as failed; "
            f"continuing to cross_final_acceptance which will "
            f"surface the crash as a release blocker."
        )
    finally:
        if not sub_failed and not sub_paused:
            ctx.cross_ckpt["sub_status"][alias] = "done"
            write_cross_checkpoint(ctx.run_dir, ctx.cross_ckpt)

    return _DISPATCH_CONTINUE
