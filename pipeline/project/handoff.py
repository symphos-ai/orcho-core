"""Project-pipeline handoff service + interactive prompt-loop contract.

ADR 0042 Phase D. Owns the pause / resume / retry lifecycle for
phase handoffs in single-project runs:

* :func:`apply_phase_handoff_pause` — persist the
  ``meta.phase_handoff`` payload, mark
  ``session["status"]="awaiting_phase_handoff"``, set the checkpoint
  status, emit ``phase.handoff_requested``, snapshot metrics.
* :func:`apply_phase_handoff_resume` — load + classify the decision
  artifact (via the shared engine in
  :mod:`pipeline.control.handoff_decisions`) and dispatch to the
  halt / continue / retry_feedback / continue_with_waiver branches.
* :func:`apply_review_repair_handoff_retry` — one extra
  ``repair_changes -> review_changes`` round with operator feedback
  for the review-loop case.
* :func:`process_pending_phase_handoffs` — the explicit prompt-loop
  contract. Wraps what used to be a ``while
  run.state.phase_handoff_request is not None`` block inside
  ``_dispatch_via_v2_profile``. Returns
  :class:`PhaseHandoffLoopResult` with exactly one of
  ``paused`` / ``continue_dispatch`` / ``halted`` true so the caller
  can branch deterministically.

Plus the supporting helpers: loop strip/find, critique rehydration,
``critique_is_empty`` review-text classifier, decision-artifact loader
thin wrapper over the shared
:func:`pipeline.control.load_handoff_decision` engine.

Import discipline (ADR 0042 forbidden shape #10): this module MUST
NOT import from ``pipeline.project.app`` or
``pipeline.project_orchestrator``. The app service is one direction
up — composing handoff machinery — not a peer. An AST unit test at
``tests/unit/pipeline/test_handoff_isolation.py`` enforces this.

Does NOT merge with ``pipeline.cross_project.handoff_payloads``.
Both produce the same persisted ADR 0031 payload shape, but the
in-memory sources are different (single-project reads
``PhaseHandoffSignal`` from the runtime FSM; cross reads a review
dict). ADR 0040 records why a shared pause-and-persist primitive
would be wrong.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.io.ansi import C, paint
from core.observability import events as _events
from core.observability.logging import success, warn
from pipeline.checkpoint import PipelineStatus
from pipeline.control import (
    HandoffDecisionContext,
    RetryOutcome,
    human_directed_flag_from_state,
    load_handoff_decision,
    print_retry_feedback_banner,
    print_retry_outcome_banner,
    render_round_label,
)
from pipeline.control.handoff_prompt import (
    AdviceActionRequest,
    _Aborted as _HandoffPromptAborted,  # noqa: PLC2701 — isinstance() sentinel
    prompt_phase_handoff_action,
    should_prompt_for_phase_handoff,
)
from pipeline.engine import save_session
from pipeline.project.bootstrap import PhaseHandoffHaltedError
from pipeline.project.resume_artifacts import RESUME_PLAN_REQUIRED_KEY
from pipeline.project.types import PresentationPolicy
from pipeline.run_state import (
    HandoffAction,
    HandoffRetryMode,
    build_handoff_payload,
    build_phase_handoff_override,
    clear_active_handoff,
    continue_handoff,
    continue_with_waiver_handoff,
    request_active_handoff,
    retry_feedback_handoff,
)
from pipeline.run_state.terminal import mark_run_halted
from pipeline.runtime.handoff import (
    HUMAN_DIRECTED_FLAG_KEY,
    SCOPE_EXPANSION_HANDOFF_PHASE,
    SCOPE_EXPANSION_PARTICIPANT_ADD_PREFIX as _SCOPE_EXPANSION_PARTICIPANT_ADD_PREFIX,
    build_phase_handoff_signal,
)
from pipeline.runtime.roles import PhaseHandoffAction
from pipeline.runtime.runner import _dispatch_via_fsm

# ``run`` parameters are typed ``Any`` rather than ``_PipelineRun``.
# Phase F moved ``_PipelineRun`` into ``pipeline.project.run`` (a
# peer), so a ``TYPE_CHECKING`` peer import is now structurally
# possible without re-tripping forbidden-shape #10 (which only bans
# imports from ``pipeline.project.app`` / ``pipeline.project_orchestrator``).
# The annotation stays ``Any`` because the same duck-typed contract
# is consumed by stand-in classes in
# ``tests/unit/pipeline/runtime/test_loop_round_callback.py``; until
# a real type-check win lands, ``Any`` is the honest description of
# what the functions accept.


# ── critique helpers ──────────────────────────────────────────────────────


def critique_is_empty(text: str) -> bool:
    """True if ``text`` is empty or an APPROVED JSON review."""
    from core.contracts.review_schema import ReviewSchemaError
    from pipeline.review_parser import ReviewParseError, parse_review

    raw = text or ""
    if not raw.strip():
        return True
    try:
        return parse_review(raw).approved
    except (ReviewSchemaError, ReviewParseError):
        return False


def last_validate_plan_critique(
    session: dict, *, round_n: int | None,
) -> str:
    """Fallback rehydration for reviewer critique on persisted resume.

    On MCP/Web resume the process is fresh — ``state.last_critique`` is
    empty until something repopulates it. The active ``phase_handoff``
    payload carries ``last_output`` (the previous validate_plan output);
    if that is missing the session's per-attempt ``validate_plan`` list
    holds the critique under ``critique``.

    When ``round_n`` is known from ``active["round"]`` the lookup is
    a strict round match: return the entry whose ``attempt == round_n``,
    otherwise ``""``. Active handoff knows its round, so a session
    entry that doesn't match by round is treated as unrelated rather
    than guessed at.

    When ``round_n`` is ``None`` (caller could not determine the
    round), fall back to the single-entry case only — if exactly one
    ``validate_plan`` entry exists, return its critique. With
    multiple entries and no round to disambiguate, return ``""``
    rather than guessing with ``[-1]``, which could attach reviewer
    text from an unrelated round.
    """
    phases = session.get("phases")
    if not isinstance(phases, dict):
        return ""
    entries = phases.get("validate_plan")
    if not isinstance(entries, list) or not entries:
        return ""
    if round_n is not None:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("attempt") == round_n:
                critique = entry.get("critique") or ""
                return critique if isinstance(critique, str) else ""
        return ""
    if len(entries) == 1 and isinstance(entries[0], dict):
        critique = entries[0].get("critique") or ""
        return critique if isinstance(critique, str) else ""
    return ""


def last_review_critique(session: dict, *, round_n: int | None) -> str:
    """Return the review/fix critique for ``round_n`` from session rounds."""
    phases = session.get("phases")
    if not isinstance(phases, dict):
        return ""
    rounds = phases.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        return ""
    if round_n is not None:
        for entry in rounds:
            if not isinstance(entry, dict):
                continue
            if entry.get("round") == round_n:
                critique = entry.get("critique") or ""
                return critique if isinstance(critique, str) else ""
        return ""
    if len(rounds) == 1 and isinstance(rounds[0], dict):
        critique = rounds[0].get("critique") or ""
        return critique if isinstance(critique, str) else ""
    return ""


def merge_review_feedback(*, critique: str, feedback: str) -> str:
    """Concatenate prior reviewer critique with fresh operator feedback."""
    critique = (critique or "").strip()
    feedback = feedback.strip()
    if not critique:
        return f"Operator feedback:\n{feedback}"
    return f"{critique}\n\nOperator feedback:\n{feedback}"


# ── profile loop strip/find ───────────────────────────────────────────────


def _strip_loop(profile, round_extras_key: str):
    """Return ``profile`` with the LoopStep matching ``round_extras_key`` removed.

    Returns ``None`` when the strip would empty the profile —
    ``Profile.__post_init__`` rejects empty step tuples, so the caller
    must short-circuit dispatch and finalise directly in that case.
    """
    from pipeline.runtime import LoopStep, Profile

    if not isinstance(profile, Profile):
        return profile
    new_steps = tuple(
        entry
        for entry in profile.steps
        if not (
            isinstance(entry, LoopStep)
            and entry.round_extras_key == round_extras_key
        )
    )
    if len(new_steps) == len(profile.steps):
        return profile
    if not new_steps:
        return None
    return replace(profile, steps=new_steps)


def _find_loop(profile, round_extras_key: str):
    """Return the LoopStep matching ``round_extras_key``, or None."""
    from pipeline.runtime import LoopStep, Profile

    if not isinstance(profile, Profile):
        return None
    for entry in profile.steps:
        if (
            isinstance(entry, LoopStep)
            and entry.round_extras_key == round_extras_key
        ):
            return entry
    return None


def strip_plan_loop(profile):
    """Return ``profile`` with the canonical plan loop removed.

    The plan loop is identified by ``round_extras_key == "plan_round"``;
    any other LoopStep is left in place. Used by phase-handoff resume so
    a ``continue`` / approved-``retry_feedback`` decision doesn't replay
    the loop that already produced its terminal verdict in the prior
    run.
    """
    return _strip_loop(profile, "plan_round")


def find_plan_loop(profile):
    """Return the canonical plan loop entry, or None when absent."""
    return _find_loop(profile, "plan_round")


def strip_repair_loop(profile):
    """Return ``profile`` with the review/repair loop removed."""
    return _strip_loop(profile, "repair_round")


def find_repair_loop(profile):
    """Return the canonical review/repair loop entry, or None."""
    return _find_loop(profile, "repair_round")


def rehydrate_parsed_plan(run: Any) -> bool:
    """Seed ``state.parsed_plan`` from disk when a fresh-process resume needs it.

    Two resume shapes need the durably-persisted plan back in memory:

    * a ``continue`` / ``continue_with_waiver`` decision on a *plan-phase*
      handoff strips the ``plan -> validate_plan`` loop, so ``implement``
      becomes the first phase the runner dispatches; and
    * a ``retry_feedback`` decision on an *implement-phase* handoff (ADR
      0073) re-runs only the incomplete subtasks, which needs the original
      parsed plan to filter against.

    On a fresh-process resume (MCP / Web) the in-memory ``state.parsed_plan``
    from the original launch is gone, and the resume path never re-runs
    ``plan`` to repopulate it. Without this seed, subtask_dag ``implement``
    halts with "requires a parsed plan with at least one required subtask"
    even though the plan was durably persisted to ``parsed_plan.json`` by the
    plan phase — leaving the operator decision functionally hollow.

    Thin wrapper over the shared projector
    :func:`pipeline.project.resume_artifacts.load_and_project_parsed_plan`
    (lazy import) — the duplicate ``load_parsed_plan_artifact`` +
    ``render_plan_markdown`` body that used to live here is gone, so there is
    a single owner of parsed-plan resume loading. Returns True when a plan was
    rehydrated.

    No-op (returns False) when the plan is already in state (same-process
    interactive resume, where ``plan`` ran in this process), when
    ``run.output_dir`` is None, or when no readable artifact exists
    (``ParsedPlanArtifactError``, no markdown fallback — the downstream
    subtask_dag guard surfaces the missing plan to the operator rather than
    this helper masking it).

    This helper does NOT set ``RESUME_PLAN_REQUIRED_KEY``; the three resume
    sites that leave the plan behind (``continue`` / ``continue_with_waiver``
    on a plan handoff, and the implement-retry arm) set the marker explicitly
    before calling it, because they mark ``plan`` / ``validate_plan`` completed
    only later in-process — after ``state`` was built — so ``checkpoint.completed``
    did not yet carry ``plan`` when the resume-artifact bootstrap ran.
    """
    from pipeline.project.resume_artifacts import load_and_project_parsed_plan

    return load_and_project_parsed_plan(run.state, run.output_dir)


# ── decision-artifact loader (thin wrapper over shared engine) ────────────


def load_handoff_decision_validated(run_dir: Path, handoff_id: str):
    """Load + validate a decision artifact for resume.

    Thin wrapper around the shared
    :func:`pipeline.control.load_handoff_decision` engine (ADR 0040
    Phase B). The engine fail-fasts both on absent and on corrupt
    artifacts. Returns a :class:`pipeline.control.HandoffDecisionResult`
    whose ``action`` is a narrow Literal (``"halt" | "continue" |
    "retry_feedback" | "continue_with_waiver"``).
    """
    return load_handoff_decision(
        HandoffDecisionContext(
            run_id=run_dir.name,
            handoff_id=handoff_id,
            runs_dir=run_dir.parent,
            cwd=None,
            missing_message=(
                f"Cannot resume run: meta.phase_handoff records an "
                f"active handoff {handoff_id!r} but no decision "
                "artifact exists under phase_handoff_decisions/. Call "
                "phase_handoff_decide(run_id, handoff_id, action, ...) "
                "before resuming."
            ),
            invalid_message_prefix=(
                f"Cannot resume run {run_dir.name!r}: decision "
                f"artifact for handoff {handoff_id!r} failed strict "
                "validation"
            ),
        ),
    )


# ── result types ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PhaseHandoffResumeOutcome:
    """Result of :func:`apply_phase_handoff_resume`.

    ``profile`` is the (possibly stripped) profile that the main dispatch
    should walk; ``completed_phases`` lists names that the main dispatch
    must treat as already-completed so it doesn't replay them; ``paused``
    is True when the retry round produced a fresh rejection — the
    orchestrator should then call ``apply_phase_handoff_pause`` and
    return without further dispatch.
    """

    profile: Any
    completed_phases: frozenset[str]
    paused: bool
    invalidated_phases: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PhaseHandoffLoopResult:
    """Outcome of :func:`process_pending_phase_handoffs`.

    Exactly one of the boolean fields is True. Branches:

    * ``paused`` — pause persisted; caller must return the session
      (awaiting_phase_handoff). Triggered by non-interactive runs
      (``--no-interactive`` / non-TTY) and by operator-aborted prompts
      (Ctrl-C / exhausted retries).
    * ``halted`` — operator chose halt; session was synced to
      ``status="halted"`` + ``halt_reason="phase_handoff_halt"``. Caller
      returns the session.
    * ``continue_dispatch`` — loop drained cleanly or never had a
      pending handoff. Caller proceeds to ``run.finalize()``.

    ``profile`` carries the (possibly stripped) profile back to the
    caller for documentation purposes; the wrapper already re-dispatches
    it internally on the continue / retry branches before returning.
    ``session`` is ``run.session`` for caller convenience (same dict, no
    copy).
    """

    profile: Any
    session: dict
    paused: bool
    continue_dispatch: bool
    halted: bool

    def __post_init__(self) -> None:
        trues = int(self.paused) + int(self.continue_dispatch) + int(self.halted)
        if trues != 1:
            raise ValueError(
                "PhaseHandoffLoopResult requires exactly one of "
                "paused / continue_dispatch / halted to be true; "
                f"got paused={self.paused}, "
                f"continue_dispatch={self.continue_dispatch}, "
                f"halted={self.halted}."
            )


# ── pause / resume / retry ────────────────────────────────────────────────


def apply_phase_handoff_pause(run: Any) -> None:
    """Generic phase-handoff pause tail.

    Called after ``run_profile`` returns with a non-None
    ``state.phase_handoff_request`` signal. Writes:

    * ``run.session["phase_handoff"]`` — compat mirror payload for older
      UI plumbing that reads from the legacy session dict.
    * Sets ``run.session["status"] = "awaiting_phase_handoff"``.
    * Emits ``phase.handoff_requested`` event with the canonical fields.
    * Sets checkpoint status to
      ``PipelineStatus.AWAITING_PHASE_HANDOFF``.
    * Saves session + best-effort metrics snapshot.

    Idempotent: safe to call multiple times for the same active signal
    (overwrites with the same values).
    """
    signal = run.state.phase_handoff_request
    if signal is None:
        return

    payload = build_handoff_payload(
        handoff_id=signal.handoff_id,
        phase=signal.phase,
        handoff_type=signal.type.value,
        trigger=signal.trigger,
        verdict=signal.verdict,
        approved=signal.approved,
        round_extras_key=signal.round_extras_key,
        round_n=signal.round,
        loop_max_rounds=signal.loop_max_rounds,
        available_actions=signal.available_actions,
        artifacts=signal.artifacts,
        last_output=signal.last_output,
    )

    _events.emit(
        "phase.handoff_requested",
        phase=signal.phase,
        handoff_type=signal.type.value,
        trigger=signal.trigger,
        round=signal.round,
        handoff_id=signal.handoff_id,
    )
    # ADR 0046 Phase C (site 10): the pause-banner ``warn(...)`` is a
    # terminal courtesy line for the operator; the structural signal
    # (event + session status + checkpoint) above is what UI / MCP /
    # cross-project consume. Under ``PresentationPolicy.SILENT`` we
    # suppress the line. The hard invariant on ``ProjectRunRequest``
    # guarantees ``no_interactive=True`` whenever ``presentation`` is
    # SILENT, so the interactive prompt branch in
    # ``process_pending_phase_handoffs`` (above line 838) is
    # structurally unreachable too — no other handoff.py site needs
    # gating.
    if getattr(run, "_presentation", PresentationPolicy.TERMINAL) is PresentationPolicy.TERMINAL:
        label = render_round_label(
            phase=signal.phase,
            round=signal.round,
            loop_max_rounds=signal.loop_max_rounds,
            human_directed=human_directed_flag_from_state(run.state),
            rejected_again=(
                signal.round > signal.loop_max_rounds and not signal.approved
            ),
        )
        warn(
            f"Phase handoff requested for {label}: "
            f"trigger={signal.trigger!r}. Pausing for human decision."
        )
    request_active_handoff(run.session, payload=payload)
    if run._ckpt:
        run._ckpt.set_status(PipelineStatus.AWAITING_PHASE_HANDOFF)
    if run.output_dir:
        save_session(run.output_dir, run.session)
        # Persist the partial metrics accumulator so the run dir
        # carries ``metrics.json`` even when the subprocess exits at
        # rc=4 without reaching ``finalize``. Without this snapshot a
        # subsequent ``phase_handoff_decide(... halt ...)`` would land
        # ``evidence.json`` next to a missing ``metrics.json`` (halt is
        # SDK-side and has no access to the in-memory accumulator), and
        # the curated bundle would degrade to zero metrics rollups for
        # every operator-halted run. Best-effort: an I/O failure here
        # must not break the pause path.
        with contextlib.suppress(OSError):
            run._metrics.save(run.output_dir)


def _persist_handoff_running_state(run: Any) -> None:
    """Persist that a decided handoff is no longer active."""
    ckpt = getattr(run, "_ckpt", None)
    if ckpt:
        ckpt.set_status(PipelineStatus.RUNNING)
    output_dir = getattr(run, "output_dir", None)
    if output_dir:
        save_session(output_dir, run.session)
        metrics = getattr(run, "_metrics", None)
        with contextlib.suppress(OSError):
            if metrics is not None:
                metrics.save(output_dir)


def _persist_handoff_retry_metrics(run: Any) -> None:
    """Best-effort metrics snapshot after an in-process retry round."""
    output_dir = getattr(run, "output_dir", None)
    metrics = getattr(run, "_metrics", None)
    if output_dir and metrics is not None:
        with contextlib.suppress(OSError):
            metrics.save(output_dir)


def _persist_decidable_after_guard_abort(run: Any) -> None:
    """Restore a decidable persisted state after a review-retry subject-guard
    abort.

    The clean-HEAD / wrong-cwd guard runs before ``retry_feedback_handoff``
    clears the payload, so the active ``phase_handoff`` and the reused
    ``worktree`` subject are still in ``run.session``. But session-init wrote
    ``meta.status='running'`` before dispatch, which — combined with an active
    handoff — is a torn shape SDK ``decide``/``halt`` will not accept. Re-assert
    ``awaiting_phase_handoff`` (only when the active payload is present) and
    persist the session as-is so the retained ``worktree`` block survives, the
    pending handoff id stays visible to status/snapshot surfaces, and a later
    resume after the operator restores the rejected diff works. Best-effort:
    persistence failure must not mask the recoverable guard error being raised.
    """
    if isinstance(run.session.get("phase_handoff"), dict):
        run.session["status"] = "awaiting_phase_handoff"
    ckpt = getattr(run, "_ckpt", None)
    if ckpt is not None:
        with contextlib.suppress(Exception):
            ckpt.set_status(PipelineStatus.AWAITING_PHASE_HANDOFF)
    output_dir = getattr(run, "output_dir", None)
    if output_dir is not None:
        with contextlib.suppress(Exception):
            save_session(output_dir, run.session)


def _retry_resumes_provider_session(run: Any) -> bool:
    """Best-effort: does the run carry a provider session the retry reuses?

    A persisted ``agent_sessions`` row means the retry round resumes that
    provider session (falling back to a fresh one only on a miss — see
    :func:`pipeline.phases.builtin.session_invoke`). UX hint only; any
    lookup failure degrades to ``False`` (render as a fresh session).
    """
    ckpt = getattr(run, "_ckpt", None)
    if ckpt is None:
        return False
    try:
        return bool(ckpt.get_agent_sessions())
    except Exception:  # noqa: BLE001 — best-effort operator hint, never fatal
        return False


def _events_count(run: Any) -> int:
    """Current number of recorded events for ``run`` (0 on any failure)."""
    output_dir = getattr(run, "output_dir", None)
    if output_dir is None:
        return 0
    try:
        from core.observability import events as _ev
        return len(_ev.read_all(output_dir))
    except Exception:  # noqa: BLE001 — observability read is best-effort
        return 0


def _provider_fallback_since(run: Any, since: int) -> bool:
    """True if a provider-session fallback event landed after ``since``."""
    output_dir = getattr(run, "output_dir", None)
    if output_dir is None:
        return False
    try:
        from core.observability import events as _ev
        recent = _ev.read_all(output_dir)[since:]
    except Exception:  # noqa: BLE001 — observability read is best-effort
        return False
    return any(e.kind == "phase.provider_session_fallback" for e in recent)


def _classify_retry_outcome(run: Any, *, since: int, paused: bool) -> RetryOutcome:
    """Pick the post-retry banner outcome.

    A re-pause dominates (the operator must act next); otherwise a
    detected provider-session fallback is surfaced; otherwise the retry
    was accepted and the handoff is closed.
    """
    if paused:
        return RetryOutcome.REJECTED_AGAIN
    if _provider_fallback_since(run, since):
        return RetryOutcome.PROVIDER_FALLBACK
    return RetryOutcome.APPROVED


def _begin_retry_banner(run: Any) -> tuple[str, str, int] | None:
    """Print the pre-retry banner when the pending decision is retry_feedback.

    Reads the active payload + its decision artifact (present on both the
    interactive in-process path and a checkpoint/preflight resume). Returns
    a ``(handoff_id, phase, events_count)`` context for the post-banner, or
    ``None`` when the decision is not ``retry_feedback`` (no banner).
    """
    active = run.session.get("phase_handoff")
    if not isinstance(active, dict):
        return None
    handoff_id = active.get("id")
    if not isinstance(handoff_id, str) or not handoff_id:
        return None
    if getattr(run, "output_dir", None) is None:
        return None
    try:
        decision = load_handoff_decision_validated(run.output_dir, handoff_id)
    except RuntimeError:
        return None
    if decision.action != PhaseHandoffAction.RETRY_FEEDBACK.value:
        return None
    phase = active.get("phase")
    phase_str = phase if isinstance(phase, str) and phase else "?"
    round_n = active.get("round")
    loop_max = active.get("loop_max_rounds")
    worktree_subject, worktree_isolated = _retry_worktree_subject(run)
    print_retry_feedback_banner(
        run_id=run.session_ts,
        handoff_id=handoff_id,
        rejected_phase=phase_str,
        retry_kind="repair" if phase_str == "review_changes" else "plan",
        retry_round=(int(round_n) + 1) if isinstance(round_n, int) else 1,
        loop_max_rounds=int(loop_max) if isinstance(loop_max, int) else 1,
        feedback=decision.feedback or "",
        resume_provider_session=_retry_resumes_provider_session(run),
        worktree_subject=worktree_subject,
        worktree_isolated=worktree_isolated,
    )
    return (handoff_id, phase_str, _events_count(run))


def _retry_worktree_subject(run: Any) -> tuple[str | None, bool]:
    """Resolve the worktree the retry repairs in for the pre-retry banner.

    Prefers the persisted ``session['worktree']`` block (path + isolation),
    falling back to ``run.git_cwd`` when no block is recorded (an in-place
    checkout). Returns ``(path_or_None, is_isolated)``; best-effort, never
    raises — a missing/garbled block degrades to ``(None, True)`` so the
    banner renders ``(not recorded)`` rather than failing the resume.
    """
    block = run.session.get("worktree") if isinstance(run.session, dict) else None
    if isinstance(block, dict):
        path = block.get("path")
        subject = path if isinstance(path, str) and path else None
        isolated = block.get("isolation") not in (None, "off")
        if subject is not None:
            return subject, isolated
        # Block present but no usable path: fall through to git_cwd below,
        # keeping the recorded isolation intent for the label.
        git_cwd = getattr(run, "git_cwd", None)
        if isinstance(git_cwd, str) and git_cwd:
            return git_cwd, isolated
        return None, isolated
    git_cwd = getattr(run, "git_cwd", None)
    if isinstance(git_cwd, str) and git_cwd:
        # No recorded worktree block -> the retry edits the cwd in place.
        return git_cwd, False
    return None, True


def _finish_retry_banner(
    run: Any,
    retry_ctx: tuple[str, str, int] | None,
    outcome: PhaseHandoffResumeOutcome,
) -> None:
    """Print the post-retry banner classified from the resume outcome."""
    if retry_ctx is None:
        return
    handoff_id, phase_str, since = retry_ctx
    print_retry_outcome_banner(
        run_id=run.session_ts,
        handoff_id=handoff_id,
        rejected_phase=phase_str,
        outcome=_classify_retry_outcome(run, since=since, paused=outcome.paused),
    )


def apply_phase_handoff_resume_with_banners(
    run: Any, profile, ctx, *, on_round_end: Any = None,
) -> PhaseHandoffResumeOutcome:
    """``apply_phase_handoff_resume`` wrapped with pre/post retry banners.

    Both the interactive in-process loop (``process_pending_phase_handoffs``)
    and a checkpoint/preflight resume (``dispatch_via_v2_profile``) enter
    here, so a ``retry_feedback`` decision gets the same operator banners no
    matter which path consumed the decision artifact. Non-retry actions add
    no banner. The resume body itself is unchanged.
    """
    retry_ctx = _begin_retry_banner(run)
    outcome = apply_phase_handoff_resume(
        run, profile, ctx, on_round_end=on_round_end,
    )
    _finish_retry_banner(run, retry_ctx, outcome)
    return outcome


def _review_repair_steps(repair_loop):
    review_step = None
    repair_step = None
    for step in repair_loop.steps:
        if getattr(step, "phase", None) == "review_changes":
            review_step = step
        elif getattr(step, "phase", None) == "repair_changes":
            repair_step = step
    return review_step, repair_step


def apply_review_repair_handoff_retry(
    *,
    run: Any,
    profile,
    ctx,
    active: dict,
    handoff_id: str,
    feedback: str,
    note: str | None,
    decided_at: str,
) -> PhaseHandoffResumeOutcome:
    """One extra ``repair_changes -> review_changes`` round with operator
    feedback.
    """
    repair_loop = find_repair_loop(profile)
    if repair_loop is None:
        raise RuntimeError(
            f"Cannot resume run: profile {getattr(profile, 'name', '?')!r} "
            "has no canonical review_changes -> repair_changes loop, "
            f"but the active handoff {handoff_id!r} decided retry_feedback."
        )
    review_step, repair_step = _review_repair_steps(repair_loop)
    if review_step is None or repair_step is None:
        raise RuntimeError(
            f"Cannot resume run: profile {getattr(profile, 'name', '?')!r} "
            "must contain review_changes and repair_changes in the "
            f"repair loop for retry_feedback handoff {handoff_id!r}."
        )

    # Prove the rejected diff subject is present BEFORE any state mutation or
    # write-phase dispatch. The guard is read-only, so a clean-HEAD / wrong-cwd
    # abort here leaves the active handoff + decision intact. session-init
    # persisted meta.status='running' before dispatch, though, so on abort we
    # MUST re-assert a decidable awaiting_phase_handoff state (keeping the active
    # handoff payload and the reused worktree subject) before re-raising — a
    # torn running+handoff would block SDK decide/halt and a later resume. Runs
    # only on this review-retry path; other resume branches are untouched.
    from pipeline.project.retry_subject import (
        RepairSubjectUnproven,
        guard_review_retry_subject,
    )
    try:
        guard_review_retry_subject(run)
    except RepairSubjectUnproven:
        _persist_decidable_after_guard_abort(run)
        raise

    prior_round_for_critique = active.get("round")
    if not isinstance(prior_round_for_critique, int):
        prior_round_for_critique = None
    last_output = active.get("last_output")
    if not isinstance(last_output, str):
        last_output = ""
    run.state.last_critique = merge_review_feedback(
        critique=(
            last_output
            or last_review_critique(
                run.session, round_n=prior_round_for_critique,
            )
            or ""
        ),
        feedback=feedback,
    )
    run.state.human_feedback = feedback
    transition = retry_feedback_handoff(
        run.session,
        handoff_id=handoff_id,
        mode=HandoffRetryMode.REPAIR,
        feedback=feedback,
        note=note,
        decided_at=decided_at,
    )
    run.state.extras["phase_handoff_override"] = transition.override
    run.state.extras["human_feedback"] = transition.human_feedback
    _persist_handoff_running_state(run)

    prior_round = int(active.get("round", repair_loop.max_rounds) or 0)
    retry_round_n = prior_round + 1
    loop_max_rounds = int(
        active.get("loop_max_rounds", repair_loop.max_rounds)
        or repair_loop.max_rounds,
    )
    run.state.extras[repair_loop.round_extras_key] = retry_round_n
    run.state.extras[f"{repair_loop.round_extras_key}_max"] = loop_max_rounds
    run.state.extras[HUMAN_DIRECTED_FLAG_KEY] = True
    prev_active_key = run.state.extras.get("_active_loop_round_key")
    run.state.extras["_active_loop_round_key"] = repair_loop.round_extras_key
    prev_adapter_registry = ctx.session_adapter_registry
    try:
        ctx.session_adapter_registry = None
        run.state = _dispatch_via_fsm(
            repair_step,
            run.state,
            ctx,
            on_phase_start=run._on_phase_start,
            on_phase_end=run._on_phase_end,
        )
        ctx.session_adapter_registry = prev_adapter_registry
        if not run.state.halt:
            run.state = _dispatch_via_fsm(
                review_step,
                run.state,
                ctx,
                on_phase_start=run._on_phase_start,
                on_phase_end=run._on_phase_end,
            )
            review_log = run.state.phase_log.get("review_changes") or {}
            if isinstance(review_log, dict):
                review_critique = review_log.get("critique")
                if isinstance(review_critique, str):
                    pending = dict(
                        run.state.phase_log.get("rounds_pending", {}) or {},
                    )
                    pending["critique"] = review_critique
                    run.state.phase_log["rounds_pending"] = pending
        if not run.state.halt and prev_adapter_registry is not None:
            adapter = prev_adapter_registry.get_or_none("repair_changes")
            if adapter is not None:
                adapter.write(
                    "repair_changes",
                    run.state,
                    run.session,
                    round_n=retry_round_n,
                )
                if run.output_dir:
                    save_session(run.output_dir, run.session)
        run._metrics.add_round()
        if not run.state.halt:
            signal = build_phase_handoff_signal(
                review_step, repair_loop, run.state, retry_round_n,
            )
            if signal is not None:
                run.state.phase_handoff_request = signal
                run.state.stop(
                    f"phase handoff requested: {signal.handoff_id}",
                )
                _persist_handoff_retry_metrics(run)
                return PhaseHandoffResumeOutcome(
                    profile=profile,
                    completed_phases=frozenset(),
                    paused=True,
                )
    finally:
        ctx.session_adapter_registry = prev_adapter_registry
        run.state.extras.pop(HUMAN_DIRECTED_FLAG_KEY, None)
        if prev_active_key is None:
            run.state.extras.pop("_active_loop_round_key", None)
        else:
            run.state.extras["_active_loop_round_key"] = prev_active_key

    _persist_handoff_retry_metrics(run)
    return PhaseHandoffResumeOutcome(
        profile=strip_repair_loop(profile),
        completed_phases=_profile_phases_through(profile, "repair_changes"),
        paused=False,
    )


def _synthesize_continue_waiver_text(findings: Any, note: str | None) -> str:
    """Synthesize a waiver rationale for a bare ``continue`` on implement (§4d).

    ``apply_waiver_to_state`` requires a non-empty rationale, but a bare
    ``continue`` carries no operator verdict. Build one from the active
    findings (the incomplete subtasks the operator chose to accept) and append
    any operator ``note``.
    """
    if isinstance(findings, list) and findings:
        findings_str = ", ".join(str(f) for f in findings)
    else:
        findings_str = "(no findings recorded)"
    text = (
        "Operator continued without explicit waiver feedback; accepted "
        f"incomplete implement delivery: {findings_str}"
    )
    if note and note.strip():
        text = f"{text}\n{note.strip()}"
    return text


def _mark_implement_waived(run: Any, handoff_id: str, action: str) -> None:
    """Rewrite the persisted implement delivery record incomplete → waived.

    On an accept resume the implement phase is skipped (it is in
    ``completed_phases``), so the session entry persisted by the original
    paused run still reads ``delivery_status='incomplete'``. Rewrite it in
    place so evidence / status surfaces report the waived outcome.

    ``action`` (``continue`` for a bare accept, ``continue_with_waiver`` for an
    explicit operator waiver) is stamped onto the entry too: the implement
    handler that would normally persist it via ``BuildAdapter`` does not re-run
    on resume, so without this the evidence breadcrumb cannot tell a bare
    continue from a waiver (see ``pipeline/evidence/collector.py``).
    """
    phases = run.session.get("phases")
    if not isinstance(phases, dict):
        return
    impl = phases.get("implement")
    if not isinstance(impl, dict):
        return
    impl["delivery_status"] = "waived"
    impl["delivery_waived"] = True
    impl["waiver_id"] = handoff_id
    impl["action"] = action


def _build_retry_prior_context(
    run: Any, *, exclude: set[str],
) -> dict[str, dict[str, Any]]:
    """Degraded upstream context for DONE subtasks, from persisted receipts.

    On a fresh-process ``retry_feedback`` resume the re-run incomplete subtasks
    must still see their done dependencies' attestation context. The live agent
    output is gone, but ``meta.phases.implement.implementation_receipts`` persists
    each subtask's ``state`` + ``attestation_summary`` / ``attestation_error`` —
    enough for a degraded :class:`PriorSubtaskContext` (the subtask_dag handler
    rebuilds the value object from this). Returns
    ``{subtask_id: {attestation_summary, attestation_error}}`` for ``done``
    receipts whose id is not in ``exclude`` (the retry set).
    """
    impl = (run.session.get("phases") or {}).get("implement")
    receipts = impl.get("implementation_receipts") if isinstance(impl, dict) else None
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(receipts, list):
        return out
    for r in receipts:
        if not isinstance(r, dict):
            continue
        sid = r.get("subtask_id")
        if not isinstance(sid, str) or sid in exclude or r.get("state") != "done":
            continue
        out[sid] = {
            "attestation_summary": str(r.get("attestation_summary", "")),
            "attestation_error": r.get("attestation_error"),
        }
    return out


def _profile_phase_names(profile) -> tuple[str, ...]:
    """Return phase names from a profile, flattening loop steps in order."""
    out: list[str] = []
    for step in getattr(profile, "steps", ()) or ():
        phase = getattr(step, "phase", None)
        if isinstance(phase, str):
            out.append(phase)
            continue
        for inner in getattr(step, "steps", ()) or ():
            inner_phase = getattr(inner, "phase", None)
            if isinstance(inner_phase, str):
                out.append(inner_phase)
    return tuple(out)


def _profile_phases_from(profile, start_phase: str) -> frozenset[str]:
    names = _profile_phase_names(profile)
    if start_phase not in names:
        return frozenset({start_phase})
    return frozenset(names[names.index(start_phase):])


def _profile_phases_through(profile, end_phase: str) -> frozenset[str]:
    """Return profile phases up to and including ``end_phase``.

    Used by phase-handoff retry resumes after an in-process retry succeeds.
    At that point the run has logically advanced past the paused loop even if
    the checkpoint store cannot prove every upstream phase as completed. The
    resume outcome must therefore prevent dispatch from replaying earlier
    plan/implement phases before walking on to the remaining tail.
    """
    names = _profile_phase_names(profile)
    if end_phase not in names:
        return frozenset({end_phase})
    return frozenset(names[: names.index(end_phase) + 1])


def _apply_scope_expansion_handoff_resume(
    run: Any,
    profile,
    *,
    active: dict,
    handoff_id: str,
    action: str,
    feedback: Any,
    note: str | None,
    decided_at: str | None,
) -> PhaseHandoffResumeOutcome:
    """Resume arm for a ``final_acceptance`` scope-expansion handoff (ADR 0112 §5).

    The scope-expansion sanction handoff (``scope_expansion:participant_add:<repo>``
    or ``scope_expansion:out_of_plan``) rides the ADR 0038 lifecycle and both
    variants carry ``phase="final_acceptance"`` — a bare top-level phase with no
    plan/repair loop to strip or re-enter. The operator sanction closes the pause
    and lets the run continue; neither variant routes through the plan loop (the F1
    review finding: ``final_acceptance`` fell through to the generic ``continue``
    arm which stripped the plan loop + marked plan/validate_plan completed).

    The two triggers re-enter at different points, so ``completed_phases`` differs:

    * ``scope_expansion:out_of_plan`` is raised AT ``final_acceptance`` (after the
      handler ran and recorded its verdict). On resume the terminal phase is
      reported completed so the resumed walk short-circuits it instead of re-raising
      the same handoff — the idempotency contract on
      :func:`pipeline.phases.builtin.scope_expansion_support.raise_scope_expansion_handoff`.
    * ``scope_expansion:participant_add:<repo>`` is raised EARLY by the promotion
      seam (``_on_phase_pre`` → ``evaluate_scope_expansion_promotion``);
      ``final_acceptance`` has NOT run yet. Reporting it completed would skip the
      real terminal gate, so the resume reports NO completed phase: the run re-walks
      normally and the promotion seam's decision-artifact idempotency
      (``participant_promotion._participant_add_decided``) falls through to
      ``add_participant`` at the next dispatched phase's pre-hook.

    For both, ``continue`` clears the payload (no waiver) and ``continue_with_waiver``
    additionally persists a durable ``phase_handoff_waiver`` to the session (and a
    runtime copy in ``state.extras``) so a fresh-process resume and every downstream
    gate carry the sanction.

    ``retry_feedback`` is intentionally absent from this handoff's
    ``available_actions`` (see :func:`build_scope_expansion_handoff_signal`): a
    runtime-raised sanction at the terminal phase has no plan/repair loop to retry
    into, and ``continue_with_waiver`` is the durable escape hatch.
    ``phase_handoff_decide`` rejects it before any decision artifact is written;
    this arm rejects it defensively in case of a hand-edited artifact.
    """
    trigger = str(active.get("trigger") or "")
    # out_of_plan ran final_acceptance → skip it on resume; participant_add was
    # raised before it → leave it to run (promotion idempotency re-fires the seam).
    if trigger.startswith(_SCOPE_EXPANSION_PARTICIPANT_ADD_PREFIX):
        completed: frozenset[str] = frozenset()
    else:
        completed = frozenset({SCOPE_EXPANSION_HANDOFF_PHASE})

    if action == "continue":
        transition = continue_handoff(
            run.session,
            handoff_id=handoff_id,
            note=note,
            decided_at=decided_at,
        )
        run.state.extras["phase_handoff_override"] = transition.override
        _persist_handoff_running_state(run)
        return PhaseHandoffResumeOutcome(
            profile=profile,
            completed_phases=completed,
            paused=False,
        )

    if action == "continue_with_waiver":
        if not (isinstance(feedback, str) and feedback.strip()):
            raise RuntimeError(
                f"Cannot resume run: continue_with_waiver decision for "
                f"{handoff_id!r} is missing the operator verdict (feedback). "
                "The waiver must record why the out-of-plan scope expansion is "
                "accepted."
            )
        raw_artifacts = active.get("artifacts")
        waived_findings = (
            raw_artifacts.get("findings")
            if isinstance(raw_artifacts, dict)
            else None
        )
        critique = active.get("last_output")
        if not isinstance(critique, str):
            critique = ""
        transition = continue_with_waiver_handoff(
            run.session,
            handoff_id=handoff_id,
            phase=SCOPE_EXPANSION_HANDOFF_PHASE,
            feedback=feedback,
            note=note,
            decided_at=decided_at,
            findings=waived_findings,
            critique=critique,
        )
        # Durable record: persisted to session -> meta.json by
        # ``_persist_handoff_running_state`` so a fresh-process resume (MCP/Web)
        # rehydrates the waiver, and a runtime copy in ``state.extras`` for the
        # gates dispatched in THIS process. Mirror it into the session directly
        # (like the implement resume arm) rather than relying on a phase-end sync,
        # which never fires for out_of_plan (final_acceptance is skipped on
        # resume). Downstream gates read it until the final gate passes.
        run.session["phase_handoff_waiver"] = transition.waiver
        run.state.extras["phase_handoff_waiver"] = transition.waiver
        run.state.extras["phase_handoff_override"] = transition.override
        _persist_handoff_running_state(run)
        return PhaseHandoffResumeOutcome(
            profile=profile,
            completed_phases=completed,
            paused=False,
        )

    # action == "retry_feedback" — not in the scope-expansion action set; decide
    # rejects it before writing the decision artifact. Defend against a
    # hand-edited artifact rather than mis-routing into a plan/repair retry.
    raise RuntimeError(
        f"Cannot resume run: retry_feedback is not a supported action for the "
        f"scope-expansion handoff {handoff_id!r}. The terminal final_acceptance "
        "seam has no plan/repair loop to retry into — use continue, "
        "continue_with_waiver, or halt."
    )


def _apply_implement_handoff_resume(
    run: Any,
    profile,
    *,
    active: dict,
    handoff_id: str,
    action: str,
    feedback: Any,
    note: str | None,
    decided_at: str | None,
) -> PhaseHandoffResumeOutcome:
    """Resume arm for an implement-phase handoff (ADR 0073).

    ACCEPT (``continue`` / ``continue_with_waiver``): implement is a bare step,
    so there is no loop to strip — mark it completed, apply a waiver via the T9
    API and sync it to the session directly (implement is skipped on resume, so
    ``_on_phase_end`` will not fire the phase-end sync), and rewrite the
    persisted implement ``delivery_status`` to ``waived``. A bare ``continue``
    synthesizes its waiver text from the active findings and records
    ``action='continue'``; ``continue_with_waiver`` uses the operator verdict
    and records ``action='continue_with_waiver'``. Both set
    ``decided_by='operator'`` and land ``delivery_status='waived'``.

    ``retry_feedback``: seed ``state.extras['implement_retry']`` with the
    incomplete ids + feedback so the re-dispatched implement re-runs ONLY those
    subtasks (the profile is unchanged, ``completed_phases`` is empty), and
    rehydrate the parsed plan for a fresh-process resume.
    """
    from pipeline.project.handoff_waiver import (
        apply_waiver_to_state,
        sync_waiver_to_session,
    )

    raw_artifacts = active.get("artifacts")
    findings = (
        raw_artifacts.get("findings")
        if isinstance(raw_artifacts, dict)
        else None
    )
    critique = active.get("last_output")
    if not isinstance(critique, str):
        critique = ""

    if action in ("continue", "continue_with_waiver"):
        if action == "continue_with_waiver":
            if not (isinstance(feedback, str) and feedback.strip()):
                raise RuntimeError(
                    f"Cannot resume run: continue_with_waiver decision for "
                    f"{handoff_id!r} is missing the operator verdict "
                    "(feedback). The waiver must record why the incomplete "
                    "delivery is accepted."
                )
            waiver_text = feedback
            resume_action = PhaseHandoffAction.CONTINUE_WITH_WAIVER.value
            override_feedback: str | None = feedback
        else:
            # §4(d): bare continue — synthesize a rationale from findings.
            waiver_text = _synthesize_continue_waiver_text(findings, note)
            resume_action = PhaseHandoffAction.CONTINUE.value
            override_feedback = None

        apply_waiver_to_state(
            run.state,
            handoff_id=handoff_id,
            phase="implement",
            waiver_text=waiver_text,
            decided_by="operator",
            note=note,
            decided_at=decided_at,
            findings=findings,
            critique=critique,
        )
        # implement is skipped on resume (it is in completed_phases) → the
        # phase-end sync never fires; mirror the waiver to the session here.
        sync_waiver_to_session(run)
        _mark_implement_waived(run, handoff_id, resume_action)

        run.state.extras["phase_handoff_override"] = build_phase_handoff_override(
            handoff_id=handoff_id,
            action=HandoffAction(resume_action),
            feedback=override_feedback,
            note=note,
            decided_at=decided_at,
        )
        clear_active_handoff(run.session)
        _persist_handoff_running_state(run)
        return PhaseHandoffResumeOutcome(
            profile=profile,
            completed_phases=frozenset({"implement"}),
            paused=False,
        )

    # action == "retry_feedback" — exhaustive by Literal narrowing.
    if not (isinstance(feedback, str) and feedback.strip()):
        raise RuntimeError(
            f"Cannot resume run: retry_feedback decision for {handoff_id!r} "
            "is missing the feedback string."
        )
    # The retry set is every subtask that did not deliver a closed receipt:
    # INCOMPLETE subtasks AND any with a MISSING receipt. A missing receipt is
    # not "done", so it must be re-run, never treated as a satisfied dependency
    # by the prior-context construction below.
    retry_ids: list[str] = []
    if isinstance(raw_artifacts, dict):
        for _key in ("incomplete_subtasks", "missing_subtask_receipts"):
            raw_ids = raw_artifacts.get(_key)
            if isinstance(raw_ids, list):
                for i in raw_ids:
                    sid = str(i)
                    if sid not in retry_ids:
                        retry_ids.append(sid)
    # Rebuild a durable (degraded) upstream context for the DONE subtasks from
    # the persisted ``implementation_receipts`` so the re-run incomplete subtasks
    # still see their dependencies' attestation summary on a fresh-process
    # resume — the live agent output is gone, but the receipts persist it.
    prior_context = _build_retry_prior_context(run, exclude=set(retry_ids))
    run.state.extras["implement_retry"] = {
        "incomplete_ids": retry_ids,
        "feedback":       feedback,
        "prior_context":  prior_context,
    }
    run.state.extras["phase_handoff_override"] = build_phase_handoff_override(
        handoff_id=handoff_id,
        action=HandoffAction.RETRY_FEEDBACK,
        feedback=feedback,
        note=note,
        decided_at=decided_at,
    )
    run.state.human_feedback = feedback
    run.state.last_critique = critique
    # Fresh-process resume needs the persisted plan back to filter the retry
    # against (the rehydrate is a no-op when the plan is already in state).
    # Mark the plan as required first: this in-process resume marks the plan
    # phases completed later (not in ``checkpoint.completed`` at state-build
    # time), so without this the subtask_dag guard would not fire if the
    # artifact is missing/corrupt.
    run.state.extras[RESUME_PLAN_REQUIRED_KEY] = True
    rehydrate_parsed_plan(run)
    clear_active_handoff(run.session)
    _persist_handoff_running_state(run)
    return PhaseHandoffResumeOutcome(
        profile=profile,
        completed_phases=frozenset(),
        paused=False,
        invalidated_phases=_profile_phases_from(profile, "implement"),
    )


def apply_phase_handoff_resume(
    run: Any, profile, ctx, *, on_round_end: Any = None,
) -> PhaseHandoffResumeOutcome:
    """Consume an active ``meta.phase_handoff`` + matching decision.

    Resume contract:

    * No active payload → fresh dispatch (return profile unchanged).
    * Active payload + no decision artifact → fail-fast: the caller must
      decide before resuming. (The shared engine raises.)
    * Active payload + ``halt`` decision → defensive heal of torn meta
      state, then raise :class:`PhaseHandoffHaltedError` — halt is
      terminal and the SDK already finalised the run.
    * Active payload + ``continue`` decision → strip the matching loop
      (plan or repair, depending on which phase paused), mark the
      involved phases as completed, return.
    * Active payload + ``continue_with_waiver`` decision → like
      ``continue`` (strip the loop, no extra reviewer round, machine
      verdict left REJECTED) but require a non-empty operator verdict and
      durably persist a ``phase_handoff_waiver`` into the session/meta
      plus a runtime copy in ``state.extras`` so all downstream review
      gates inject the waiver and do not reopen the waived findings.
    * Active payload + ``retry_feedback`` decision → dispatch exactly
      one extra round (plan→validate or repair→review depending on
      phase). If that round triggers a fresh handoff, stash the signal
      and return ``paused=True``.
    """
    if run.output_dir is None:
        return PhaseHandoffResumeOutcome(profile, frozenset(), False)
    active = run.session.get("phase_handoff")
    if not isinstance(active, dict):
        return PhaseHandoffResumeOutcome(profile, frozenset(), False)
    handoff_id = active.get("id")
    if not isinstance(handoff_id, str) or not handoff_id:
        return PhaseHandoffResumeOutcome(profile, frozenset(), False)

    decision = load_handoff_decision_validated(run.output_dir, handoff_id)
    action = decision.action
    feedback = decision.feedback
    note = decision.note
    decided_at = decision.decided_at

    if action == "halt":
        # Halt is terminal by audit contract: the SDK normally
        # finalises ``meta.status="halted"`` + clears the active
        # payload as part of the halt transition, which the
        # carry-forward guard in ``init_session_with_atexit`` catches
        # before dispatch ever starts. Reaching here means the SDK
        # wrote the halt decision artifact but the meta finalisation
        # step did not land — a torn write, a manual edit, or a
        # partial restore. The active payload + valid halt artifact
        # combination is *not* an invitation to continue dispatching:
        # heal the torn meta state synchronously (so the next launch
        # is rejected by the carry-forward guard) and then refuse this
        # resume.
        mark_run_halted(
            run.session, halt_reason="phase_handoff_halt", halted_at=decided_at,
        )
        if run._ckpt:
            run._ckpt.set_status(PipelineStatus.HALTED)
        heal_error: OSError | None = None
        if run.output_dir:
            try:
                save_session(run.output_dir, run.session)
            except OSError as exc:
                heal_error = exc
        if heal_error is not None:
            raise PhaseHandoffHaltedError(
                f"Cannot resume run {run.output_dir.name!r}: decision "
                f"artifact for handoff {handoff_id!r} records ``halt`` "
                "and the orchestrator attempted to heal the torn meta "
                f"state, but writing meta.json failed: {heal_error}. "
                "Manual repair required — halt is terminal."
            ) from heal_error
        raise PhaseHandoffHaltedError(
            f"Cannot resume run {run.output_dir.name!r}: decision "
            f"artifact for handoff {handoff_id!r} records ``halt`` but "
            "meta.status was not yet halted (torn write or partial "
            "restore). Meta has been healed to ``status=halted`` — "
            "halt is terminal; start a new run instead."
        )

    if active.get("phase") == "implement":
        # ADR 0073: the implement handoff is a bare top-level step (no loop to
        # strip). Accept marks implement completed + records a waiver;
        # retry_feedback re-runs only the incomplete subtasks. Kept as thin
        # glue in a dedicated helper.
        return _apply_implement_handoff_resume(
            run,
            profile,
            active=active,
            handoff_id=handoff_id,
            action=action,
            feedback=feedback,
            note=note,
            decided_at=decided_at,
        )

    if active.get("phase") == SCOPE_EXPANSION_HANDOFF_PHASE:
        # ADR 0112 §5 (F1 fix): the scope-expansion sanction handoff
        # (``scope_expansion:participant_add:<repo>`` / ``scope_expansion:out_of_plan``)
        # is raised at the terminal ``final_acceptance`` seam — a bare top-level
        # phase with no plan/repair loop to strip or re-enter. Routing it through
        # the generic ``continue`` arm below would ``strip_plan_loop()`` + mark
        # plan/validate_plan completed + rehydrate the plan, mis-resuming a
        # finished run as a plan-loop continuation (and ``retry_feedback`` would
        # mis-route into a plan retry). Dispatch to the dedicated arm so the
        # operator sanction simply closes the pause and lets the run finalize.
        return _apply_scope_expansion_handoff_resume(
            run,
            profile,
            active=active,
            handoff_id=handoff_id,
            action=action,
            feedback=feedback,
            note=note,
            decided_at=decided_at,
        )

    if action == "continue":
        active_phase = active.get("phase")
        if active_phase == "review_changes":
            next_profile = strip_repair_loop(profile)
            completed = frozenset({"review_changes", "repair_changes"})
        else:
            next_profile = strip_plan_loop(profile)
            completed = frozenset({"plan", "validate_plan"})
            # Stripping the plan loop makes implement the first phase the
            # runner sees; rehydrate the rejected plan from disk so a
            # fresh-process resume doesn't halt subtask_dag with a missing
            # parsed plan. Mark the plan required first (see the implement-retry
            # arm): the plan phases are marked completed later in-process, so
            # checkpoint.completed did not carry them at state-build time.
            run.state.extras[RESUME_PLAN_REQUIRED_KEY] = True
            rehydrate_parsed_plan(run)
        transition = continue_handoff(
            run.session,
            handoff_id=handoff_id,
            note=note,
            decided_at=decided_at,
        )
        run.state.extras["phase_handoff_override"] = transition.override
        _persist_handoff_running_state(run)
        return PhaseHandoffResumeOutcome(
            profile=next_profile,
            completed_phases=completed,
            paused=False,
        )

    if action == "continue_with_waiver":
        # Like ``continue`` (strip the loop, no extra reviewer round,
        # machine verdict stays REJECTED) but the operator verdict is
        # mandatory and a durable waiver is persisted so every downstream
        # review gate injects it and does not reopen the waived findings.
        if not feedback.strip():
            raise RuntimeError(
                f"Cannot resume run: continue_with_waiver decision for "
                f"{handoff_id!r} is missing the operator verdict "
                "(feedback). The waiver must record why the rejected "
                "findings are accepted."
            )
        active_phase = active.get("phase")
        if active_phase == "review_changes":
            next_profile = strip_repair_loop(profile)
            completed = frozenset({"review_changes", "repair_changes"})
        else:
            next_profile = strip_plan_loop(profile)
            completed = frozenset({"plan", "validate_plan"})
            # See the ``continue`` branch: carry the (rejected, now waived)
            # plan into implement on a fresh-process resume. Mark the plan
            # required first so the subtask_dag guard fires on a
            # missing/corrupt artifact (plan phases marked completed later
            # in-process, after state build).
            run.state.extras[RESUME_PLAN_REQUIRED_KEY] = True
            rehydrate_parsed_plan(run)
        raw_artifacts = active.get("artifacts")
        waived_findings = (
            raw_artifacts.get("findings")
            if isinstance(raw_artifacts, dict)
            else None
        )
        critique = active.get("last_output")
        if not isinstance(critique, str):
            critique = ""
        transition = continue_with_waiver_handoff(
            run.session,
            handoff_id=handoff_id,
            phase=active_phase,
            feedback=feedback,
            note=note,
            decided_at=decided_at,
            findings=waived_findings,
            critique=critique,
        )
        # Durable record: persisted to session -> meta.json by
        # ``_persist_handoff_running_state`` so a fresh-process resume
        # (MCP/Web) rehydrates the waiver. Do NOT clear this key on
        # resume — downstream gates read it until the final gate passes.
        run.session["phase_handoff_waiver"] = transition.waiver
        # Runtime copy for the gates dispatched in THIS process.
        run.state.extras["phase_handoff_waiver"] = transition.waiver
        run.state.extras["phase_handoff_override"] = transition.override
        _persist_handoff_running_state(run)
        return PhaseHandoffResumeOutcome(
            profile=next_profile,
            completed_phases=completed,
            paused=False,
        )

    # action == "retry_feedback" — exhaustive by Literal narrowing.
    if not feedback.strip():
        raise RuntimeError(
            f"Cannot resume run: retry_feedback decision for "
            f"{handoff_id!r} is missing the feedback string."
        )
    if active.get("phase") == "review_changes":
        return apply_review_repair_handoff_retry(
            run=run,
            profile=profile,
            ctx=ctx,
            active=active,
            handoff_id=handoff_id,
            feedback=feedback,
            note=note,
            decided_at=decided_at,
        )
    plan_loop = find_plan_loop(profile)
    if plan_loop is None:
        raise RuntimeError(
            f"Cannot resume run: profile {getattr(profile, 'name', '?')!r} "
            "has no canonical plan loop, but the active handoff "
            f"{handoff_id!r} decided retry_feedback. The profile "
            "must contain a plan -> validate_plan loop for "
            "retry_feedback resume."
        )
    # Clear the prior active payload (status=running) before the retry
    # round so a re-launch without progress won't loop on the same
    # decision; the override + human_feedback markers carry the typed
    # plan-retry mode without parsing the paused phase string.
    transition = retry_feedback_handoff(
        run.session,
        handoff_id=handoff_id,
        mode=HandoffRetryMode.PLAN,
        feedback=feedback,
        note=note,
        decided_at=decided_at,
    )
    run.state.extras["phase_handoff_override"] = transition.override
    run.state.extras["human_feedback"] = transition.human_feedback
    # Rehydrate reviewer critique from the persisted active handoff
    # payload (last_output is the prior validate_plan output) or
    # from the session's validate_plan entry matching the active
    # round. Required for MCP/Web resume where in-memory
    # state.last_critique is empty after a fresh-process restart.
    prior_round_for_critique = active.get("round")
    if not isinstance(prior_round_for_critique, int):
        prior_round_for_critique = None
    last_output = active.get("last_output")
    if not isinstance(last_output, str):
        last_output = ""
    run.state.last_critique = (
        last_output
        or last_validate_plan_critique(
            run.session, round_n=prior_round_for_critique,
        )
        or ""
    )
    run.state.human_feedback = feedback

    _persist_handoff_running_state(run)

    prior_round = int(active.get("round", plan_loop.max_rounds) or 0)
    retry_round_n = prior_round + 1
    loop_max_rounds = int(
        active.get("loop_max_rounds", plan_loop.max_rounds)
        or plan_loop.max_rounds,
    )
    run.state.extras[plan_loop.round_extras_key] = retry_round_n
    run.state.extras[f"{plan_loop.round_extras_key}_max"] = loop_max_rounds
    run.state.extras[HUMAN_DIRECTED_FLAG_KEY] = True
    prev_active_key = run.state.extras.get("_active_loop_round_key")
    run.state.extras["_active_loop_round_key"] = (
        plan_loop.round_extras_key
    )
    try:
        for inner_step in plan_loop.steps:
            if run.state.halt:
                break
            run.state = _dispatch_via_fsm(
                inner_step,
                run.state,
                ctx,
                on_phase_start=run._on_phase_start,
                on_phase_end=run._on_phase_end,
            )
            if run.state.halt:
                break
            # After each inner step, check whether the handoff
            # policy on that step would fire for this human-directed
            # round (the retry round counts as the new "final" round
            # for the on-reject trigger).
            signal = build_phase_handoff_signal(
                inner_step, plan_loop, run.state, retry_round_n,
            )
            if signal is None:
                continue
            # New pause requested — store the signal for the
            # orchestrator's persistence tail to pick up.
            run.state.phase_handoff_request = signal
            run.state.stop(
                f"phase handoff requested: {signal.handoff_id}",
            )
            if on_round_end is not None:
                with contextlib.suppress(Exception):
                    on_round_end(plan_loop, retry_round_n, run.state)
            _persist_handoff_retry_metrics(run)
            return PhaseHandoffResumeOutcome(
                profile=profile,
                completed_phases=frozenset(),
                paused=True,
            )
    finally:
        run.state.extras.pop(HUMAN_DIRECTED_FLAG_KEY, None)
        if prev_active_key is None:
            run.state.extras.pop("_active_loop_round_key", None)
        else:
            run.state.extras["_active_loop_round_key"] = prev_active_key

    # No fresh handoff fired → loop is logically closed; strip it
    # from the dispatched profile and let the rest run.
    if on_round_end is not None:
        with contextlib.suppress(Exception):
            on_round_end(plan_loop, retry_round_n, run.state)
    _persist_handoff_retry_metrics(run)
    return PhaseHandoffResumeOutcome(
        profile=strip_plan_loop(profile),
        completed_phases=frozenset({"plan", "validate_plan"}),
        paused=False,
    )


# ── CI handoff-advice aggregate ───────────────────────────────────────────

#: ``run.state.extras`` slot carrying the in-memory CI advisor lifecycle counters
#: + last-advice fields. Read by the final DONE/HALTED summary (T4).
_CI_ADVICE_AGGREGATE_KEY = "_ci_agent_advice"


def _ci_advice_aggregate(run: Any) -> dict[str, Any]:
    """Get-or-create the ``_ci_agent_advice`` aggregate on ``run.state.extras``.

    Created lazily ONLY when the CI auto-retry path actually runs, so a disabled
    policy / interactive run never seeds the aggregate.
    """
    extras = run.state.extras
    agg = extras.get(_CI_ADVICE_AGGREGATE_KEY)
    if not isinstance(agg, dict):
        agg = {
            "retries": 0,
            "resolved": 0,
            "stopped": 0,
            "last_recommendation": "",
            "last_confidence": "",
            "last_findings_fingerprint": "",
            "scope_unchecked": False,
        }
        extras[_CI_ADVICE_AGGREGATE_KEY] = agg
    return agg


def _fingerprint_str(fingerprint: Any) -> str:
    """Stable string form of a findings fingerprint for the durable aggregate."""
    if not fingerprint:
        return ""
    return ";".join(sorted("|".join(part) for part in fingerprint))


def _record_ci_advice_fields(agg: dict[str, Any], outcome: Any) -> None:
    """Fold the per-call advisor fields into the aggregate (every CI call)."""
    agg["last_recommendation"] = outcome.last_recommendation
    agg["last_confidence"] = outcome.last_confidence
    agg["last_findings_fingerprint"] = _fingerprint_str(outcome.findings_fingerprint)
    agg["scope_unchecked"] = outcome.scope_unchecked


def _persist_ci_advice_aggregate(run: Any, agg: dict[str, Any]) -> None:
    """Flush the CI advisor aggregate into the durable run meta + save it.

    A paused CI stop returns without ever reaching DONE/HALTED finalization, so
    the aggregate must be made durable here: ``apply_phase_handoff_pause`` saved
    the session BEFORE the aggregate existed/updated, and ``save_session`` only
    writes ``run.session``. Mirroring the in-memory ``run.state.extras`` aggregate
    onto ``run.session`` (meta.json) and re-saving keeps the persisted
    paused-report and the in-memory view in sync. Best-effort: skipped when the
    run has no ``output_dir`` (the in-memory aggregate still carries the values).
    """
    run.session[_CI_ADVICE_AGGREGATE_KEY] = dict(agg)
    if run.output_dir:
        save_session(run.output_dir, run.session)


# ── interactive prompt loop ───────────────────────────────────────────────


def process_pending_phase_handoffs(
    run: Any,
    profile: Any,
    ctx: Any,
    *,
    on_round_end: Any = None,
) -> PhaseHandoffLoopResult:
    """Drain pending phase handoffs from ``run.state``.

    Replaces the inline ``while run.state.phase_handoff_request is not
    None`` block that used to live inside
    ``_dispatch_via_v2_profile``. Iterates the (already-extracted)
    pause + prompt + resume cycle until either:

    * the state machine exits the awaiting-handoff status,
    * the operator chooses halt,
    * a non-interactive transport or operator-aborted prompt forces a
      pause that persists.

    The wrapper handles the SDK audit-trail write through
    ``sdk.phase_handoff.phase_handoff_decide`` (lazy-imported because
    ``sdk.runner`` itself imports the project orchestrator and the
    top-level import here would create a circular graph).

    ``on_round_end`` is the round-end callback owned by the caller
    (currently a closure inside ``_dispatch_via_v2_profile``). When
    re-dispatch produces another handoff that satisfies its budget,
    the round trace flows through this callback.
    """
    # Carried across the bounded CI retry loop so an identical recurring P1/P2
    # finding is detected instead of looping (compared by T2's fingerprint).
    prev_fingerprint: frozenset[tuple[str, str, str]] | None = None
    while run.state.phase_handoff_request is not None:
        apply_phase_handoff_pause(run)

        signal = run.state.phase_handoff_request
        from pipeline.project import handoff_advice as _handoff_advice
        ci_retry_active = False
        agg: dict[str, Any] | None = None

        if not should_prompt_for_phase_handoff(
            no_interactive=run.no_interactive,
        ):
            # Non-interactive (CI): a policy-driven auto-retry through the SAME
            # decide + resume path a human ``retry_feedback`` uses, or a typed
            # stop. Heavy logic lives in the policy (T1) + CI sub-flow (T2)
            # modules; this is thin routing + aggregate accounting.
            from pipeline.project import handoff_advice_policy as _policy
            policy = _policy.resolve_handoff_advice_policy(run)
            if not (
                policy.auto_retry_with_agent
                and _handoff_advice.advice_actions_available(signal)
            ):
                return PhaseHandoffLoopResult(
                    profile=profile,
                    session=run.session,
                    paused=True,
                    continue_dispatch=False,
                    halted=False,
                )
            from pipeline.project import handoff_advice_ci as _ci

            agg = _ci_advice_aggregate(run)
            budget_remaining = policy.max_agent_retries - int(agg["retries"])
            ci_outcome = _ci.handle_ci_advice(
                run, signal, policy,
                budget_remaining=budget_remaining,
                prev_findings_fingerprint=prev_fingerprint,
            )
            _record_ci_advice_fields(agg, ci_outcome)
            if ci_outcome.findings_fingerprint:
                prev_fingerprint = ci_outcome.findings_fingerprint
            if ci_outcome.outcome == "stop":
                agg["stopped"] = int(agg["stopped"]) + 1
                _persist_ci_advice_aggregate(run, agg)
                if ci_outcome.state == "halt":
                    # Route the CI halt through the SAME handler-halt tail a
                    # gate-abort uses: set ``state.halt`` + clear the pending
                    # request and let the loop fall through to the caller's
                    # ``run.finalize()``, which renders the HALTED summary
                    # (including the ``Agent advice`` block) and clears the
                    # pending handoff via ``mark_run_halted``. No parallel halt
                    # path, no decision artifact (the advisor recommended halt).
                    run.state.phase_handoff_request = None
                    run.state.halt = True
                    run.state.halt_reason = "phase_handoff_halt"
                    run._dispatch_active = False
                    continue
                return PhaseHandoffLoopResult(
                    profile=profile,
                    session=run.session,
                    paused=True,
                    continue_dispatch=False,
                    halted=False,
                )
            # proceed: count the retry and flow the ci_agent decision through the
            # shared decide + resume path below — no parallel repair branch.
            agg["retries"] = int(agg["retries"]) + 1
            decision_input = ci_outcome.decision_input
            ci_retry_active = True
        else:
            # Advisory eligibility is policy: computed here (trigger + verdict +
            # retry_feedback + findings/last_output), never inside the pure prompt.
            advisory_available = _handoff_advice.advice_actions_available(signal)
            decision_input = prompt_phase_handoff_action(
                signal, advisory_available=advisory_available,
            )
            if isinstance(decision_input, _HandoffPromptAborted):
                warn(
                    "Interactive phase-handoff decision aborted; "
                    "leaving run paused for off-band resolution."
                )
                return PhaseHandoffLoopResult(
                    profile=profile,
                    session=run.session,
                    paused=True,
                    continue_dispatch=False,
                    halted=False,
                )

            # Advisory pseudo-action (5/advice or 6/retry_with_advice): the
            # advisor sub-flow runs the read-only advisor, persists the advice
            # artifact, and returns either an ordinary HandoffDecisionInput
            # (retry_feedback with a provenance note, or halt) to flow through the
            # EXISTING decide + resume path below, or None to redisplay the menu
            # (no decision written; the pause is already persisted, so the outer
            # loop re-prompts).
            if isinstance(decision_input, AdviceActionRequest):
                from pipeline.project import (
                    handoff_advice_dispatch as _advice_dispatch,
                )
                decision_input = _advice_dispatch._handle_advice_request(
                    run, signal, decision_input,
                )
                if decision_input is None:
                    continue

        # Audit-trail invariant (ADR 0031 § 5): every decision lands
        # through the SDK function MCP / Web / scripted CLI use.
        # Lazy import — sdk.runner pulls
        # ``pipeline.cross_project.orchestrator`` which pulls back
        # into ``pipeline.project_orchestrator``, so a top-level
        # import here creates a cycle.
        from sdk.errors import (
            InvalidPhaseHandoffState as _SDKInvalidPhaseHandoffState,
        )
        from sdk.phase_handoff import (
            phase_handoff_decide as _sdk_phase_handoff_decide,
        )

        runs_root = (
            run.output_dir.parent
            if run.output_dir is not None else None
        )
        try:
            _sdk_phase_handoff_decide(
                run.session_ts,
                signal.handoff_id,
                decision_input.action,
                feedback=decision_input.feedback,
                note=decision_input.note,
                runs_dir=runs_root,
                cwd=None,
            )
        except (ValueError, _SDKInvalidPhaseHandoffState) as exc:
            # ``print_error`` is CLI-leaf; emit via stderr directly so
            # this module stays out of the CLI dep graph. Stderr-bound
            # output passes stream=sys.stderr so auto-detect consults
            # stderr's TTY status — see Terminal color discipline rule
            # in orcho-core/CLAUDE.md.
            import sys
            body = (
                f"phase_handoff_decide({decision_input.action!r}) "
                f"rejected: {exc}"
            )
            print(
                f"{paint('Error:', C.RED, C.BOLD, stream=sys.stderr)} "
                f"{paint(body, C.RED, stream=sys.stderr)}",
                file=sys.stderr,
            )
            return PhaseHandoffLoopResult(
                profile=profile,
                session=run.session,
                paused=True,
                continue_dispatch=False,
                halted=False,
            )

        # Operator-facing confirmation: the SDK decision artifact is now
        # on disk and the orchestrator is about to apply the action
        # in-process. Surfacing the transition keeps the demo flow
        # legible — otherwise the extra plan round / continuation
        # starts with no acknowledgement that the decision landed.
        success(f"Decision recorded: {decision_input.action}")
        # retry_feedback pre/post banners are emitted by
        # ``apply_phase_handoff_resume_with_banners`` below so the
        # checkpoint/preflight resume path gets the same banners; only the
        # non-retry transition notes are printed here.
        if decision_input.action == PhaseHandoffAction.CONTINUE.value:
            print(paint(
                "  ↳ Continuing original profile after "
                "manual override...",
                C.GREY,
            ))
        elif decision_input.action == PhaseHandoffAction.HALT.value:
            print(paint("  ↳ Halting run synchronously...", C.GREY))

        if decision_input.action == PhaseHandoffAction.HALT.value:
            # SDK has already flipped ``meta.status="halted"`` +
            # cleared the active payload as part of the halt
            # transition. Sync the in-memory session to match so
            # finalize / save_session do not re-introduce a stale
            # ``awaiting_phase_handoff`` row.
            # No ``halted_at`` here: this in-process sync mirrors the SDK
            # decision that already flipped meta; preserving the current
            # no-timestamp shape avoids behavioural drift.
            mark_run_halted(run.session, halt_reason="phase_handoff_halt")
            if run._ckpt:
                run._ckpt.set_status(PipelineStatus.HALTED)
            if run.output_dir:
                save_session(run.output_dir, run.session)
            run._dispatch_active = False
            return PhaseHandoffLoopResult(
                profile=profile,
                session=run.session,
                paused=False,
                continue_dispatch=False,
                halted=True,
            )

        # ``continue`` / ``retry_feedback``: clear the runner-side
        # request, then let ``apply_phase_handoff_resume`` consume
        # the persisted active payload + freshly-written decision
        # artifact and apply action semantics in-process. For
        # ``retry_feedback`` the helper runs one extra plan round;
        # if that round itself fires a new handoff, the helper
        # stashes the signal back on ``run.state.phase_handoff_request``
        # and returns ``paused=True`` — this outer loop iterates,
        # persists the new pause, and prompts again.
        #
        # The runner sets ``state.halt`` + ``state.halt_reason`` when
        # the handoff trigger fires (it uses ``state.stop`` to break
        # out of the loop). Those carry over into ``finalize`` and
        # would mark the run as halted even though the operator
        # picked ``continue`` / ``retry_feedback``. Reset BEFORE
        # ``apply_phase_handoff_resume`` so ``retry_feedback``'s
        # extra round doesn't see a stale halt.
        run.state.phase_handoff_request = None
        run.state.halt = False
        run.state.halt_reason = ""
        prev_dispatch_active = bool(getattr(run, "_dispatch_active", False))
        run._dispatch_active = True
        try:
            resume_outcome = apply_phase_handoff_resume_with_banners(
                run, profile, ctx, on_round_end=on_round_end,
            )
        finally:
            run._dispatch_active = prev_dispatch_active
        # A ci_agent retry is RESOLVED when its extra round produced no fresh
        # rejected/incomplete handoff; otherwise the loop re-evaluates the new
        # pause under the (now smaller) budget on the next iteration.
        if (
            ci_retry_active
            and agg is not None
            and not resume_outcome.paused
            and run.state.phase_handoff_request is None
        ):
            agg["resolved"] = int(agg["resolved"]) + 1
            _persist_ci_advice_aggregate(run, agg)
        if resume_outcome.paused:
            # Inner extra round triggered a new handoff; loop will
            # see the new request on the next iteration top.
            continue
        profile = resume_outcome.profile
        if profile is None:
            # ``plan`` profile after ``continue`` — nothing else to
            # dispatch; signal the caller to finalize.
            run._dispatch_active = False
            return PhaseHandoffLoopResult(
                profile=None,
                session=run.session,
                paused=False,
                continue_dispatch=True,
                halted=False,
            )

        # Re-dispatch the remaining profile (plan loop stripped on
        # ``continue`` / approved-retry, full profile when retry
        # left additional handoffs ahead). Refresh
        # ``completed_phases`` from the checkpoint store so the
        # second dispatch doesn't re-execute phases the first leg
        # already committed.
        from pipeline.runtime import run_profile

        completed_phases = set(resume_outcome.completed_phases)
        if run._ckpt is not None:
            try:
                _prior = run._ckpt.load(run.session_ts)
                prior_completed = set(_prior.completed or ())
                prior_completed -= set(resume_outcome.invalidated_phases)
                completed_phases = (
                    prior_completed
                    | set(resume_outcome.completed_phases)
                )
            except Exception as exc:
                raise RuntimeError(
                    "Cannot safely resume in-process: checkpoint state "
                    f"could not be loaded for run {run.session_ts!r}."
                ) from exc

        run._dispatch_active = True
        # Arm the per-phase gate hooks for the resume re-dispatch. A resume runs
        # in a fresh process where ``_gate_profile`` starts unset, so without
        # this the post-implement verification gates silently never fire on a
        # resumed run and delivery blocks on the receipts they would have
        # materialized (single-sourced with the fresh-dispatch arming).
        from pipeline.project.gate_repair import arm_gate_context
        arm_gate_context(run, profile, ctx)
        try:
            run_profile(
                profile,
                run.state,
                run.registry,
                on_phase_start=run._on_phase_start,
                on_phase_end=run._on_phase_end,
                on_round_end=on_round_end,
                ctx=ctx,
                completed_phases=completed_phases,
            )
        except Exception as exc:
            current_phase = run.state.extras.get(
                "_current_phase",
            ) or "<v2-dispatch>"
            run._record_phase_failure(
                exc, fallback_phase=current_phase,
            )
            raise
        finally:
            run._dispatch_active = False
        # Loop iterates: if the re-dispatched profile fired another
        # handoff (rare — would need a non-bypass policy on a
        # post-plan phase, currently unsupported), we handle it the
        # same way. Otherwise the top of the while clears.

    return PhaseHandoffLoopResult(
        profile=profile,
        session=run.session,
        paused=False,
        continue_dispatch=True,
        halted=False,
    )


__all__ = [
    "PhaseHandoffLoopResult",
    "PhaseHandoffResumeOutcome",
    "apply_phase_handoff_pause",
    "apply_phase_handoff_resume",
    "apply_phase_handoff_resume_with_banners",
    "apply_review_repair_handoff_retry",
    "critique_is_empty",
    "find_plan_loop",
    "find_repair_loop",
    "last_review_critique",
    "last_validate_plan_critique",
    "load_handoff_decision_validated",
    "merge_review_feedback",
    "process_pending_phase_handoffs",
    "strip_plan_loop",
    "strip_repair_loop",
]
