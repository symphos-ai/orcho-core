"""Runtime dispatch + hypothesis prelude for the single-project pipeline.

Moved out of ``pipeline.project_orchestrator`` per ADR 0042 Phase E.
Three concerns live here:

* **Profile-shape helpers** — ``resolve_phase_models``,
  ``resolve_mode_gates``, ``profile_contains_phase``,
  ``first_phase_step``, ``plan_hypothesis_step``,
  ``hypothesis_attempts_for_step``, ``hypothesis_format_for_step``.
  Pure inspection of the v2 ``Profile`` recipe used by the call
  site in ``run_pipeline`` (gates) and the dispatch path (hypothesis
  step lookup).
* **Phase banners + outcome surfacing** — ``emit_phase_banner``,
  ``emit_phase_log_end``, ``resolve_round_n``,
  ``followup_banner_suffix``, ``print_handoff_outcome``,
  ``_PHASE_BANNER_CONFIG``, ``_HANDOFF_OUTCOME_PREFIX`` /
  ``_HANDOFF_OUTCOME_COLOR``. Operator-visible terminal output
  produced during dispatch.
* **Dispatch + runtime overrides** — ``dispatch_via_v2_profile``
  (Phase 5c step 2 entry, ~240 LoC), ``apply_runtime_max_rounds``
  (CLI ``--max-rounds`` honoured against profile-declared loops),
  ``run_hypothesis_block`` (Phase 5d-step-4 pre-PLAN gut-check),
  and ``apply_followup_session_seeds`` /
  ``_FOLLOWUP_ROLE_TO_AGENT_ATTR`` (CHAIN follow-up runtime session
  seeding, punted from Phase C because seeding touches per-phase
  agents).

The orchestrator re-imports each public name under its legacy
underscore-prefixed alias to keep existing call sites byte-identical;
tests that patch ``pipeline.project_orchestrator.maybe_run_hypothesis``
were updated in the same commit to point at the new namespace.

The ``run`` parameter is typed ``Any`` rather than ``_PipelineRun``
because the two modules are peers under ``pipeline.project.*`` and
the orchestrator-side dispatch call site doesn't need the static
hint. Phase F moved ``_PipelineRun`` into ``pipeline.project.run``;
the annotation could tighten via a ``TYPE_CHECKING`` peer import,
but the runtime callable is duck-typed across enough variants
(real ``_PipelineRun`` in production, ``_Run`` stand-in classes in
``tests/unit/pipeline/runtime/test_loop_round_callback.py``, the
``test_review_findings`` fixture) that the loose annotation is
honest about what the function actually accepts.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agents.registry import PhaseAgentConfig
from core.infra import config
from core.io.ansi import C, paint
from core.observability import phases as _pk
from core.observability.logging import log_phase, warn
from pipeline.control.handoff_labels import render_round_label
from pipeline.engine import maybe_run_hypothesis, save_session
from pipeline.project.correction_route_display import (
    format_correction_route_decision,
)
from pipeline.project.resume_phase_summary import format_resume_phase_summary
from pipeline.project.types import PresentationPolicy
from pipeline.runtime import PipelineState
from pipeline.runtime.handoff import HandoffOutcome, HandoffOutcomeKind
from pipeline.runtime.runner import RESUME_SKIP_REASON

# ── follow-up role mapping ────────────────────────────────────────────────

# Role name (handler key) → ``PhaseAgentConfig`` attribute holding the
# matching agent slot. Used for follow-up runtime-session seeding and
# the per-phase banner suffix that surfaces a resumed parent session.
_FOLLOWUP_ROLE_TO_AGENT_ATTR: dict[str, str] = {
    "plan": "plan_agent",
    "validate_plan": "validate_plan_agent",
    "implement": "implement_agent",
    "review_changes": "review_changes_agent",
    "repair_changes": "repair_changes_agent",
    "final_acceptance": "final_acceptance_agent",
}


def apply_followup_session_seeds(
    phase_config: PhaseAgentConfig,
    seeds: Mapping[str, str],
) -> int:
    """Seed each per-phase agent with the parent's runtime session id.

    Walks ``_FOLLOWUP_ROLE_TO_AGENT_ATTR`` and, for every role present in
    ``seeds``, sets ``agent.session_id`` plus arms
    ``_followup_resume_pending`` on the matching slot. The next
    ``agent.invoke`` consumes the flag, forces ``--resume <sid>``, and
    clears it (see ``ClaudeAgent.invoke`` / ``CodexAgent.invoke``).
    Missing slots and missing seeds are skipped silently.

    Returns the number of slots actually seeded — used by the per-phase
    banner suffix (``followup_banner_suffix``) and tests.

    CHAIN-mode caveat. ``_phase_repair_changes`` in CHAIN mode dispatches
    to ``implement_agent`` instead of ``repair_changes_agent`` (see
    ``pipeline/phases/builtin/:_resolve_fix_runtime_config``). So:

    * A ``repair_changes`` seed applied here lands on
      ``repair_changes_agent`` but is **inert** for a CHAIN repair —
      that handler never invokes its assigned slot.
    * The parent's chained implement session is the source of truth for
      CHAIN repair context; it's already seeded onto
      ``implement_agent``, and the chained implement session naturally
      continues into the follow-up's repair invocations.

    The banner suffix follows the actual dispatched slot (CHAIN repair
    surface points back at the implement banner), so the operator never
    sees "repair resumes parent repair session" claim when the run is
    really continuing the chained implement session. HYBRID / STATELESS
    profiles invoke ``repair_changes_agent`` directly and honour the
    repair_changes seed normally.

    This function does NOT need to know the active session mode —
    seed every slot, let the dispatcher decide which agent invokes.
    """
    count = 0
    for role, attr in _FOLLOWUP_ROLE_TO_AGENT_ATTR.items():
        sid = seeds.get(role)
        agent = getattr(phase_config, attr, None)
        if not sid or agent is None:
            continue
        agent.session_id = sid
        agent._followup_resume_pending = True
        count += 1
    return count


# ── profile-shape helpers ─────────────────────────────────────────────────


def resolve_phase_models(
    phase_config: PhaseAgentConfig | None,
    fallback_code_model: str,
) -> tuple[str, str, str, str, str]:
    """Pick (plan, build, fix, repair_escalation, review) models in that order.
    ``phase_config`` overrides everything when supplied; otherwise defaults
    come from config + the caller's ``fallback_code_model``.

    Every slot now goes through :func:`config.phase_model` so the
    ``_config/config.local.json`` overrides reach the run header
    banner. Before this, only ``plan`` and ``repair_escalation`` read
    AppConfig; ``implement`` / ``repair_changes`` fell back to the
    raw ``fallback_code_model`` (the CLI ``--model`` default) and
    ``review`` used the module-level ``CODEX_MODEL`` constant — both
    bypass paths ignored ``config.local.json``. The agent
    instantiation in ``_synthesize_phase_config`` already applied
    AppConfig correctly, so this fixes the operator-facing banner
    that was advertising the wrong model.
    """
    if phase_config is not None:
        return (
            phase_config.plan_agent.model,
            phase_config.implement_agent.model,
            phase_config.repair_changes_agent.model,
            phase_config.repair_escalation_agent.model,
            phase_config.review_changes_agent.model,
        )
    return (
        config.phase_model("plan", "claude-opus-4-8[1m]"),
        config.phase_model("implement", fallback_code_model),
        config.phase_model("repair_changes", fallback_code_model),
        config.phase_model("repair_escalation", "claude-opus-4-8[1m]"),
        config.phase_model("review_changes", config.CODEX_MODEL),
    )


def profile_contains_phase(profile: Any, phase_name: str) -> bool:
    """Return True when a v2 Profile recipe contains ``phase_name``.

    Phase 6 originally derived gates from profile names. That was enough
    for shipped profiles, but it would accidentally run the hypothesis
    prelude for a custom build-only profile. The profile shape is the
    source of truth; names are labels.
    """
    from pipeline.runtime import LoopStep, PhaseStep

    def _entry_has(entry: Any) -> bool:
        if isinstance(entry, PhaseStep):
            return entry.phase == phase_name
        if isinstance(entry, LoopStep):
            return any(_entry_has(inner) for inner in entry.steps)
        return False

    return any(_entry_has(entry) for entry in getattr(profile, "steps", ()) or ())


def first_phase_step(profile: Any, phase_name: str):
    """Return the first matching ``PhaseStep`` in profile order."""
    from pipeline.runtime import LoopStep, PhaseStep

    for entry in getattr(profile, "steps", ()) or ():
        if isinstance(entry, PhaseStep):
            if entry.phase == phase_name:
                return entry
        elif isinstance(entry, LoopStep):
            for inner in entry.steps:
                if inner.phase == phase_name:
                    return inner
    return None


def plan_hypothesis_step(profile: Any, *, override_enabled: bool | None):
    """Resolve the plan step whose profile-owned hypothesis prelude should run.

    ``None`` means skip. CLI/API overrides may force the prelude on or off,
    but the profile shape is the default source of truth.
    """
    plan_step = first_phase_step(profile, "plan")
    if plan_step is None:
        return None
    if override_enabled is False:
        return None
    if override_enabled is True:
        return plan_step
    hypothesis = getattr(plan_step, "hypothesis", None)
    return (
        plan_step
        if hypothesis is not None and int(getattr(hypothesis, "attempts", 0) or 0) > 0
        else None
    )


def hypothesis_attempts_for_step(plan_step) -> int:
    hypothesis = getattr(plan_step, "hypothesis", None)
    if hypothesis is None:
        return 1
    return max(1, int(getattr(hypothesis, "attempts", 0) or 0))


def hypothesis_format_for_step(plan_step) -> str | None:
    hypothesis = getattr(plan_step, "hypothesis", None)
    if hypothesis is not None and getattr(hypothesis, "format", None):
        return hypothesis.format
    prompt = getattr(plan_step, "prompt", None)
    return getattr(prompt, "format", None)


def resolve_mode_gates(
    profile: Any,
    max_rounds: int,
) -> tuple[bool, bool, bool, int]:
    """Apply profile shape → derive (do_plan, do_build, do_review, max_rounds).

    Phase 6 replaces the ``PipelineMode + skip_plan`` matrix with a direct
    v2 Profile lookup. The profile recipe is the source of truth: custom
    profiles that omit PLAN must not trigger the pre-PLAN hypothesis block
    just because their name is not one of the shipped scoped variants.

    Built-in ``plan`` and ``review`` variants still clamp ``max_rounds`` to
    0 because their loop budget is profile-owned rather than runtime
    review/fix budget.
    """
    variant = getattr(profile, "variant", None)
    do_plan = profile_contains_phase(profile, "plan")
    do_build = profile_contains_phase(profile, "implement")
    do_review = any(
        profile_contains_phase(profile, phase)
        for phase in ("review_changes", "repair_changes", "final_acceptance")
    )
    # Scoped profile names ("plan" / "review") are user-facing variant
    # labels; the phase-rename in ADR 0022 did NOT touch them.
    if variant in ("plan", "review"):
        max_rounds = 0
    return do_plan, do_build, do_review, max_rounds


# ── runtime overrides ─────────────────────────────────────────────────────


def apply_runtime_max_rounds(profile, *, max_rounds: int):
    """Override the review/fix LoopStep's ``max_rounds`` in a v2
    ``Profile`` with the runtime caller's ``max_rounds`` param.

    The plan loop's budget now lives entirely in the profile's
    declared ``LoopStep.max_rounds``; there is no runtime override
    for it. The repair/review loop keeps a runtime knob because
    ``--max-rounds`` is the operator's per-run intent (e.g. "let me
    iterate more on the implement→review→repair cycle for this
    single task") and is not tied to handoff semantics.

    Mapping:
      * round_extras_key == "repair_round" → use ``max_rounds``
      * other keys → leave the profile bound untouched

    When the runtime bound is 0 → drop the LoopStep entirely.

    Returns a new ``Profile`` instance (frozen dataclass — ``replace``
    used). When no override applies, returns the input profile
    unchanged so identity comparison still holds for snapshot tests.
    """
    from dataclasses import replace as _replace

    from pipeline.runtime import LoopStep, Profile

    if not isinstance(profile, Profile):
        return profile  # legacy PipelineProfile passthrough

    new_steps: list = []
    mutated = False
    for entry in profile.steps:
        if not isinstance(entry, LoopStep):
            new_steps.append(entry)
            continue
        if entry.round_extras_key != "repair_round":
            new_steps.append(entry)
            continue

        override = max_rounds
        if override <= 0:
            mutated = True
            continue  # drop loop entirely
        if override == entry.max_rounds:
            new_steps.append(entry)
            continue
        new_steps.append(_replace(entry, max_rounds=override))
        mutated = True

    if not mutated:
        return profile
    return _replace(profile, steps=tuple(new_steps))


# ── phase banner / log-end rendering ──────────────────────────────────────


_PHASE_BANNER_CONFIG: dict[str, dict[str, Any]] = {
    # name → {label, color, phase_kind, header}
    "plan":             {"header": "PLAN",             "color": "C.MAGENTA", "phase_kind": "PLAN",
                         "label_template": "PLAN -- architect creates MD artifacts (round {round_n})"},
    "validate_plan":    {"header": "VALIDATE_PLAN",    "color": "C.YELLOW",  "phase_kind": "VALIDATE_PLAN",
                         "label_template": "VALIDATE PLAN -- reviewer audits the plan (round {round_n})"},
    "implement":        {"header": "IMPLEMENT",        "color": "C.BLUE",    "phase_kind": "IMPLEMENT",
                         "label_template": "IMPLEMENT -- developer applies the change"},
    "review_changes":   {"header": "REVIEW_CHANGES",   "color": "C.YELLOW",  "phase_kind": "REVIEW_CHANGES",
                         "label_template": "review_changes -- Round {round_n}"},
    "repair_changes":   {"header": "REPAIR_CHANGES",   "color": "C.GREEN",   "phase_kind": "REPAIR_CHANGES",
                         "label_template": "REPAIR CHANGES -- Round {round_n}"},
    "final_acceptance": {"header": "FINAL_ACCEPTANCE", "color": "C.CYAN",    "phase_kind": "FINAL_ACCEPTANCE",
                         "label_template": "FINAL ACCEPTANCE -- closing gate"},
    "correction_triage": {"header": "CORRECTION_TRIAGE", "color": "C.MAGENTA", "phase_kind": "CORRECTION_TRIAGE",
                          "label_template": "CORRECTION TRIAGE -- classify the release blockers"},
}


def resolve_round_n(name: str, st: PipelineState) -> int:
    """Helper: read the round counter relevant to ``name`` for
    banner / log entry rendering. Mirrors ``_round_n_for_adapter``."""
    active_key = st.extras.get("_active_loop_round_key")
    if isinstance(active_key, str) and active_key:
        v = st.extras.get(active_key)
        if v is not None:
            return int(v)
    by_phase = {
        "plan":           "plan_round",
        "validate_plan":  "plan_round",
        "review_changes": "repair_round",
        "repair_changes": "repair_round",
    }
    return int(st.extras.get(by_phase.get(name, "loop_round"), 1) or 1)


def emit_phase_banner(
    name: str, st: PipelineState, *, terminal: bool = True,
) -> None:
    """Phase 5d-fixup: emit progress-log banner for the start of a
    phase under v2 dispatch. Mirrors the ``banner(...)`` calls the
    legacy ``run_*_loop`` methods used to fire.

    ``name`` is the lowercase handler-registry key (``plan`` /
    ``validate_plan`` / …); it flows into the event-store context as
    ``phase_key`` so downstream emits carry the canonical machine key.

    ADR 0046 Phase C — split render from log:
      * ``terminal=True`` (default, CLI/SDK): print the banner header
        AND write the progress.log line + emit ``phase.start`` event.
        Byte-identical to the legacy ``banner(...)`` behaviour.
      * ``terminal=False`` (SILENT path from ``run_project_pipeline``):
        skip the ``print(...)`` half only — ``log_phase`` (file + event
        writer, ADR 0046 stop #9) still fires so the silent boundary
        keeps observability parity with TERMINAL. The function is split
        inline instead of calling ``banner(...)`` because ``banner``
        always prints; the silent path needs the log half without the
        stdout half.
    """
    cfg = _PHASE_BANNER_CONFIG.get(name)
    if cfg is None:
        return
    round_n = resolve_round_n(name, st)
    label = cfg["label_template"].format(round_n=round_n)
    # ADR 0039 extension: the post-repair re-verify pass fires inside
    # the same loop iteration as the original review_changes — both
    # share ``round_n``. Without a distinguishing suffix the banner
    # reads as two identical "Round N" entries back to back. The
    # runner sets ``_review_reverify_resume`` on state.extras around
    # the re-dispatch; surface it here so operators can tell the
    # validating pass apart from the original review.
    if name == "review_changes" and st.extras.get(
        "_review_reverify_resume",
    ):
        label = f"{label} (re-verify)"
    suffix = followup_banner_suffix(name, st)
    if suffix:
        label = f"{label}{suffix}"
    color_name = cfg["color"].split(".", 1)[1]
    color = getattr(C, color_name, C.RESET)
    pk = getattr(_pk, cfg["phase_kind"], None)
    if terminal:
        from core.io.transcript import render_phase_header
        print(render_phase_header(cfg["header"], label, color=color))
    log_phase(
        cfg["header"], label,
        phase_kind=pk, attempt=round_n,
        phase_key=name, round=round_n,
    )


def followup_banner_suffix(name: str, st: PipelineState) -> str:
    pc = st.phase_config
    if pc is None:
        return ""
    attr = _FOLLOWUP_ROLE_TO_AGENT_ATTR.get(name)
    if attr is None:
        return ""
    if name == "repair_changes":
        try:
            from agents.protocols import SessionMode as _SessionMode
            from pipeline.phases.builtin import _resolve_fix_runtime_config

            cfg = _resolve_fix_runtime_config(st)
            if cfg.get("effective_mode") is _SessionMode.CHAIN:
                attr = "implement_agent"
        except Exception:
            attr = "repair_changes_agent"
    agent = getattr(pc, attr, None)
    if not getattr(agent, "_followup_resume_pending", False):
        return ""
    sid = getattr(agent, "session_id", None)
    if not sid:
        return ""
    return f"  (follow-up: resuming parent session {sid[:8]}...)"


def emit_phase_log_end(
    name: str, st: PipelineState, *, terminal: bool = True,
    phases: Mapping[str, Any] | None = None,
) -> None:
    """Phase 5d-fixup: emit progress-log END entry paired with banner.
    Acceptance tests assert START + END pairs around each phase header.

    ADR 0046 Phase C — split render from log:
      * ``terminal=True`` (default): print the ``  ↳ skipped: …`` grey
        line when a skip reason is present AND write the progress.log
        END line + emit ``phase.end`` event.
      * ``terminal=False`` (SILENT): skip the ``print(...)`` half only.
        ``log_phase("END", ...)`` still fires so silent callers see the
        same START/END event pairing TERMINAL callers see (ADR 0046
        stop #9 — ``log_phase`` is file + event, never gated).
    """
    cfg = _PHASE_BANNER_CONFIG.get(name)
    if cfg is None:
        return
    round_n = resolve_round_n(name, st)
    label = cfg["label_template"].format(round_n=round_n)
    pk = getattr(_pk, cfg["phase_kind"], None)
    log_entry = st.phase_log.get(name, {})
    skipped_reason = ""
    if isinstance(log_entry, dict):
        skipped_reason = str(log_entry.get("skipped") or "")
    if skipped_reason and terminal:
        # ADR 0046 Phase C (site 9): the grey "↳ skipped: …" line is a
        # CLI courtesy chip; the structural signal is the skip_reason
        # mirrored into the ``phase.end`` event via ``outcome`` below.
        #
        # Resume-skip enrichment: when (and only when) the skip is the
        # resume reason, replace the bare chip with a per-phase summary
        # derived from the already-rehydrated ``session['phases']`` — the
        # operator lost the live output, so surface what the phase produced.
        # Strictly gated on ``RESUME_SKIP_REASON`` (every other skip line
        # stays byte-identical) and best-effort (missing/partial/unknown →
        # the summary is ``None`` and we fall back to the bare chip).
        summary = (
            format_resume_phase_summary(name, phases)
            if skipped_reason == RESUME_SKIP_REASON
            else None
        )
        if summary is not None:
            print(paint(
                f"  ↳ {name} — {summary}  (skipped on resume)", C.GREY,
            ))
        else:
            print(paint(f"  ↳ skipped: {skipped_reason}", C.GREY))
    # Outcome string: shape inspected by tests but free-form. Keep the
    # minimal "ok" for completed phases and surface skip reasons for
    # intentionally empty banners.
    if st.halt and st.extras.get("_current_phase") == name:
        outcome = f"halted: {st.halt_reason or 'halt'}"
    else:
        outcome = f"skipped: {skipped_reason}" if skipped_reason else "ok"
    # Correction-route decision line (ADR 0086 presentation): at the
    # correction_triage END, surface the full route decision — kind, skip
    # phases, or halt reason/blockers — both on the terminal (below the END
    # chip) and folded into the progress.log END outcome so the whole
    # decision lands at this exact event, no new phase event. Strict no-op
    # for any profile without a triage record (byte-identical otherwise).
    if name == "correction_triage":
        display = format_correction_route_decision(
            st.phase_log.get("correction_triage")
        )
        if display is not None:
            outcome = f"{outcome} | {display.text}"
            if terminal:
                tone = C.YELLOW if display.halted else C.CYAN
                prefix = "⚠" if display.halted else "•"
                print(paint(f"  {prefix} {display.text}", tone))
    log_phase(
        cfg["header"], label, "END", outcome,
        phase_kind=pk, attempt=round_n,
        phase_key=name, round=round_n,
    )


# ── handoff-outcome operator surface ──────────────────────────────────────

# Visual prefix per outcome kind. Three kinds get a colour because the
# operator should be able to tell at a glance whether the policy fired,
# deferred, bypassed, or hit a malformed verdict:
#
# * ``fired``     — yellow ⚠ — a pause / prompt is imminent;
# * ``deferred``  — grey  · — policy active but auto-retry budget kept it quiet;
# * ``bypassed``  — green ✓ — policy active and approved verdict means no pause;
# * ``no_verdict``— red   ✗ — defensive: phase log has no usable ``approved``
#                  bool, runtime keeps iterating but the operator should know.
_HANDOFF_OUTCOME_PREFIX: dict[str, str] = {
    HandoffOutcomeKind.FIRED.value:      "⚠",
    HandoffOutcomeKind.DEFERRED.value:   "·",
    HandoffOutcomeKind.BYPASSED.value:   "✓",
    HandoffOutcomeKind.NO_VERDICT.value: "✗",
}
_HANDOFF_OUTCOME_COLOR: dict[str, str] = {
    HandoffOutcomeKind.FIRED.value:      C.YELLOW,
    HandoffOutcomeKind.DEFERRED.value:   C.GREY,
    HandoffOutcomeKind.BYPASSED.value:   C.GREEN,
    HandoffOutcomeKind.NO_VERDICT.value: C.RED,
}

# Operator-readable decision phrase per outcome kind. The structured
# round label (``render_round_label``) carries the round identity; this
# phrase carries what the policy decided. Built here rather than reusing
# ``outcome.message`` because that runtime-built string embeds a raw
# ``round N/M`` fraction, which is impossible (``N > M``) for the
# one-shot human-directed retry round.
_HANDOFF_OUTCOME_PHRASE: dict[str, str] = {
    HandoffOutcomeKind.FIRED.value:      "pausing for human decision",
    HandoffOutcomeKind.DEFERRED.value:   "auto-retry budget remains — no pause",
    HandoffOutcomeKind.BYPASSED.value:   "approved verdict — no human input required",
    HandoffOutcomeKind.NO_VERDICT.value: "no verdict this round — runtime keeps iterating",
}


def print_handoff_outcome(outcome: HandoffOutcome) -> None:
    """Surface every per-round handoff decision to the operator.

    Wired into ``run_profile(on_handoff_outcome=...)``. Fires once per
    inner-step round whose ``handoff`` policy is non-bypass — that
    includes rounds where no pause happens. Operators see a single line
    confirming the policy was active and what it decided, instead of
    silent rounds for ``human_feedback_on_reject`` with an approved
    verdict or remaining auto-retry budget.

    The round identity is rendered through
    :func:`pipeline.control.handoff_labels.render_round_label` so a
    one-shot human-directed retry never prints an impossible ``N/M``
    fraction. The ``FIRED`` line is informational only; the existing
    pause/prompt machinery in :func:`dispatch_via_v2_profile` still
    drives the interactive flow and writes the persisted artifact.
    """
    kind = outcome.kind.value
    prefix = _HANDOFF_OUTCOME_PREFIX.get(kind, "·")
    color = _HANDOFF_OUTCOME_COLOR.get(kind, C.GREY)
    label = render_round_label(
        phase=outcome.phase,
        round=outcome.round,
        loop_max_rounds=outcome.loop_max_rounds,
        rejected_again=(
            outcome.round > outcome.loop_max_rounds
            and outcome.approved is False
        ),
    )
    phrase = _HANDOFF_OUTCOME_PHRASE.get(kind, outcome.message)
    print(paint(
        f"  {prefix} Handoff ({kind}): {label} — {phrase}",
        color,
    ))


# ── hypothesis prelude ────────────────────────────────────────────────────


def run_hypothesis_block(run: Any, plan_step=None) -> None:
    """Phase 5d step 4: HYPOTHESIS pre-PLAN gut-check.

    Replaces the deleted ``_PipelineRun.run_hypothesis_loop`` method.
    Runs ``maybe_run_hypothesis`` with the planner+QA agents,
    populates ``run.research_hypothesis`` + ``run.session["phases"]
    ["hypothesis"]`` via HypothesisAdapter, and emits the
    HYPOTHESIS banner / log_phase end.

    Hypothesis is a plan-step prelude: profiles opt in with
    ``PhaseStep.hypothesis`` and the prelude reuses the plan step's
    prompt detail style.
    """
    plan_agent_for_research = run.phase_config.plan_agent
    qa_agent_for_research   = run.phase_config.validate_plan_agent
    # ADR 0046 Phase C (site 12) — split render from log: print the
    # banner header only under TERMINAL, but always fire
    # ``log_phase("HYPOTHESIS", …, "START", …)`` so the silent boundary
    # still emits a matched ``phase.start`` for the unconditional
    # ``log_phase(..., "END", …)`` call below. Otherwise SILENT runs
    # produced an unpaired ``phase.end`` event in ``events.jsonl``.
    _hypothesis_terminal = (
        getattr(run, "_presentation", PresentationPolicy.TERMINAL)
        is PresentationPolicy.TERMINAL
    )
    if _hypothesis_terminal:
        from core.io.transcript import render_phase_header
        print(render_phase_header(
            "HYPOTHESIS",
            "HYPOTHESIS -- fast pre-plan gut-check",
            color=C.MAGENTA,
        ))
    log_phase(
        "HYPOTHESIS", "HYPOTHESIS -- fast pre-plan gut-check",
        phase_kind=_pk.HYPOTHESIS, attempt=1,
    )
    run.research_hypothesis, attempts = maybe_run_hypothesis(
        task=run.task,
        cwd=run.git_cwd,
        codemap=run.codemap,
        dry_run=run.dry_run,
        plan_agent=plan_agent_for_research,
        qa_agent=qa_agent_for_research,
        prompt_spec=getattr(plan_step, "prompt", None),
        hypothesis_format=hypothesis_format_for_step(plan_step),
        override_enabled=True,
        override_max_attempts=hypothesis_attempts_for_step(plan_step),
    )
    if attempts:
        run.state.phase_log["hypothesis"] = {
            "attempts": attempts,
            "approved": bool(run.research_hypothesis),
        }
        run._session_adapters.get("hypothesis").write(
            "hypothesis", run.state, run.session,
        )
        # When QA rejected every attempt, the reviewer's findings are
        # still valuable as *negative* planning context. Stash them on
        # state.extras so the PLAN handler can append them as rejected-
        # hypothesis feedback (distinct from an approved direction).
        if not run.research_hypothesis:
            run.state.extras["hypothesis_attempts"] = attempts
    log_phase(
        "HYPOTHESIS",
        "HYPOTHESIS -- fast pre-plan gut-check",
        "END",
        ("direction validated" if run.research_hypothesis
         else "all rejected" if attempts
         else "skipped"),
        phase_kind=_pk.HYPOTHESIS,
        attempt=1,
    )


# ── main dispatch entry ───────────────────────────────────────────────────


def dispatch_via_v2_profile(run: Any, profile) -> dict:
    """Phase 5c step 2 dispatch: drive the run through ``run_profile``
    using the orchestrator's existing on_phase_start / on_phase_end
    callbacks. Adapters fire automatically as part of
    ``_on_phase_end``; handlers self-contain their per-round data
    (Phase 5c step 1).

    Runs the profile-owned hypothesis prelude when the active plan step
    declares ``hypothesis`` attempts, then dispatches the regular profile.

    Phase 3 cutover: pause semantics live in the generic phase-handoff
    machinery, not in the validate_plan handler. Profiles declare a
    ``handoff`` policy on ``validate_plan`` (e.g. ``human_feedback_on_reject``
    on ``advanced``/``enterprise``, ``human_feedback_always`` on ``plan``).
    The loop runner raises ``state.phase_handoff_request`` when the policy
    triggers; after dispatch the orchestrator detects that signal and calls
    ``apply_phase_handoff_pause`` which writes ``meta.phase_handoff`` +
    ``meta.status="awaiting_phase_handoff"``, emits
    ``phase.handoff_requested``, sets checkpoint status to
    ``AWAITING_PHASE_HANDOFF`` and saves the session.

    Phase 5c step 6: register ``on_round_end`` callback so the runtime
    fires ``save_session`` after each LoopStep round — mirrors legacy
    ``run_review_fix_loop`` per-round checkpoint behaviour. Without
    this, a crash mid-loop would lose all completed rounds since
    finalize.

    Phase 5d-fixup: respect runtime ``max_rounds`` parameter as an
    override for the v2 review/fix LoopStep declared in profile JSON.
    Legacy ``run_review_fix_loop`` honoured it (``range(1, max_rounds+1)``);
    v2 profile dispatch must too. The plan loop's budget lives entirely
    in the profile (``LoopStep.max_rounds``) — there is no runtime
    override for it.
    """
    from pipeline.project.handoff import (
        apply_phase_handoff_pause,
        apply_phase_handoff_resume_with_banners,
        find_plan_loop,
        process_pending_phase_handoffs,
    )
    from pipeline.runtime import run_profile

    # Phase 5d-fixup: override LoopStep.max_rounds with runtime params
    # to mirror legacy semantics. Profile JSON declares the upper bound;
    # CLI / API params can clamp it down (or to 0 to skip the loop
    # entirely). Recursive walk handles nested LoopStep though Phase 1
    # forbids nesting.
    profile = apply_runtime_max_rounds(
        profile,
        max_rounds=run.max_rounds,
    )
    run._done_summary_profile = profile

    # Phase 5e-5 substep 6c: orchestrator-private dispatch-active guard.
    # Replaces the substep-6b ``state.extras["_v2_dispatch_active"]``
    # cross-cutting channel — the flag only ever guarded
    # orchestrator-internal banner / mock-plan side-effects, never a
    # handler concern, so it lives on the run object.
    run._dispatch_active = True
    # Phase 5d step 1: legacy ``run_review_fix_loop`` initialised
    # ``session["phases"]["rounds"] = []`` BEFORE iterating. Mirror that
    # so v2 dispatch produces a ``rounds`` key even on profiles where the
    # review/fix loop never runs (e.g. SCOPED review profile).
    run.session.setdefault("phases", {}).setdefault("rounds", [])
    def _on_round_end(loop_step, round_n: int, state) -> None:
        """Phase 5c step 6: per-LoopStep-round checkpoint hook.

        Persist session.json after each round so a mid-loop crash
        leaves the most recent completed round on disk. Mirrors the
        ``save_session(...)`` calls at lines 1273 + 1368 of legacy
        ``run_review_fix_loop``.

        Also bumps the metrics rounds counter once per loop iteration —
        legacy ``run_review_fix_loop`` called ``add_round()`` per round;
        v2 dispatch lost that wiring, so ``metrics.json.total_rounds``
        was always 0 and the history table showed ``Rnd: 0`` even for
        runs with multiple review/fix iterations. Only the repair-loop
        rounds count (matches the legacy "Rnd" column's meaning); the
        plan/validate loop's rounds stay in ``plan.attempt`` per round
        record and aren't double-counted here.
        """
        if loop_step.round_extras_key in ("repair_round", "loop_round"):
            run._metrics.add_round()
        if run.output_dir:
            try:
                save_session(run.output_dir, run.session)
            except Exception as exc:  # pragma: no cover — defensive
                # ADR 0046 Phase C (site 13): the mid-loop save_session
                # warn is defensive observability — checkpoint failure
                # is real but rare. Under SILENT the library caller
                # consumes events + exceptions; gate the warn so the
                # "zero stdout/stderr" contract holds even on this
                # rare path. The structural failure detection
                # (next round's save / finalize) is unchanged.
                if getattr(run, "_presentation", PresentationPolicy.TERMINAL) is PresentationPolicy.TERMINAL:
                    warn(
                        f"v2 dispatch: mid-loop save_session failed "
                        f"(round {round_n}, key={loop_step.round_extras_key}): {exc}"
                    )

    # Phase 5e-5 substep 4: build LifecycleContext with FSM stages
    # 8-10 wired (adapter / checkpoint / metrics). _on_phase_end is
    # slimmed to handle only banner-END + mock plan write + timer
    # cleanup (substep 6 migrates those too).
    from pipeline.lifecycle import default_lifecycle_context

    ctx = default_lifecycle_context(
        phase_registry=run.registry,
        session_adapter_registry=run._session_adapters,
        provider=run._provider,
        run_config={
            "max_rounds": run.max_rounds,
            # FSM adapter stage reads ``run_config["session"]`` — wire
            # the orchestrator's session dict so SessionAdapter writes
            # land there (matches legacy ``_on_phase_end`` adapter call).
            "session": run.session,
        },
    )
    ctx.on_checkpoint = run._fsm_checkpoint
    ctx.on_metrics = run._fsm_metrics

    # Phase 4 cutover: consume an active phase-handoff payload + matching
    # decision artifact before the main dispatch. The helper may execute
    # a single human-directed plan -> validate_plan retry round, strip
    # the plan loop from the profile (for ``continue`` or approved
    # ``retry_feedback``), or raise a fresh pause when the retry round
    # is itself rejected.
    resume_outcome = apply_phase_handoff_resume_with_banners(
        run, profile, ctx, on_round_end=_on_round_end,
    )
    if resume_outcome.paused:
        # retry_feedback round triggered a new handoff — persist + exit.
        apply_phase_handoff_pause(run)
        run._dispatch_active = False
        return run.session
    profile = resume_outcome.profile

    # ADR 0081 ``on_resume`` gate hook: on a checkpoint resume, evaluate the
    # required gates scheduled for ``on_resume`` before continuing dispatch.
    # No-op without a contract or an on_resume gate.
    if getattr(run, "checkpoint_resume", False) and profile is not None:
        from pipeline.project.gate_repair import run_gate_hook

        on_resume_outcome = run_gate_hook(run, profile, ctx, hook="on_resume")
        if on_resume_outcome.active and on_resume_outcome.paused:
            apply_phase_handoff_pause(run)
            run._dispatch_active = False
            return run.session
        if on_resume_outcome.active and on_resume_outcome.halted:
            run._dispatch_active = False
            return run.session

    # When the stripped profile has no remaining steps (e.g. the
    # single-loop ``plan`` profile after a ``continue`` decision), there
    # is no dispatch to perform — go straight to ``finalize`` so the
    # operator's manual override surfaces as ``done`` instead of looping
    # back through ``run_profile`` (which would raise on an empty steps
    # tuple).
    if profile is None:
        run._dispatch_active = False
        return run.finalize()

    # Resume contract: when ``run._ckpt`` records phases as completed
    # from a prior run with the same ``session_ts``, the runtime must
    # NOT re-execute them on this pass. The checkpoint store is the
    # source of truth — a fresh run's checkpoint is empty, so this is
    # a no-op for fresh dispatch. See ``run_profile``'s
    # ``completed_phases`` contract for the dispatch-side semantics.
    completed_phases: set[str] = set(resume_outcome.completed_phases)

    try:
        if run._ckpt is not None:
            try:
                _prior = run._ckpt.load(run.session_ts)
                # ADR 0073 §5: UNION the checkpoint's completed phases with the
                # resume outcome's, rather than overwriting. An implement
                # handoff accept marks ``implement`` completed via
                # ``resume_outcome.completed_phases``; the checkpoint (written
                # before the pause) does NOT list implement, so a plain
                # overwrite would re-execute it after the operator already
                # accepted the delivery. For retry_feedback, the checkpoint may
                # contain phases that are now stale (implement + downstream);
                # remove those before the union so the retry actually runs.
                prior_completed = set(_prior.completed or ())
                prior_completed -= set(resume_outcome.invalidated_phases)
                completed_phases = (
                    prior_completed
                    | set(resume_outcome.completed_phases)
                )
            except Exception as exc:
                raise RuntimeError(
                    "Cannot safely resume: checkpoint state could not be "
                    f"loaded for run {run.session_ts!r}. Refusing to "
                    "re-execute phases without completed-phase evidence."
                ) from exc

        # Hypothesis prelude is part of the plan loop's fresh-run
        # pre-work. On checkpoint resumes the checkpoint is the context
        # handoff, not a provider-session continuation; starting a new
        # stateless hypothesis here asks the model to "continue" without
        # the prior plan/review context. On phase-handoff resumes the plan
        # loop has either been stripped or marked completed, so the
        # hypothesis is already obsolete there as well.
        if (
            not getattr(run, "checkpoint_resume", False)
            and not completed_phases
            and find_plan_loop(profile) is not None
        ):
            hypothesis_step = plan_hypothesis_step(
                profile,
                override_enabled=getattr(run, "hypothesis_enabled", None),
            )
            if run.do_plan and hypothesis_step is not None:
                run_hypothesis_block(run, hypothesis_step)

        # ADR 0046 Phase C (site 11): ``print_handoff_outcome`` is a
        # terminal-only courtesy line. Under SILENT we pass ``None``
        # so ``run_profile`` skips the callback entirely; the
        # structural per-round handoff record still lands in
        # ``state.phase_log`` + events.
        _on_handoff_outcome = (
            print_handoff_outcome
            if getattr(run, "_presentation", PresentationPolicy.TERMINAL)
            is PresentationPolicy.TERMINAL
            else None
        )
        # ADR 0081 Stage 4: stash the resolved profile + ctx so the per-phase
        # gate hooks (``run._on_phase_pre`` / ``run._on_phase_end``) can build
        # the executable gate plan and dispatch repair_changes. ``before_phase``
        # / ``before_delivery`` fire via ``on_phase_pre`` (pre-handler, so they
        # can pre-empt the phase); ``after_phase`` fires via ``on_phase_end``.
        from pipeline.project.gate_repair import arm_gate_context
        arm_gate_context(run, profile, ctx)
        run_profile(
            profile,
            run.state,
            run.registry,
            on_phase_start=run._on_phase_start,
            on_phase_end=run._on_phase_end,
            on_round_end=_on_round_end,
            ctx=ctx,
            completed_phases=completed_phases,
            on_handoff_outcome=_on_handoff_outcome,
            # ``getattr`` keeps duck-typed run stand-ins (runtime tests) working:
            # without ``_on_phase_pre`` the pre-phase seam is simply inert.
            on_phase_pre=getattr(run, "_on_phase_pre", None),
        )
    except Exception as exc:
        # Phase 5d-fixup: preserve legacy ``_safe_phase`` behaviour —
        # any handler exception that propagated out of dispatch must
        # write a structured ``failed`` record to session + checkpoint.
        # ``_safe_phase`` already records single-handler failures, but
        # exceptions raised outside any handler (or that escape via
        # ``raise``) need the same tail. Re-raise after recording so
        # callers / tests still see the original error type.
        # Phase tracking is via ``state.extras["_current_phase"]`` set
        # by ``_on_phase_start`` — fallback ``"<v2-dispatch>"`` only
        # used when the exception fires outside any phase (rare).
        current_phase = run.state.extras.get(
            "_current_phase",
        ) or "<v2-dispatch>"
        run._record_phase_failure(
            exc, fallback_phase=current_phase,
        )
        raise
    finally:
        run._dispatch_active = False
        from pipeline.project.gate_repair import disarm_gate_context
        disarm_gate_context(run)

    # Phase 3 cutover: detect a generic phase-handoff pause raised by
    # the loop runner. The runner sets ``state.phase_handoff_request``
    # when a non-bypass handoff policy triggers (currently only
    # validate_plan inside the plan loop). The orchestrator owns the
    # meta/session/event/checkpoint side; the SDK owns decision
    # artifacts on resume.
    #
    # Two paths:
    #
    # * **Non-interactive** (CI, MCP, piped invocations, ``--no-interactive``,
    #   stdin/stdout not a TTY) — persist + return; ``main()`` exits rc=4
    #   and the operator decides later via SDK / MCP / Web.
    # * **Interactive** (real TTY + ``--no-interactive`` not set) — persist
    #   the pause so the SDK can write a decision artifact, prompt the
    #   operator at the keyboard for ``continue`` / ``retry_feedback`` /
    #   ``halt``, record the decision through the same
    #   ``sdk.phase_handoff_decide`` function the other transports use
    #   (audit-trail invariant), then apply the action in-process via the
    #   existing ``apply_phase_handoff_resume`` mechanism. The run
    #   continues or terminates inside this same subprocess — no rc=4
    #   exit, no respawn under ``--resume`` required.
    # ADR 0042 Phase D: the inline ``while
    # run.state.phase_handoff_request is not None`` block (formerly
    # ~170 lines here) is now wrapped in
    # ``process_pending_phase_handoffs``. The wrapper returns a typed
    # ``PhaseHandoffLoopResult`` with exactly one of paused /
    # continue_dispatch / halted true; the caller branches without
    # owning the loop body any more.
    # ADR 0081 Stage 4 gate hooks now fire *inside* run_profile via the
    # per-phase callbacks: ``before_phase`` / ``before_delivery`` through
    # ``run._on_phase_pre`` (pre-handler, so they can pre-empt a phase) and
    # ``after_phase`` (implement = critical repair flow) through
    # ``run._on_phase_end``. A gate that paused set ``state.phase_handoff_request``
    # (run_profile broke before review); ``process_pending_phase_handoffs`` below
    # persists + resolves that pause exactly like a loop-driven handoff. A gate
    # that aborted set ``state.halt`` with no request → falls through to
    # ``finalize`` as a halted run (same tail as any handler-side halt).
    loop_result = process_pending_phase_handoffs(
        run, profile, ctx, on_round_end=_on_round_end,
    )
    if loop_result.paused or loop_result.halted:
        return run.session
    return run.finalize()
