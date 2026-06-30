"""Cross-project planning sub-flow.

Owns the cross-plan / cross_validate_plan retry-and-handoff loop that
used to live inline in :func:`pipeline.cross_project.orchestrator.run_cross_pipeline`.

Public surface:

* :class:`CrossPlanningContext` — typed bundle of every input the loop
  needs (task, projects, profile projection, agents, mutable shared
  state: ``cross_ckpt``, ``session``, ``cross_phase_usage``).
* :class:`CrossPlanningResult` — what the loop returns. ``status`` is
  load-bearing: ``"paused"`` / ``"halted"`` mean the caller MUST return
  the session to the SDK immediately (the loop has already persisted
  the terminal/pause invariant); ``"approved"`` / ``"bypass"`` mean the
  caller proceeds to per-project dispatch.
* :func:`run_cross_planning` — driver.

Plus the lower-level helpers reused by the loop and by direct callers:

* :func:`approved_review_json` — render an APPROVED reviewer JSON
  envelope for dry-run paths.
* :class:`CrossValidateResult` / :func:`validate_cross_plan` — single
  cross_validate_plan invocation.

The driver intentionally uses late imports for orchestrator-local
terminal helpers (``banner``, ``success``, ``warn``, usage printers)
to avoid a circular import — orchestrator imports planning_loop at
module load, planning_loop reaches back at call time.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from core.io.ansi import paint
from pipeline.cross_project.prompts import (
    cross_plan_review_focus as _cross_plan_review_focus_impl,
    set_orchestrator_root as _set_orchestrator_root,
)
from pipeline.cross_project.session_invoke import session_aware_invoke
from pipeline.runtime.roles import SessionContinuity, SessionInvocationRole
from pipeline.runtime.run_shape import OperatingMode
from pipeline.runtime.session_disposition import decide


def _cross_continue_session(role: SessionInvocationRole) -> bool:
    """ADR 0113 cross continuity decision (single policy site, explicit policy).

    The cross planner (``plan``) and reviewer (``review``) are non-edit-shaped
    roles, so they declare the ``fresh_only`` continuity policy and always start
    FRESH — never a hardcoded ``continue_session=True``. This cross path has no
    profile step to resolve, so it passes the policy directly. The compact
    handoff (prior critique) is carried by ``cross_replan_prompt`` / the cross
    review focus, not by a resumed provider session. ``same_write_zone`` /
    ``loop_followon`` are irrelevant for ``fresh_only``; ``operating_mode``
    never relaxes continuation, so ``FAST`` is a safe fixed input here.
    """
    del role  # plan/review both resolve to fresh_only on the cross path
    return decide(
        policy=SessionContinuity.FRESH_ONLY,
        same_write_zone=False,
        loop_followon=False,
        operating_mode=OperatingMode.FAST,
    ).continue_session


def approved_review_json(short_summary: str) -> str:
    """Render an APPROVED reviewer JSON envelope.

    Mirrors :func:`pipeline.phases.adapters._approved_review_json` so the
    cross-orchestrator's dry-run reviewer paths can round-trip through
    the same :func:`pipeline.review_parser.parse_review` contract that
    real reviewer responses use. Keeping a local copy avoids importing a
    private helper across the cross boundary; the JSON contract surface
    is the single source of truth — both copies must stay byte-shape
    compatible with what ``parse_review`` accepts.
    """
    return json.dumps({
        "verdict":       "APPROVED",
        "short_summary": short_summary,
        "findings":      [],
        "risks":         [],
        "checks":        [],
    })


@dataclass(frozen=True)
class CrossValidateResult:
    """Outcome of :func:`validate_cross_plan`.

    Exposes the reviewer prompt and raw output so the caller can feed
    them into ``capture_invoke_usage`` for token-split estimation on
    runtimes that only surface ``last_tokens_total`` (e.g. Codex).
    """
    approved: bool
    critique_markdown: str
    review_dict: dict
    raw_output: str
    prompt_text: str


def validate_cross_plan(
    qa_agent: Any,
    plan_output: str,
    task: str,
    aliases: list[str],
    cwd: str,
    *,
    continue_session: bool = False,
    orchestrator_root: Path | None = None,
) -> CrossValidateResult:
    """Run CROSS_VALIDATE_PLAN: ask the reviewer to validate the just-produced
    cross-plan.

    Returns :class:`CrossValidateResult`. The reviewer parser is the
    shared :func:`pipeline.review_parser.parse_review`, so a malformed
    JSON contract is treated as a hard rejection — the contract is the
    gate, not prose.

    ``continue_session=True`` resumes the reviewer's prior bridge —
    caller sets it on round 2+ so QA keeps memory of its previous
    findings.

    ``orchestrator_root`` is the workspace root used to resolve
    composable prompt overrides (ADR 0009). It must be supplied
    explicitly by the caller so tests / embedders that override the
    orchestrator's root see their override take effect here too —
    capturing the prompts-module global at planning_loop import time
    would freeze it to whatever was resolved on first import. The
    cross orchestrator passes its own ``_ORCHESTRATOR_ROOT``; in-process
    callers without an override may pass ``None`` to reuse whatever
    the prompts module currently has.
    """
    from core.contracts.review_schema import ReviewSchemaError
    from core.io.stdout_render import defer_assistant_json
    from pipeline.review_markdown import render_review_markdown
    from pipeline.review_parser import ReviewParseError, parse_review

    if orchestrator_root is not None:
        _set_orchestrator_root(orchestrator_root)
    _review_turn = _cross_plan_review_focus_impl(
        task,
        aliases,
        plan_artifact=plan_output,
        plan_artifact_path="cross_plan.md",
    )
    try:
        from core.observability.prompt_trace import set_last_prompt_turn

        set_last_prompt_turn(_review_turn)
        with defer_assistant_json():
            raw = qa_agent.invoke(
                _review_turn.text, cwd, continue_session=continue_session,
            )
    except RuntimeError as e:
        err = f"[validate_cross_plan error: {e}]"
        return CrossValidateResult(
            approved=False,
            critique_markdown=err,
            review_dict={
                "verdict":       "REJECTED",
                "short_summary": err,
                "findings":      [],
                "parse_error":   str(e),
            },
            raw_output="",
            prompt_text=_review_turn.text,
        )
    try:
        parsed = parse_review(raw or "")
    except (ReviewSchemaError, ReviewParseError) as e:
        body = f"validate_cross_plan parse error: {e}\n\nRaw output:\n{raw}"
        return CrossValidateResult(
            approved=False,
            critique_markdown=body,
            review_dict={
                "verdict":       "REJECTED",
                "short_summary": f"validate_cross_plan parse error: {e}",
                "findings":      [],
                "parse_error":   str(e),
            },
            raw_output=raw or "",
            prompt_text=_review_turn.text,
        )
    return CrossValidateResult(
        approved=parsed.approved,
        critique_markdown=render_review_markdown(parsed, title="Cross-plan QA"),
        review_dict={
            "verdict":       parsed.verdict,
            "short_summary": parsed.short_summary,
            "findings":      parsed.findings_as_dicts(),
            "risks":         list(parsed.risks),
            "checks":        list(parsed.checks),
        },
        raw_output=raw or "",
        prompt_text=_review_turn.text,
    )


# ════════════════════════════════════════════════════════════════════════════
#  CROSS PLANNING DRIVER (Phase 4b)
# ════════════════════════════════════════════════════════════════════════════


PlanningStatus = Literal["approved", "paused", "halted", "bypass"]


@dataclass(slots=True)
class CrossPlanningContext:
    """Input bundle for :func:`run_cross_planning`.

    Carries inputs (immutable in spirit) plus three pieces of mutable
    shared state — ``cross_ckpt``, ``session``, ``cross_phase_usage`` —
    that the loop must update in place so the caller observes the same
    side-effects the original inline block produced.
    """
    # Run identity
    task: str
    projects: dict[str, Path]
    aliases: list[str]
    run_dir: Path
    output_dir: Any                    # truthy → persist to disk; mirrors orchestrator's own arg
    common_cwd: str
    dry_run: bool
    resume_from: str | None
    resumed_meta: dict | None
    plan_file_label: str | None        # original --plan-file value for the banner subtitle
    pre_existing_plan_text: str | None # contents read from --plan-file (None when absent / invalid)
    # Agents
    plan_agent: Any                    # claude_plan
    review_agent: Any                  # codex
    # Hypothesis seeds
    cross_hypothesis: Any
    cross_hypothesis_attempts: Any
    # Profile projection results
    has_global_plan: bool
    has_global_validate: bool
    plan_loop_max_rounds: int
    validate_handoff_fires: bool       # global validate step declares human_feedback_on_reject?
    requested_profile_name: str
    orchestrator_root: Path
    # Mutable shared state
    cross_ckpt: dict
    session: dict
    cross_phase_usage: dict[str, dict]

    # ADR 0047 Phase E — presentation policy flag. ``True`` (default)
    # = legacy CLI / SDK terminal output unchanged; ``False`` = banners
    # / chips suppressed but ``log_phase`` writes preserved. Set by the
    # cross app body from ``request.presentation``.
    terminal: bool = True

    # ADR 0055 — in-memory prompt-session store for session-aware delta
    # rendering of the plan agent across plan→replan rounds. Keyed by
    # PhysicalSessionKey; survives across rounds within one
    # ``run_cross_planning`` call so round 2 (cross_replan on a resumed
    # provider session) omits the stable parts round 1 already sent. A
    # cross-process resume starts empty → first invoke renders full.
    prompt_sessions: dict = field(default_factory=dict)


@dataclass(slots=True)
class CrossPlanningResult:
    """Outcome of :func:`run_cross_planning`.

    ``status`` semantics:

    * ``"approved"`` — plan accepted (by reviewer or by operator
      ``continue`` / ``retry_feedback``). Caller writes
      ``session["phases"]["cross_plan"]`` and proceeds to per-project
      dispatch.
    * ``"bypass"`` — profile has no global plan step. Caller skips
      cross-level planning and proceeds straight to dispatch.
    * ``"paused"`` — operator handoff requested. Loop already wrote
      ``session["phases"]["cross_plan"]``, called
      ``apply_cross_phase_handoff_pause`` (which persisted meta +
      checkpoint), and the caller MUST return the session.
    * ``"halted"`` — operator chose halt. Loop already called
      ``finalize_cross_terminal`` and cleared the checkpoint handoff
      markers; the caller MUST return the session.
    """
    status: PlanningStatus
    plan_output: str = ""
    plan_approved: bool = False
    plan_critique: str = ""
    plan_review_dict: dict | None = None
    plan_rounds: list[dict] = field(default_factory=list)
    skipped_phase0: bool = False


def run_cross_planning(ctx: CrossPlanningContext) -> CrossPlanningResult:
    """Drive the cross-plan / cross_validate_plan loop end to end.

    Dispatches in priority order:

    1. No global plan step → ``bypass``.
    2. ``--plan-file`` pre-existing plan → ``approved`` immediately.
    3. Resume with pending cross_plan handoff → operator decision branch
       (``halt`` / ``continue`` / ``retry_feedback``).
    4. Resume with phase0 already done on disk → ``approved`` (cached).
    5. Otherwise → fresh plan loop with the configured retry budget.
    """
    if not ctx.has_global_plan:
        return _bypass(ctx)
    if ctx.pre_existing_plan_text is not None:
        return _approve_pre_existing(ctx)
    if (
        ctx.resume_from
        and ctx.cross_ckpt.get("phase_handoff_pending")
        # Phase A invariant 1 — kind-aware resume routing. Only route
        # cross_plan pauses into this loop. Legacy entries that did
        # not stamp ``phase_handoff_kind`` default to ``"plan"`` (the
        # only kind that existed before ADR 0039 cross parity);
        # ``"project"`` is handled off-band in app.py and ``"cfa"`` is
        # owned by :mod:`pipeline.cross_project.cfa_gate`.
        and ctx.cross_ckpt.get("phase_handoff_kind", "plan") == "plan"
    ):
        return _resume_handoff_decision(ctx)
    if (
        ctx.resume_from
        and ctx.cross_ckpt.get("phase0_done")
        and (ctx.run_dir / "cross_plan.json").exists()
    ):
        return _approve_resume_cached(ctx)
    return _run_initial_loop(ctx)


# ── branch: bypass ──────────────────────────────────────────────────────────


def _bypass(ctx: CrossPlanningContext) -> CrossPlanningResult:
    from pipeline.cross_project.checkpoint import write_cross_checkpoint
    from pipeline.cross_project.rendering import silent_renderers

    # ADR 0047 Phase E — presentation-aware renderers per ctx.terminal.
    (
        banner, success, warn, preview,
        _render_cross_plan_preview, print, _C,  # noqa: A001 — print shadow
    ) = silent_renderers(ctx.terminal)

    success(
        f"Profile {ctx.requested_profile_name!r}: no global plan step "
        "in projection — skipping cross-level planning."
    )
    ctx.cross_ckpt["phase0_done"] = True
    write_cross_checkpoint(ctx.run_dir, ctx.cross_ckpt)
    return CrossPlanningResult(
        status="bypass",
        plan_approved=True,
        skipped_phase0=True,
    )


# ── branch: pre-existing plan via --plan-file ───────────────────────────────


def _approve_pre_existing(ctx: CrossPlanningContext) -> CrossPlanningResult:
    from core.observability import phases as _pk
    from core.observability.logging import log_phase

    # ADR 0047 Phase E — presentation-aware renderers per ctx.terminal.
    # ``silent_renderers`` returns the canonical helpers under TERMINAL
    # and stdout-suppressing shadows under SILENT (banner still calls
    # log_phase — ADR 0046 stop #9 invariant). Destructuring at the
    # top of each function gives Python a single unconditional binding
    # of every name, so there is no UnboundLocalError risk from
    # conditional ``def`` blocks.
    from pipeline.cross_project.rendering import silent_renderers
    (
        banner, success, warn, preview,
        _render_cross_plan_preview, print, C,  # noqa: A001 — print shadow
    ) = silent_renderers(ctx.terminal)

    import json as _json

    from pipeline.cross_project.plan_parser import (
        CrossPlanParseError,
        aliasize_cross_plan,
        parse_cross_plan,
        write_cross_plan_artifacts,
    )

    label = ctx.plan_file_label or "<plan>"
    title = f"CROSS-PLAN — using pre-existing plan from {label}"
    banner("CROSS_PLAN", title, C.MAGENTA, phase_kind=_pk.PLAN, attempt=1)
    print(paint(f"  [cwd] {ctx.common_cwd}", C.GREY))
    print(paint(f"  [run dir] {ctx.run_dir}", C.GREY))
    # ADR 0054: --plan-file is JSON-only. Validate the operator-supplied
    # cross-plan JSON, then persist the canonical cross_plan.json + a
    # derived cross_plan.md. A markdown plan file (or any invalid JSON) is
    # rejected loudly rather than dispatched as-is.
    raw = ctx.pre_existing_plan_text or ""
    try:
        result = parse_cross_plan(raw, list(ctx.aliases))
    except CrossPlanParseError as exc:
        warn(f"--plan-file is not a valid cross-plan JSON object: {exc}")
        raise
    if ctx.run_dir is not None:
        result, _document = write_cross_plan_artifacts(
            ctx.run_dir, result,
            task=ctx.task, projects=ctx.projects, aliases=list(ctx.aliases),
        )
    else:
        result = aliasize_cross_plan(result, ctx.projects, list(ctx.aliases))
    plan_output = _json.dumps(result.data, ensure_ascii=False)
    log_phase(
        "CROSS_PLAN", title, "DONE",
        f"{len(plan_output)} chars",
        phase_kind=_pk.PLAN, attempt=1,
    )
    return CrossPlanningResult(
        status="approved",
        plan_output=plan_output,
        plan_approved=True,
    )


# ── branch: resume after phase0 already cleared ─────────────────────────────


def _approve_resume_cached(ctx: CrossPlanningContext) -> CrossPlanningResult:
    from core.observability import phases as _pk
    from core.observability.logging import log_phase

    # ADR 0047 Phase E — presentation-aware renderers per ctx.terminal.
    # ``silent_renderers`` returns the canonical helpers under TERMINAL
    # and stdout-suppressing shadows under SILENT (banner still calls
    # log_phase — ADR 0046 stop #9 invariant). Destructuring at the
    # top of each function gives Python a single unconditional binding
    # of every name, so there is no UnboundLocalError risk from
    # conditional ``def`` blocks.
    from pipeline.cross_project.rendering import silent_renderers
    (
        banner, success, warn, preview,
        _render_cross_plan_preview, print, C,  # noqa: A001 — print shadow
    ) = silent_renderers(ctx.terminal)

    title = "CROSS-PLAN — skipped (resume, plan from previous run)"
    banner("CROSS_PLAN", title, C.MAGENTA, phase_kind=_pk.PLAN, attempt=1)
    print(paint(f"  [cwd] {ctx.common_cwd}", C.GREY))
    print(paint(f"  [run dir] {ctx.run_dir}", C.GREY))
    # ADR 0054: cross_plan.json is the canonical normalized plan. Resume
    # reloads it (NOT cross_plan.md — the .md is a derived audit render and
    # is not valid input to parse_cross_plan). Re-validate at the boundary
    # so a missing/corrupt cached plan refuses loudly rather than degrading
    # into dispatch.
    from pipeline.cross_project.plan_parser import (
        CrossPlanParseError,
        parse_cross_plan,
    )
    plan_output = (ctx.run_dir / "cross_plan.json").read_text(encoding="utf-8")
    try:
        parse_cross_plan(plan_output, list(ctx.aliases))
    except CrossPlanParseError as exc:
        raise RuntimeError(
            f"Cannot resume cross run {ctx.run_dir.name!r}: cached "
            f"cross_plan.json is not a valid cross plan ({exc})."
        ) from exc
    log_phase(
        "CROSS_PLAN", title, "DONE",
        f"{len(plan_output)} chars (cached)",
        phase_kind=_pk.PLAN, attempt=1,
    )
    return CrossPlanningResult(
        status="approved",
        plan_output=plan_output,
        plan_approved=True,
        skipped_phase0=True,
    )


# ── branch: resume after operator handoff decision ─────────────────────────


def _resume_handoff_decision(ctx: CrossPlanningContext) -> CrossPlanningResult:
    """Apply the operator's decision on a pending cross_plan handoff.

    Loads the decision artifact, then dispatches to halt / continue /
    retry_feedback. The retry_feedback branch runs exactly one extra
    plan + validate round on top of the configured ``loop_max_rounds``
    budget and may re-pause if QA rejects again.
    """
    from core.observability import phases as _pk
    from core.observability.logging import log_phase
    from pipeline.control import (
        HandoffDecisionContext,
        load_handoff_decision,
    )
    from pipeline.cross_project.checkpoint import write_cross_checkpoint
    from pipeline.cross_project.handoff_payloads import (
        CROSS_PLAN_ROUND_KEY,
        parse_cross_handoff_round,
    )

    # ADR 0047 Phase E — presentation-aware renderers per ctx.terminal.
    # ``silent_renderers`` returns the canonical helpers under TERMINAL
    # and stdout-suppressing shadows under SILENT (banner still calls
    # log_phase — ADR 0046 stop #9 invariant). Destructuring at the
    # top of each function gives Python a single unconditional binding
    # of every name, so there is no UnboundLocalError risk from
    # conditional ``def`` blocks.
    from pipeline.cross_project.rendering import silent_renderers
    from pipeline.cross_project.terminal import finalize_cross_terminal
    from pipeline.run_state.terminal import evict_cross_handoff_markers
    (
        banner, success, warn, preview,
        _render_cross_plan_preview, print, C,  # noqa: A001 — print shadow
    ) = silent_renderers(ctx.terminal)

    handoff_id = ctx.cross_ckpt.get("phase_handoff_id") or (
        f"cross_plan:{CROSS_PLAN_ROUND_KEY}:{ctx.plan_loop_max_rounds}"
    )
    decision = load_handoff_decision(
        HandoffDecisionContext(
            run_id=ctx.run_dir.name,
            handoff_id=handoff_id,
            runs_dir=ctx.run_dir.parent,
            cwd=None,
            missing_message=(
                f"Cannot resume cross run {ctx.run_dir.name!r}: cross "
                f"checkpoint flags handoff {handoff_id!r} as pending "
                "but no decision artifact found under "
                "phase_handoff_decisions/. Call "
                "orcho_phase_handoff_decide before orcho_run_resume."
            ),
            invalid_message_prefix=(
                f"Cannot resume cross run {ctx.run_dir.name!r}: decision "
                f"artifact for handoff {handoff_id!r} failed strict "
                "validation"
            ),
        ),
    )
    action = decision.action
    feedback = decision.feedback
    banner(
        "CROSS_PLAN",
        f"CROSS-PLAN — resume with handoff decision: {action!r}",
        C.MAGENTA, phase_kind=_pk.PLAN, attempt=ctx.plan_loop_max_rounds,
    )

    # Hydrate prior rejection rounds from the persisted session so the
    # tail meta writer in the orchestrator does not overwrite ``rounds``
    # with the fresh-empty list when we return ``approved``.
    plan_rounds: list[dict] = []
    prior_rounds = (
        ctx.session.get("phases", {})
                   .get("cross_plan", {})
                   .get("rounds")
        or []
    )
    if isinstance(prior_rounds, list):
        plan_rounds.extend(prior_rounds)

    active_handoff = (ctx.resumed_meta or {}).get("phase_handoff") or {}
    meta_round = active_handoff.get("round")
    if isinstance(meta_round, int):
        paused_round = meta_round
    else:
        paused_round = parse_cross_handoff_round(
            handoff_id, ctx.plan_loop_max_rounds,
        )

    # Branch on the narrow Literal returned by the decision engine.
    # ``_narrow_action`` in the engine accepts four values; cross_plan
    # handoffs publish only continue / retry_feedback / halt (see
    # build_cross_plan_handoff_payload — ``continue_with_waiver`` is a
    # single-project-only action per ADR 0072), so the fourth value is
    # rejected loudly below rather than silently mis-routed.
    if action == "halt":
        finalize_cross_terminal(
            run_dir=ctx.run_dir if ctx.output_dir else None,
            session=ctx.session,
            status="halted",
            halt_reason="phase_handoff_halt",
            cross_ckpt=ctx.cross_ckpt,
        )
        evict_cross_handoff_markers(ctx.cross_ckpt)
        write_cross_checkpoint(ctx.run_dir, ctx.cross_ckpt)
        success("Cross run halted by operator")
        return CrossPlanningResult(status="halted", plan_rounds=plan_rounds)

    if action == "continue":
        # ADR 0054 stale-plan guard: continue dispatches the canonical
        # cross_plan.json (the latest schema-VALID plan, persisted pre-QA),
        # NOT cross_plan.md (a derived audit render, not valid parser
        # input). ``can_continue`` already withholds ``continue`` from the
        # operator when the paused round was schema-invalid; re-validate
        # here defensively so a missing/corrupt artifact refuses rather
        # than silently dispatching a stale or unparseable plan.
        from pipeline.cross_project.plan_parser import (
            CrossPlanParseError,
            parse_cross_plan,
        )
        plan_path = ctx.run_dir / "cross_plan.json"
        if not plan_path.is_file():
            raise RuntimeError(
                f"Cannot resume cross run {ctx.run_dir.name!r}: 'continue' "
                "action requires cross_plan.json to exist next to "
                "meta.json, but the file is missing."
            )
        plan_output = plan_path.read_text(encoding="utf-8")
        try:
            parse_cross_plan(plan_output, list(ctx.aliases))
        except CrossPlanParseError as exc:
            raise RuntimeError(
                f"Cannot continue cross run {ctx.run_dir.name!r}: "
                f"cross_plan.json is not a valid cross plan ({exc}). Use "
                "retry_feedback to regenerate the plan."
            ) from exc
        evict_cross_handoff_markers(ctx.cross_ckpt)
        # phase0_done is a positive progress marker (cross planning is now
        # accepted), NOT handoff residue — set it separately after the
        # handoff-marker eviction.
        ctx.cross_ckpt["phase0_done"] = True
        write_cross_checkpoint(ctx.run_dir, ctx.cross_ckpt)
        log_phase(
            "CROSS_PLAN",
            "CROSS-PLAN — resume continue (operator override)",
            "DONE", f"{len(plan_output)} chars (cached)",
            phase_kind=_pk.PLAN, attempt=ctx.plan_loop_max_rounds,
        )
        return CrossPlanningResult(
            status="approved",
            plan_output=plan_output,
            plan_approved=True,
            skipped_phase0=True,
            plan_rounds=plan_rounds,
        )

    if action == "retry_feedback":
        return _retry_feedback_round(
            ctx,
            feedback=feedback,
            paused_round=paused_round,
            plan_rounds=plan_rounds,
        )

    # ``continue_with_waiver`` (ADR 0072) is single-project only: the
    # cross_plan producer never offers it, so the SDK decide gate refuses
    # it upstream. Guard here too — the shared HandoffDecisionAction Literal
    # now carries four values, so this point is no longer exhaustive by
    # narrowing alone. Fail loudly rather than mis-routing to retry.
    raise RuntimeError(
        f"Cross-plan resume does not support action {action!r}; cross_plan "
        "handoffs offer only continue / retry_feedback / halt."
    )


def _invalid_cross_plan_critique(parse_err: str) -> str:
    """Synthetic-reject critique for an unparseable cross plan (ADR 0054).

    Embeds the schema error + a JSON-schema reminder so the next replan
    round re-emits a valid object even when the session-aware delta has
    dropped the contract part from the prompt.
    """
    return (
        "Your cross-plan output was not a valid cross-plan JSON object: "
        f"{parse_err}\n\n"
        "Re-emit exactly ONE JSON object matching the cross-plan schema: "
        "short_summary, interface_contract, implementation_order, and "
        "subtasks[] with exactly one entry per supplied alias (each with "
        "alias/goal/spec, optional depends_on referencing declared aliases "
        "without cycles). No prose, markdown fences, or trailing commentary."
    )


def _invalid_cross_plan_review(parse_err: str) -> dict:
    """Synthetic review dict for an unparseable cross plan."""
    return {
        "verdict":       "REJECTED",
        "short_summary": "cross-plan output was not valid JSON",
        "findings":      [],
        "parse_error":   parse_err,
    }


def _render_cross_validate_findings(review_dict, print_fn) -> None:
    """Render the cross-plan reviewer's findings block.

    Shared by the automatic validate rounds and the operator-retry rounds so the
    operator always sees WHY a plan was rejected at the continue/retry/halt
    handoff — instead of only a terse "also rejected" that forced digging the
    critique out of ``events.jsonl``. No-op when there is no review dict.
    """
    if not review_dict:
        return
    from core.io.transcript import render_review_block
    print_fn(render_review_block(review_dict, title="Cross-plan validation"))


def _retry_feedback_round(
    ctx: CrossPlanningContext,
    *,
    feedback: str,
    paused_round: int,
    plan_rounds: list[dict],
) -> CrossPlanningResult:
    """One human-directed plan→validate cycle with operator feedback."""
    from core.observability import events as _events, phases as _pk
    from core.observability.logging import log_phase
    from core.observability.trace import vdump, vtimed
    from pipeline.cross_project.checkpoint import write_cross_checkpoint
    from pipeline.cross_project.handoff_payloads import (
        apply_cross_phase_handoff_pause,
        build_cross_plan_handoff_payload,
    )
    from pipeline.cross_project.orchestrator import cross_replan_prompt

    # ADR 0047 Phase E — presentation-aware renderers per ctx.terminal.
    # ``silent_renderers`` returns the canonical helpers under TERMINAL
    # and stdout-suppressing shadows under SILENT (banner still calls
    # log_phase — ADR 0046 stop #9 invariant). Destructuring at the
    # top of each function gives Python a single unconditional binding
    # of every name, so there is no UnboundLocalError risk from
    # conditional ``def`` blocks.
    from pipeline.cross_project.rendering import silent_renderers

    # Render helpers come from ``rendering``; the terminal-aware usage
    # wrappers ``_capture_invoke_usage`` / ``_print_usage_snapshot`` and
    # the underlying ``accumulate_phase_usage`` all live in their
    # canonical leaf home ``usage`` (Stage 2). Importing them from the
    # leaf — not from ``app`` — keeps planning_loop free of a back-import
    # into the typed boundary (no ``app → planning_loop → app`` cycle).
    from pipeline.cross_project.usage import (
        _capture_invoke_usage,
        _print_usage_snapshot,
        accumulate_phase_usage as _accumulate_phase_usage,
    )
    from pipeline.run_state.terminal import evict_cross_handoff_markers
    (
        banner, success, warn, preview,
        _render_cross_plan_preview, print, C,  # noqa: A001 — print shadow
    ) = silent_renderers(ctx.terminal)

    retry_round = paused_round + 1
    banner(
        "CROSS_PLAN",
        f"CROSS-PLAN Retry round {retry_round} (operator feedback)",
        C.MAGENTA, phase_kind=_pk.PLAN, attempt=retry_round,
    )
    print(paint(f"  [cwd] {ctx.common_cwd}", C.GREY))
    print(paint(f"  [run dir] {ctx.run_dir}", C.GREY))
    retry_turn = cross_replan_prompt(
        ctx.task, feedback, ctx.projects, ctx.run_dir,
    )
    vdump("CROSS_PLAN", f"prompt-resume-retry-{retry_round}", retry_turn.text)

    cp_t0 = time.time()
    with vtimed("CROSS_PLAN", f"cross-plan retry {retry_round}"):
        # ADR 0055 — same session-aware delta path. On a cross-process
        # resume the in-memory store is empty, so this first post-restart
        # invoke renders full (parity with mono's cold-session behaviour).
        plan_output = (
            "[DRY RUN]" if ctx.dry_run
            else session_aware_invoke(
                ctx.plan_agent,
                prompt_sessions=ctx.prompt_sessions,
                run_id=ctx.run_dir.name,
                phase="cross_plan",
                turn=retry_turn,
                cwd=ctx.common_cwd,
                continue_session=_cross_continue_session(
                    SessionInvocationRole.PLAN
                ),
            )
        )
    if not ctx.dry_run:
        cp_usage = _capture_invoke_usage(
            ctx.plan_agent, time.time() - cp_t0,
            prompt=retry_turn.text, output=plan_output,
            model=getattr(ctx.plan_agent, "model", None),
            terminal=ctx.terminal,
        )
        _accumulate_phase_usage(ctx.cross_phase_usage, "cross_plan", cp_usage)
        _print_usage_snapshot(
            f"cross_plan retry={retry_round}", cp_usage,
            terminal=ctx.terminal,
        )
    raw_retry = plan_output
    _render_cross_plan_preview(raw_retry, list(ctx.aliases))

    # ADR 0054 — parse the retry plan. Valid → persist canonical
    # cross_plan.json + derived cross_plan.md and the reviewer sees the
    # render. Invalid → synthetic reject (no reviewer); cross_plan.json is
    # NOT refreshed (latest-valid-wins).
    parsed_retry = None
    parse_err: str | None = None
    plan_document = ""
    normalized_output = raw_retry
    if not ctx.dry_run:
        import json as _json

        from pipeline.cross_project.plan_parser import (
            CrossPlanParseError,
            parse_cross_plan,
            write_cross_plan_artifacts,
        )
        try:
            parsed_retry = parse_cross_plan(raw_retry, list(ctx.aliases))
        except CrossPlanParseError as exc:
            parse_err = str(exc)
            vdump(
                "CROSS_PLAN",
                f"plan-resume-retry-{retry_round}-parse-error", parse_err,
            )
        else:
            parsed_retry, plan_document = write_cross_plan_artifacts(
                ctx.run_dir, parsed_retry,
                task=ctx.task, projects=ctx.projects, aliases=list(ctx.aliases),
            )
            normalized_output = _json.dumps(parsed_retry.data, ensure_ascii=False)
            vdump("CROSS_PLAN", f"plan-resume-retry-{retry_round}", plan_document)

    banner(
        "CROSS_VALIDATE_PLAN",
        f"CROSS_VALIDATE_PLAN Retry round {retry_round}",
        C.YELLOW, phase_kind=_pk.VALIDATE_PLAN, attempt=retry_round,
    )
    if ctx.dry_run:
        plan_approved, plan_critique, plan_review_dict = (
            _dry_run_validate_outcome()
        )
    elif parse_err is not None:
        plan_approved = False
        plan_critique = _invalid_cross_plan_critique(parse_err)
        plan_review_dict = _invalid_cross_plan_review(parse_err)
        warn(f"Operator retry round {retry_round} rejected: invalid plan JSON")
    else:
        cv_t0 = time.time()
        cv_result = validate_cross_plan(
            ctx.review_agent, plan_document, ctx.task, ctx.aliases, ctx.common_cwd,
            continue_session=_cross_continue_session(
                SessionInvocationRole.REVIEW
            ),
            orchestrator_root=ctx.orchestrator_root,
        )
        cv_usage = _capture_invoke_usage(
            ctx.review_agent, time.time() - cv_t0,
            prompt=cv_result.prompt_text,
            output=cv_result.raw_output,
            model=getattr(ctx.review_agent, "model", None),
            terminal=ctx.terminal,
        )
        _accumulate_phase_usage(
            ctx.cross_phase_usage, "cross_validate_plan", cv_usage,
        )
        _print_usage_snapshot(
            f"cross_validate_plan retry={retry_round}", cv_usage,
            terminal=ctx.terminal,
        )
        plan_approved = cv_result.approved
        plan_critique = cv_result.critique_markdown
        plan_review_dict = cv_result.review_dict
    plan_rounds.append({
        "round":             retry_round,
        "plan":              (plan_document if parsed_retry is not None else raw_retry),
        "approved":          plan_approved,
        "critique":          plan_critique,
        "review":            plan_review_dict,
        "trigger":           "operator_retry_feedback",
        "raw_output":        raw_retry,
        "normalized_plan":   (parsed_retry.data if parsed_retry is not None else None),
        "rendered_markdown": plan_document,
        "parse_error":       parse_err,
        "parse_warnings":    (list(parsed_retry.parse_warnings) if parsed_retry is not None else []),
    })
    _events.emit(
        "cross_validate_plan.verdict",
        attempt=retry_round,
        approved=plan_approved,
        critique=(plan_critique or "")[:2000],
    )
    # Render the reviewer's findings on operator-retry rounds too — historically
    # the retry path only ``warn``-ed "also rejected", so the operator faced the
    # continue/retry/halt handoff with no findings. Shared helper = same render as
    # the automatic ``_validate`` path (asymmetry removed by construction).
    _render_cross_validate_findings(plan_review_dict, print)
    log_phase(
        "CROSS_VALIDATE_PLAN",
        f"CROSS_VALIDATE_PLAN Retry round {retry_round}",
        "END",
        ("approved" if plan_approved else "rejected"),
        phase_kind=_pk.VALIDATE_PLAN, attempt=retry_round,
    )

    if plan_approved:
        success(
            f"Cross-plan approved on operator retry round {retry_round}"
        )
        evict_cross_handoff_markers(ctx.cross_ckpt)
        # phase0_done is a positive progress marker (cross planning is now
        # accepted), NOT handoff residue — set it separately after the
        # handoff-marker eviction.
        ctx.cross_ckpt["phase0_done"] = True
        write_cross_checkpoint(ctx.run_dir, ctx.cross_ckpt)
        return CrossPlanningResult(
            status="approved",
            plan_output=normalized_output,
            plan_approved=True,
            plan_critique=plan_critique,
            plan_review_dict=plan_review_dict,
            plan_rounds=plan_rounds,
        )

    # Retry rejected — re-pause with new round number. Operator keeps
    # decision authority indefinitely (mirrors single-run's open-ended
    # human-directed budget). ``continue`` is withheld when this retry
    # round did not parse schema-valid (no fresh cross_plan.json for it).
    warn(f"Operator retry round {retry_round} also rejected")
    handoff_payload = build_cross_plan_handoff_payload(
        round_n=retry_round,
        max_rounds=ctx.plan_loop_max_rounds,
        plan_review_dict=plan_review_dict,
        plan_output=(plan_document or raw_retry),
        can_continue=parsed_retry is not None,
    )
    # Populate the round trace BEFORE the pause helper saves the session,
    # otherwise the persisted meta.json drops the rejection round while
    # the in-memory return value carries it. ADR 0038 review fix.
    ctx.session["phases"]["cross_plan"] = {
        "output":   normalized_output,
        "run_dir":  str(ctx.run_dir) if ctx.run_dir else "",
        "rounds":   plan_rounds,
        "approved": False,
    }
    apply_cross_phase_handoff_pause(
        run_dir=ctx.run_dir if ctx.output_dir else None,
        session=ctx.session,
        cross_ckpt=ctx.cross_ckpt,
        payload=handoff_payload,
        cross_phase_usage=ctx.cross_phase_usage,
        terminal=ctx.terminal,
    )
    return CrossPlanningResult(
        status="paused",
        plan_output=normalized_output,
        plan_approved=False,
        plan_critique=plan_critique,
        plan_review_dict=plan_review_dict,
        plan_rounds=plan_rounds,
    )


# ── branch: fresh plan/validate loop ────────────────────────────────────────


def _run_initial_loop(ctx: CrossPlanningContext) -> CrossPlanningResult:
    """Iterate plan→validate rounds up to ``plan_loop_max_rounds``.

    Approves on first clean QA verdict. On all-rejected: if the
    projected validate step declares ``human_feedback_on_reject``,
    pauses for operator decision; otherwise emits the legacy
    "proceed-with-rejected-plan" warning (bypass profile policy).

    Phase D (ADR 0040): the round-budget + retry control flow lives in
    :func:`pipeline.control.run_reviewed_loop`. The cross-domain side
    effects (banners, ``vdump``, persistence of ``cross_plan.md``,
    validate-verdict event + review-block render + ``log_phase`` END,
    per-round ``success`` / ``warn`` lines) all stay inside the
    ``_produce`` / ``_validate`` closures supplied to the primitive —
    none of that vocabulary leaks into ``ReviewedLoopPolicy``.
    """
    from core.observability import events as _events, phases as _pk
    from core.observability.logging import log_phase
    from core.observability.trace import vdump
    from pipeline.control import (
        ReviewedLoopPolicy,
        ReviewOutcome,
        run_reviewed_loop,
    )
    from pipeline.cross_project.handoff_payloads import (
        apply_cross_phase_handoff_pause,
        build_cross_plan_handoff_payload,
    )

    # ADR 0047 Phase E — presentation-aware renderers per ctx.terminal.
    # ``silent_renderers`` returns the canonical helpers under TERMINAL
    # and stdout-suppressing shadows under SILENT (banner still calls
    # log_phase — ADR 0046 stop #9 invariant). Destructuring at the
    # top of each function gives Python a single unconditional binding
    # of every name, so there is no UnboundLocalError risk from
    # conditional ``def`` blocks.
    from pipeline.cross_project.rendering import silent_renderers
    (
        banner, success, warn, preview,
        _render_cross_plan_preview, print, C,  # noqa: A001 — print shadow
    ) = silent_renderers(ctx.terminal)

    import json

    from pipeline.cross_project.plan_parser import (
        CrossPlanParse,
        CrossPlanParseError,
        parse_cross_plan,
        write_cross_plan_artifacts,
    )

    # ADR 0054 round stashes — keyed by round number. ``_produce`` writes;
    # the post-loop trace builder + the pause/approve handlers read.
    parsed_by_round: dict[int, CrossPlanParse] = {}
    parse_error_by_round: dict[int, str] = {}
    raw_by_round: dict[int, str] = {}

    def _produce(round_n: int, is_retry: bool, prior_critique: str) -> str:
        banner(
            "CROSS_PLAN",
            f"CROSS-PLAN -- Round {round_n}/{ctx.plan_loop_max_rounds}",
            C.MAGENTA, phase_kind=_pk.PLAN, attempt=round_n,
        )
        print(paint(f"  [cwd] {ctx.common_cwd}", C.GREY))
        print(paint(f"  [run dir] {ctx.run_dir}", C.GREY))

        plan_output = _invoke_plan_round(
            ctx, round_n, prior_critique,
        )
        if ctx.dry_run:
            # No agent JSON to parse/persist on a dry run.
            return plan_output

        raw_by_round[round_n] = plan_output
        _render_cross_plan_preview(plan_output, list(ctx.aliases))
        try:
            result = parse_cross_plan(plan_output, list(ctx.aliases))
        except CrossPlanParseError as exc:
            # Parse failure never raises out of the loop (ADR 0054): stash
            # the schema error so ``_validate`` turns it into a synthetic
            # reject with a JSON-schema reminder. cross_plan.json is NOT
            # refreshed — an invalid round must not overwrite the latest
            # schema-valid plan (latest-valid-wins).
            parse_error_by_round[round_n] = str(exc)
            vdump("CROSS_PLAN", f"plan-round-{round_n}-parse-error", str(exc))
            return plan_output

        # Persist the latest schema-VALID plan before QA: the normalized
        # JSON is canonical (cross_plan.json, the runtime source of truth);
        # cross_plan.md is the derived audit render. ``write_cross_plan_artifacts``
        # aliasizes the data (leak-clean canonical object), writes both files,
        # and returns the full document so the reviewer artifact / on-disk md /
        # round-trace all match. A crash or an operator ``continue`` on a
        # QA-rejected-but-valid plan finds the right one.
        result, document = write_cross_plan_artifacts(
            ctx.run_dir, result,
            task=ctx.task, projects=ctx.projects, aliases=list(ctx.aliases),
        )
        parsed_by_round[round_n] = result
        vdump("CROSS_PLAN", f"plan-round-{round_n}", document)
        return document

    def _validate(
        round_n: int, is_retry: bool, plan_output: str,
    ) -> ReviewOutcome:
        # ADR 0054: a round whose JSON did not parse/validate becomes a
        # SYNTHETIC reject — the reviewer is never invoked (there is no
        # plan to review). The critique carries the schema error plus a
        # JSON-schema reminder so the replan round re-emits a valid object
        # (robust against the session-aware-delta dropping the contract).
        parse_err = parse_error_by_round.get(round_n)
        if parse_err is not None:
            critique = _invalid_cross_plan_critique(parse_err)
            review_dict = _invalid_cross_plan_review(parse_err)
            warn(f"Cross-plan round {round_n} rejected: invalid plan JSON")
            _events.emit(
                "cross_validate_plan.verdict",
                attempt=round_n, approved=False, critique=critique[:2000],
            )
            log_phase(
                "CROSS_VALIDATE_PLAN",
                f"CROSS_VALIDATE_PLAN -- Round "
                f"{round_n}/{ctx.plan_loop_max_rounds}",
                "END", "rejected",
                phase_kind=_pk.VALIDATE_PLAN, attempt=round_n,
            )
            return ReviewOutcome(
                approved=False, critique=critique, review=review_dict,
            )

        if not ctx.has_global_validate:
            # Profile has no global validate step (e.g. ``lite``).
            # Single plan invocation is canonical; the loop auto-
            # approves so no QA budget is consumed.
            success("Cross-plan produced (profile has no validate gate)")
            return ReviewOutcome(approved=True, critique="", review=None)

        banner(
            "CROSS_VALIDATE_PLAN",
            f"CROSS_VALIDATE_PLAN -- Round "
            f"{round_n}/{ctx.plan_loop_max_rounds}",
            C.YELLOW, phase_kind=_pk.VALIDATE_PLAN, attempt=round_n,
        )
        approved, critique, review_dict = _invoke_validate_round(
            ctx, round_n, plan_output,
        )
        _events.emit(
            "cross_validate_plan.verdict",
            attempt=round_n,
            approved=approved,
            critique=(critique or "")[:2000],
        )
        _render_cross_validate_findings(review_dict, print)
        log_phase(
            "CROSS_VALIDATE_PLAN",
            f"CROSS_VALIDATE_PLAN -- Round "
            f"{round_n}/{ctx.plan_loop_max_rounds}",
            "END",
            ("approved" if approved else "rejected"),
            phase_kind=_pk.VALIDATE_PLAN, attempt=round_n,
        )
        if approved:
            success(f"Cross-plan approved on round {round_n}")
        else:
            warn(f"Cross-plan round {round_n} rejected by QA")
        return ReviewOutcome(
            approved=approved, critique=critique, review=review_dict,
        )

    policy = ReviewedLoopPolicy(
        max_rounds=ctx.plan_loop_max_rounds,
        pause_on_exhausted_reject=ctx.validate_handoff_fires,
        bypass_on_exhausted_reject=not ctx.validate_handoff_fires,
    )
    result = run_reviewed_loop(
        policy=policy,
        produce=_produce,
        validate=_validate,
    )

    def _round_entry(r) -> dict:
        # Pinned round-trace shape (ADR 0054): always carries raw_output,
        # normalized_plan (None on invalid), rendered_markdown ("" on
        # invalid), parse_error (None on valid), parse_warnings.
        n = r.round_n
        cpp = parsed_by_round.get(n)
        return {
            "round":             n,
            "plan":              r.output,
            "approved":          r.approved,
            "critique":          r.critique,
            "review":            r.review,
            "raw_output":        raw_by_round.get(n, r.output),
            "normalized_plan":   (cpp.data if cpp is not None else None),
            "rendered_markdown": (r.output if n in parsed_by_round else ""),
            "parse_error":       parse_error_by_round.get(n),
            "parse_warnings":    (list(cpp.parse_warnings) if cpp is not None else []),
        }

    plan_rounds: list[dict] = [_round_entry(r) for r in result.rounds]

    def _approved_plan_output() -> str:
        # The approved round's NORMALIZED plan is the canonical channel
        # downstream dispatch re-parses (cross_plan.json source of truth).
        # Dry runs perform no parse → fall back to the raw loop output.
        if result.rounds:
            cpp = parsed_by_round.get(result.rounds[-1].round_n)
            if cpp is not None:
                return json.dumps(cpp.data, ensure_ascii=False)
        return result.last_output

    if result.status == "approved":
        return CrossPlanningResult(
            status="approved",
            plan_output=_approved_plan_output(),
            plan_approved=True,
            plan_critique=result.last_critique,
            plan_review_dict=result.last_review,
            plan_rounds=plan_rounds,
        )

    if result.status == "exhausted_pause":
        # ADR 0038: every round in the budget was rejected and the
        # projected validate step declared human_feedback_on_reject.
        # Build the payload + populate the session round trace BEFORE
        # the pause helper persists meta.json (otherwise the persisted
        # artifact drops the rejection rounds while the in-memory
        # return value carries them — asymmetric post-mortem surface).
        # ADR 0054 invariant: ``continue`` is offered only when the PAUSED
        # round itself parsed schema-valid (so cross_plan.json holds THIS
        # round's plan). A schema-invalid final round leaves an older valid
        # cross_plan.json on disk; continuing it would dispatch a plan older
        # than the one just rejected — so narrow to [retry_feedback, halt].
        can_continue = ctx.plan_loop_max_rounds in parsed_by_round
        handoff_payload = build_cross_plan_handoff_payload(
            round_n=ctx.plan_loop_max_rounds,
            max_rounds=ctx.plan_loop_max_rounds,
            plan_review_dict=result.last_review,
            plan_output=result.last_output,
            can_continue=can_continue,
        )
        ctx.session["phases"]["cross_plan"] = {
            "output":   result.last_output,
            "run_dir":  str(ctx.run_dir) if ctx.run_dir else "",
            "rounds":   plan_rounds,
            "approved": False,
        }
        apply_cross_phase_handoff_pause(
            run_dir=ctx.run_dir if ctx.output_dir else None,
            session=ctx.session,
            cross_ckpt=ctx.cross_ckpt,
            payload=handoff_payload,
            cross_phase_usage=ctx.cross_phase_usage,
            terminal=ctx.terminal,
        )
        return CrossPlanningResult(
            status="paused",
            plan_output=result.last_output,
            plan_approved=False,
            plan_critique=result.last_critique,
            plan_review_dict=result.last_review,
            plan_rounds=plan_rounds,
        )

    # status == "exhausted_bypass" — profile declared human_bypass on
    # cross_validate_plan. Surface the legacy proceed-with-rejected-plan
    # warning and let dispatch consume the last rejected plan.
    warn(
        f"All {ctx.plan_loop_max_rounds} cross-plan rounds rejected — "
        "proceeding with last plan (profile declares bypass handoff)"
    )
    return CrossPlanningResult(
        status="approved",
        plan_output=result.last_output,
        plan_approved=False,
        plan_critique=result.last_critique,
        plan_review_dict=result.last_review,
        plan_rounds=plan_rounds,
    )


def _invoke_plan_round(
    ctx: CrossPlanningContext,
    round_idx: int,
    prior_critique: str,
) -> str:
    """One cross_plan handler invocation.

    ADR 0113: cross_plan / cross_replan are FRESH every round (the ``plan``
    role is non-edit-shaped); the replan handoff (prior critique) rides
    ``cross_replan_prompt``, not a resumed session — so there is no ``resume``
    input any more.
    """
    from core.observability.trace import vdump, vtimed

    # Usage wrappers ``_capture_invoke_usage`` / ``_print_usage_snapshot``
    # live in their canonical leaf home ``usage`` (Stage 2); prompt
    # helpers stay at ``orchestrator`` (public surface); render helper
    # ``success`` from ``rendering``. Importing usage from the leaf keeps
    # planning_loop free of an ``app → planning_loop → app`` back-import.
    from pipeline.cross_project.orchestrator import (
        cross_plan_prompt,
        cross_replan_prompt,
    )

    # ADR 0047 Phase E — presentation-aware renderers per ctx.terminal.
    # ``silent_renderers`` returns the canonical helpers under TERMINAL
    # and stdout-suppressing shadows under SILENT (banner still calls
    # log_phase — ADR 0046 stop #9 invariant). Destructuring at the
    # top of each function gives Python a single unconditional binding
    # of every name, so there is no UnboundLocalError risk from
    # conditional ``def`` blocks.
    from pipeline.cross_project.rendering import silent_renderers
    from pipeline.cross_project.usage import (
        _capture_invoke_usage,
        _print_usage_snapshot,
        accumulate_phase_usage as _accumulate_phase_usage,
    )
    (
        banner, success, warn, preview,
        _render_cross_plan_preview, print, C,  # noqa: A001 — print shadow
    ) = silent_renderers(ctx.terminal)

    if round_idx == 1:
        from pipeline.prompts.turn import (
            PromptTurnEditor,
            hypothesis_suffix_part as _hyp_part,
        )
        base_turn = cross_plan_prompt(ctx.task, ctx.projects, ctx.run_dir)
        if ctx.cross_hypothesis:
            from pipeline.engine.hypothesis import (
                format_validated_hypothesis_context,
            )
            hyp_text = format_validated_hypothesis_context(ctx.cross_hypothesis)
            base_turn = PromptTurnEditor(base_turn).append(_hyp_part(hyp_text)).build()
            success(
                "Planning with validated hypothesis context "
                "(incorporate, falsify, or explain divergence)"
            )
        elif ctx.cross_hypothesis_attempts:
            from pipeline.engine.hypothesis import (
                format_rejected_hypothesis_feedback,
            )
            hyp_text = format_rejected_hypothesis_feedback(ctx.cross_hypothesis_attempts)
            base_turn = PromptTurnEditor(base_turn).append(_hyp_part(hyp_text)).build()
            success(
                "Planning with rejected hypothesis feedback "
                "(not validated direction)"
            )
        turn = base_turn
    else:
        turn = cross_replan_prompt(
            ctx.task, prior_critique, ctx.projects, ctx.run_dir,
        )
    vdump("CROSS_PLAN", f"prompt-round-{round_idx}", turn.text)
    cp_t0 = time.time()
    with vtimed("CROSS_PLAN", f"cross-plan round {round_idx}"):
        # ADR 0113 — route through the policy: cross_plan/cross_replan are
        # FRESH every round (non-edit-shaped ``plan`` role). The session-aware
        # helper still keys ``phase="cross_plan"`` so the trace is coherent,
        # but each round renders full (a fresh provider session) and the
        # replan handoff rides ``cross_replan_prompt``.
        out = (
            "[DRY RUN]" if ctx.dry_run
            else session_aware_invoke(
                ctx.plan_agent,
                prompt_sessions=ctx.prompt_sessions,
                run_id=ctx.run_dir.name,
                phase="cross_plan",
                turn=turn,
                cwd=ctx.common_cwd,
                continue_session=_cross_continue_session(
                    SessionInvocationRole.PLAN
                ),
            )
        )
    if not ctx.dry_run:
        cp_usage = _capture_invoke_usage(
            ctx.plan_agent, time.time() - cp_t0,
            prompt=turn.text, output=out,
            model=getattr(ctx.plan_agent, "model", None),
            terminal=ctx.terminal,
        )
        _accumulate_phase_usage(ctx.cross_phase_usage, "cross_plan", cp_usage)
        _print_usage_snapshot(
            f"cross_plan round={round_idx}", cp_usage,
            terminal=ctx.terminal,
        )
    return out


def _invoke_validate_round(
    ctx: CrossPlanningContext,
    round_idx: int,
    plan_text: str,
) -> tuple[bool, str, dict]:
    """One cross_validate_plan handler invocation. Returns (approved, critique, review_dict).

    ADR 0113: the cross reviewer (``review`` role) is FRESH every round; its
    handoff (plan + prior critique) rides the cross review focus, not a resumed
    session, so there is no ``resume`` input.
    """
    # Usage wrappers and ``accumulate_phase_usage`` all live in their
    # canonical leaf home ``usage`` (Stage 2) — imported from the leaf so
    # planning_loop carries no back-import into the ``app`` boundary.
    from pipeline.cross_project.usage import (
        _capture_invoke_usage,
        _print_usage_snapshot,
        accumulate_phase_usage as _accumulate_phase_usage,
    )

    if ctx.dry_run:
        return _dry_run_validate_outcome()
    cv_t0 = time.time()
    cv_result = validate_cross_plan(
        ctx.review_agent, plan_text, ctx.task, ctx.aliases, ctx.common_cwd,
        continue_session=_cross_continue_session(SessionInvocationRole.REVIEW),
        orchestrator_root=ctx.orchestrator_root,
    )
    cv_usage = _capture_invoke_usage(
        ctx.review_agent, time.time() - cv_t0,
        prompt=cv_result.prompt_text,
        output=cv_result.raw_output,
        model=getattr(ctx.review_agent, "model", None),
        terminal=ctx.terminal,
    )
    _accumulate_phase_usage(
        ctx.cross_phase_usage, "cross_validate_plan", cv_usage,
    )
    _print_usage_snapshot(
        f"cross_validate_plan round={round_idx}", cv_usage,
        terminal=ctx.terminal,
    )
    return (
        cv_result.approved,
        cv_result.critique_markdown,
        cv_result.review_dict,
    )


def _dry_run_validate_outcome() -> tuple[bool, str, dict]:
    """Synthesize an APPROVED outcome for dry-run paths.

    Round-trips through :func:`parse_review` so the dry-run path
    exercises the same JSON contract real reviewer responses use.
    """
    from pipeline.review_markdown import render_review_markdown
    from pipeline.review_parser import parse_review

    raw = approved_review_json(
        "cross_validate_plan dry run skipped reviewer invocation."
    )
    parsed = parse_review(raw)
    return (
        parsed.approved,
        render_review_markdown(parsed, title="Cross-plan QA"),
        {
            "verdict":       parsed.verdict,
            "short_summary": parsed.short_summary,
            "findings":      parsed.findings_as_dicts(),
            "risks":         list(parsed.risks),
            "checks":        list(parsed.checks),
            "raw_response":  raw,
        },
    )
