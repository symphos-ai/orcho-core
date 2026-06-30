# SPDX-License-Identifier: Apache-2.0
"""ADR 0112 §5 scope-expansion phase-handoff triggers — end-to-end coverage.

Both scope-expansion handoff triggers ride the existing ADR 0038 phase-handoff
lifecycle:

* ``scope_expansion:participant_add:<repo>`` — a participant-add promotion;
* ``scope_expansion:out_of_plan`` — a generic out-of-plan blocker.

Covered, parametrized over both triggers:

* signal generation via ``build_scope_expansion_handoff_signal`` with the right
  trigger / handoff_id / full action set;
* both support-check sites accept the ``final_acceptance`` seam without dropping
  — the handoff.py phase guard and the runner ``_validate_handoff_support``;
* ``phase_handoff_decide`` accepts each opaque trigger and applies the action,
  with exact-payload idempotency by handoff_id + action;
* ``request_handoff_advice`` returns a typed ``HandoffAdviceResult`` with a
  recommended action for each trigger (under ``--mock``);
* the trigger is preserved byte-identically in the ``meta.phase_handoff``
  payload and in the durable advice artifact.

Follows the patterns in ``tests/sdk/test_phase_handoff.py`` /
``tests/sdk/test_request_handoff_advice.py`` /
``tests/unit/pipeline/runtime/test_handoff_trigger.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agents.runtimes._strategy import MockAgentProvider
from pipeline.plugins import PluginConfig
from pipeline.run_state.handoff import build_handoff_payload
from pipeline.runtime import (
    LoopStep,
    PhaseHandoffPolicy,
    PhaseHandoffType,
    PhaseStep,
    PipelineState,
    Profile,
)
from pipeline.runtime.handoff import (
    SCOPE_EXPANSION_HANDOFF_PHASE,
    SCOPE_EXPANSION_OUT_OF_PLAN_TRIGGER,
    PhaseHandoffRequested,
    build_scope_expansion_handoff_signal,
    scope_expansion_participant_add_trigger,
)
from pipeline.runtime.runner import _validate_handoff_support
from sdk import (
    HandoffAdviceEvidence,
    HandoffAdviceResult,
    PhaseHandoffDecision,
    list_handoff_advice,
    phase_handoff_decide,
    request_handoff_advice,
)
from sdk.errors import InvalidPhaseHandoffState

# (id, trigger) for the two scope-expansion handoff variants.
_PARTICIPANT_ADD = scope_expansion_participant_add_trigger("orcho-mcp")
_TRIGGERS = [
    ("participant_add", _PARTICIPANT_ADD),
    ("out_of_plan", SCOPE_EXPANSION_OUT_OF_PLAN_TRIGGER),
]


def _signal(trigger: str, *, round_n: int = 1) -> PhaseHandoffRequested:
    return build_scope_expansion_handoff_signal(
        trigger=trigger,
        round_n=round_n,
        artifacts={
            "findings": [
                {"id": "S1", "severity": "P2", "title": "out-of-plan change",
                 "body": "sdk/new_wire.py changed outside the declared plan"},
            ],
        },
        last_output="out-of-plan scope expansion needs operator sanction",
    )


def _payload_from_signal(signal: PhaseHandoffRequested) -> dict[str, Any]:
    """Build the canonical meta.phase_handoff payload from a signal."""
    return build_handoff_payload(
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


def _seed_paused_run(
    tmp_path: Path,
    payload: dict[str, Any],
    run_id: str = "20260629_120000_aaaaaa",
) -> tuple[Path, str, Path]:
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    run_dir = runs / run_id
    run_dir.mkdir()
    meta: dict[str, Any] = {
        "task": "Ship the feature",
        "project": str(project),
        "model": "claude-opus-4-8",
        "profile": "feature",
        "status": "awaiting_phase_handoff",
        "phases": {},
        "phase_handoff": payload,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    return runs, run_id, run_dir


# ── signal generation ────────────────────────────────────────────────────────


@pytest.mark.parametrize(("name", "trigger"), _TRIGGERS, ids=[t[0] for t in _TRIGGERS])
def test_build_signal_carries_trigger_and_full_actions(
    name: str, trigger: str,
) -> None:
    signal = _signal(trigger)
    assert signal is not None
    assert signal.trigger == trigger
    assert signal.phase == SCOPE_EXPANSION_HANDOFF_PHASE
    assert signal.handoff_id == f"{SCOPE_EXPANSION_HANDOFF_PHASE}:{trigger}:1"
    # The operator action set keeps continue_with_waiver available but omits
    # retry_feedback: the terminal final_acceptance seam has no plan/repair loop
    # to retry into (see ``_apply_scope_expansion_handoff_resume``).
    assert signal.available_actions == (
        "continue", "halt", "continue_with_waiver",
    )
    assert "retry_feedback" not in signal.available_actions
    assert signal.type is PhaseHandoffType.HUMAN_FEEDBACK_ALWAYS


def test_participant_add_trigger_preserves_repo() -> None:
    assert scope_expansion_participant_add_trigger("orcho-web") == (
        "scope_expansion:participant_add:orcho-web"
    )


def test_non_scope_trigger_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="scope_expansion:"):
        build_scope_expansion_handoff_signal(trigger="rejected")


# ── support-check site 1: the handoff.py phase guard ─────────────────────────


@pytest.mark.parametrize(("name", "trigger"), _TRIGGERS, ids=[t[0] for t in _TRIGGERS])
def test_supported_seam_phase_not_dropped(name: str, trigger: str) -> None:
    # final_acceptance is the supported scope-expansion seam → signal returned.
    assert build_scope_expansion_handoff_signal(trigger=trigger) is not None


@pytest.mark.parametrize(("name", "trigger"), _TRIGGERS, ids=[t[0] for t in _TRIGGERS])
def test_unsupported_seam_phase_is_dropped(name: str, trigger: str) -> None:
    # An unsupported phase is dropped (None), mirroring build_phase_handoff_signal.
    assert build_scope_expansion_handoff_signal(
        trigger=trigger, phase="repair_changes",
    ) is None


# ── support-check site 2: the runner _validate_handoff_support matrix ────────


def test_runner_support_accepts_bare_final_acceptance_handoff() -> None:
    # The scope-expansion seam passes the runner support matrix as a bare
    # top-level step (no enclosing loop) — not dropped / not rejected.
    entries = (
        PhaseStep(
            phase=SCOPE_EXPANSION_HANDOFF_PHASE,
            handoff=PhaseHandoffPolicy(type=PhaseHandoffType.HUMAN_FEEDBACK_ALWAYS),
        ),
    )
    # Does not raise.
    _validate_handoff_support(entries, "scope_expansion_seam")


# ── support-check site: decide accepts each opaque trigger ───────────────────


@pytest.mark.parametrize(("name", "trigger"), _TRIGGERS, ids=[t[0] for t in _TRIGGERS])
def test_decide_accepts_trigger_and_applies_action(
    tmp_path: Path, name: str, trigger: str,
) -> None:
    signal = _signal(trigger)
    payload = _payload_from_signal(signal)
    # Trigger is byte-identical in the persisted meta.phase_handoff payload.
    assert payload["trigger"] == trigger
    runs, run_id, run_dir = _seed_paused_run(tmp_path, payload)

    result = phase_handoff_decide(
        run_id, signal.handoff_id, "continue",
        note="operator sanctioned the scope expansion",
        runs_dir=runs, cwd=None,
    )
    assert isinstance(result, PhaseHandoffDecision)
    assert result.handoff_id == signal.handoff_id
    assert result.phase == SCOPE_EXPANSION_HANDOFF_PHASE
    assert result.action == "continue"

    # meta.phase_handoff still carries the exact trigger after the decision.
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["phase_handoff"]["trigger"] == trigger

    # Exact-payload idempotency by handoff_id + action.
    again = phase_handoff_decide(
        run_id, signal.handoff_id, "continue",
        note="operator sanctioned the scope expansion",
        runs_dir=runs, cwd=None,
    )
    assert again.decided_at == result.decided_at


@pytest.mark.parametrize(("name", "trigger"), _TRIGGERS, ids=[t[0] for t in _TRIGGERS])
def test_decide_halt_applies_for_trigger(
    tmp_path: Path, name: str, trigger: str,
) -> None:
    signal = _signal(trigger)
    runs, run_id, run_dir = _seed_paused_run(
        tmp_path, _payload_from_signal(signal),
    )
    result = phase_handoff_decide(
        run_id, signal.handoff_id, "halt",
        note="operator halted on the scope expansion",
        runs_dir=runs, cwd=None,
    )
    assert result.action == "halt"
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "halted"


# ── support-check site: advice accepts each opaque trigger ───────────────────


@pytest.mark.parametrize(("name", "trigger"), _TRIGGERS, ids=[t[0] for t in _TRIGGERS])
def test_advice_returns_typed_recommendation_for_trigger(
    tmp_path: Path, name: str, trigger: str,
) -> None:
    signal = _signal(trigger)
    runs, run_id, run_dir = _seed_paused_run(
        tmp_path, _payload_from_signal(signal),
    )

    result = request_handoff_advice(
        run_id, signal.handoff_id, runs_dir=runs, cwd=None,
        provider=MockAgentProvider(),
    )

    assert isinstance(result, HandoffAdviceResult)
    assert result.handoff_id == signal.handoff_id
    assert result.phase == SCOPE_EXPANSION_HANDOFF_PHASE
    # A typed recommended action is returned (the opaque trigger is accepted).
    assert result.recommended_action
    assert result.recommended_action in (
        "continue", "halt", "continue_with_waiver",
    )

    # The durable advice artifact landed and preserves the trigger byte-identically.
    assert result.advice_artifact.startswith("phase_handoff_advice/")
    artifact = json.loads(
        (run_dir / result.advice_artifact).read_text(encoding="utf-8"),
    )
    assert artifact["trigger"] == trigger


# ── T4: MCP-visibility determination + hermetic in-core mock-smoke ───────────
#
# DETERMINATION (recorded here; transcribed into the ADR by T5):
#   BOTH scope-expansion triggers (scope_expansion:participant_add:<repo> and
#   scope_expansion:out_of_plan) are OPAQUE — they introduce NO new MCP-visible
#   surface. Evidence for this determination:
#     * No new typed wire field/type: ``docs/sdk_schema.json`` is unmodified by
#       this change (the phase-handoff ``trigger`` and the ``HandoffAdviceCall``
#       ``trigger`` are pre-existing opaque ``str`` fields; only new *values*
#       flow through them, not a new field/type).
#     * No new action enum value: ``phase_handoff_decide`` still validates the
#       same four actions; the trigger string is never validated by decide.
#     * No change to the MCP-visible return shapes: ``HandoffAdviceResult`` (the
#       ``orcho_handoff_advice`` payload) carries no ``trigger`` field and is
#       unchanged; ``decide``/``advice`` were already source-agnostic.
#     * The handoff_advice evidence slice derives its ``trigger`` label from the
#       verdict (``_trigger_from_verdict`` → ``rejected``/``incomplete``), so the
#       opaque ``scope_expansion:*`` string never even reaches the MCP-visible
#       slice — it lives only in the in-core durable artifact and the
#       (pre-existing, opaque) ``meta.phase_handoff`` payload.
#   => Branch (a) of the §5 MCP-validation rule applies: NO orcho-mcp surface or
#      E2E smoke is required, and orcho-mcp is NOT modified. An in-core hermetic
#      mock-smoke (below) discharges D for both triggers. (If a future change
#      added a client-visible field/condition, branch (b) would require a paired
#      orcho-mcp surface + E2E mock-smoke in the same change, or halt-as-blocked.)


@pytest.mark.parametrize(("name", "trigger"), _TRIGGERS, ids=[t[0] for t in _TRIGGERS])
def test_mock_smoke_scope_expansion_handoff_decide_advice_evidence(
    tmp_path: Path, name: str, trigger: str,
) -> None:
    # Hermetic in-core smoke: drive a scope_expansion HANDOFF through the full
    # advice + decide flow under MockAgentProvider (mock=True, no real model
    # calls), then confirm it surfaces in the handoff_advice evidence slice.
    signal = _signal(trigger)
    runs, run_id, run_dir = _seed_paused_run(
        tmp_path, _payload_from_signal(signal),
    )

    # 1) advice — read-only advisory pass, no model call.
    advice = request_handoff_advice(
        run_id, signal.handoff_id, runs_dir=runs, cwd=None,
        provider=MockAgentProvider(),
    )
    assert isinstance(advice, HandoffAdviceResult)
    assert advice.recommended_action  # typed recommendation produced

    # 2) decide — operator sanctions the scope expansion (continue).
    decision = phase_handoff_decide(
        run_id, signal.handoff_id, "continue",
        note="operator sanctioned the scope expansion",
        runs_dir=runs, cwd=None,
    )
    assert decision.action == "continue"

    # 3) visible in evidence (slice handoff_advice): a call for this advice exists.
    evidence = list_handoff_advice(run_id, runs_dir=runs, cwd=None)
    assert isinstance(evidence, HandoffAdviceEvidence)
    matching = [c for c in evidence.calls if c.advice_artifact == advice.advice_artifact]
    assert matching, "scope-expansion advice not visible in handoff_advice evidence"
    call = matching[0]
    assert call.recommended_action

    # Opacity check: the MCP-visible slice exposes the verdict-derived trigger
    # label (``rejected``), NOT the opaque scope_expansion string — no new wire
    # value escapes. The opaque trigger survives only in the in-core artifact.
    assert not call.trigger.startswith("scope_expansion:")
    artifact = json.loads(
        (run_dir / advice.advice_artifact).read_text(encoding="utf-8"),
    )
    assert artifact["trigger"] == trigger


# ── F1 fix: resume-dispatch after the operator decision ──────────────────────
#
# decide()/advice() above prove the SDK accepts each opaque trigger; these drive
# the ACTUAL resume through ``apply_phase_handoff_resume`` so the scope-expansion
# handoff closes/continues the terminal ``final_acceptance`` pause instead of
# mis-routing into the plan loop (the F1 review finding: a final_acceptance
# handoff fell through to ``strip_plan_loop`` + plan/validate_plan completed +
# plan rehydrate). The profile below carries a canonical plan loop on purpose —
# a mis-route would strip it and surface in the assertions.


def _scope_resume_profile() -> Profile:
    return Profile(
        name="feature",
        kind="advanced",
        description="full pipeline with a plan loop + terminal acceptance",
        steps=(
            LoopStep(
                steps=(
                    PhaseStep(phase="plan"),
                    PhaseStep(phase="validate_plan"),
                ),
                until="validate_plan.approved",
                max_rounds=2,
                round_extras_key="plan_round",
            ),
            PhaseStep(phase="implement"),
            PhaseStep(phase=SCOPE_EXPANSION_HANDOFF_PHASE),
        ),
    )


def _seed_scope_decision(
    run_dir: Path,
    *,
    handoff_id: str,
    action: str,
    feedback: str | None = None,
    note: str | None = None,
    decided_at: str = "2026-06-29T12:00:00+00:00",
) -> None:
    from sdk.phase_handoff import safe_handoff_id

    decisions = run_dir / "phase_handoff_decisions"
    decisions.mkdir(parents=True, exist_ok=True)
    (decisions / f"{safe_handoff_id(handoff_id)}.json").write_text(
        json.dumps(
            {
                "run_id":     run_dir.name,
                "handoff_id": handoff_id,
                "phase":      SCOPE_EXPANSION_HANDOFF_PHASE,
                "action":     action,
                "feedback":   feedback,
                "note":       note,
                "decided_at": decided_at,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _scope_resume_run(run_dir: Path, state: Any, payload: dict[str, Any]) -> Any:
    return SimpleNamespace(
        output_dir=run_dir,
        session={
            "status":        "awaiting_phase_handoff",
            "phases":        {},
            "phase_handoff": payload,
        },
        _ckpt=None,
        _metrics=None,
        state=state,
    )


def _scope_resume_state(tmp_path: Path) -> Any:
    return PipelineState(
        task="t", project_dir=str(tmp_path), plugin=PluginConfig(),
    )


def _expected_completed(trigger: str) -> frozenset[str]:
    # out_of_plan is raised AT final_acceptance (after it ran) → the resume skips
    # the already-run terminal phase. participant_add is raised EARLY by the
    # promotion seam (final_acceptance has NOT run) → the resume reports no
    # completed phase so the run re-walks, the promotion seam re-fires via its
    # decision-artifact idempotency, and the real terminal gate still runs.
    if trigger.startswith("scope_expansion:participant_add:"):
        return frozenset()
    return frozenset({SCOPE_EXPANSION_HANDOFF_PHASE})


@pytest.mark.parametrize(("name", "trigger"), _TRIGGERS, ids=[t[0] for t in _TRIGGERS])
def test_resume_continue_closes_final_acceptance_not_plan_loop(
    tmp_path: Path, name: str, trigger: str,
) -> None:
    from pipeline.project.handoff import apply_phase_handoff_resume, find_plan_loop
    from pipeline.project.resume_artifacts import RESUME_PLAN_REQUIRED_KEY

    signal = _signal(trigger)
    payload = _payload_from_signal(signal)
    run_dir = tmp_path / "20260629_120000_scope"
    run_dir.mkdir()
    _seed_scope_decision(
        run_dir, handoff_id=signal.handoff_id, action="continue",
        note="operator sanctioned the scope expansion",
    )
    state = _scope_resume_state(tmp_path)
    run = _scope_resume_run(run_dir, state, payload)
    profile = _scope_resume_profile()

    outcome = apply_phase_handoff_resume(run, profile, None)

    assert outcome.paused is False
    # Trigger-specific completed set, but NEVER plan/validate_plan (the F1
    # mis-route): out_of_plan skips the already-run final_acceptance;
    # participant_add reports nothing completed so the terminal gate still runs.
    assert outcome.completed_phases == _expected_completed(trigger)
    assert "plan" not in outcome.completed_phases
    assert "validate_plan" not in outcome.completed_phases
    # The plan loop is untouched (no strip_plan_loop): the profile is the same
    # object and still carries its canonical plan loop.
    assert outcome.profile is profile
    assert find_plan_loop(outcome.profile) is not None
    # No plan-loop resume artifacts leaked in (the mis-route would set these).
    assert RESUME_PLAN_REQUIRED_KEY not in state.extras
    assert state.parsed_plan is None
    # The pause is closed and the override records a bare continue.
    assert run.session["status"] == "running"
    assert "phase_handoff" not in run.session
    override = state.extras["phase_handoff_override"]
    assert override["action"] == "continue"
    assert override["feedback"] is None
    # A bare continue does NOT persist a waiver.
    assert "phase_handoff_waiver" not in run.session
    assert "phase_handoff_waiver" not in state.extras


@pytest.mark.parametrize(("name", "trigger"), _TRIGGERS, ids=[t[0] for t in _TRIGGERS])
def test_resume_continue_with_waiver_persists_waiver(
    tmp_path: Path, name: str, trigger: str,
) -> None:
    from pipeline.project.handoff import apply_phase_handoff_resume, find_plan_loop

    signal = _signal(trigger)
    payload = _payload_from_signal(signal)
    run_dir = tmp_path / "20260629_120100_scope"
    run_dir.mkdir()
    _seed_scope_decision(
        run_dir, handoff_id=signal.handoff_id, action="continue_with_waiver",
        feedback="accepted: the out-of-plan export is the task's stated goal",
    )
    state = _scope_resume_state(tmp_path)
    run = _scope_resume_run(run_dir, state, payload)
    profile = _scope_resume_profile()

    outcome = apply_phase_handoff_resume(run, profile, None)

    assert outcome.paused is False
    assert outcome.completed_phases == _expected_completed(trigger)
    # Still no plan-loop strip.
    assert outcome.profile is profile
    assert find_plan_loop(outcome.profile) is not None
    # A durable waiver lands in both the session (fresh-process resume) and the
    # runtime extras (gates dispatched in-process), keyed to this handoff/phase.
    waiver = state.extras["phase_handoff_waiver"]
    assert run.session["phase_handoff_waiver"] == waiver
    assert waiver["handoff_id"] == signal.handoff_id
    assert waiver["phase"] == SCOPE_EXPANSION_HANDOFF_PHASE
    assert waiver["waiver_text"].startswith("accepted:")
    override = state.extras["phase_handoff_override"]
    assert override["action"] == "continue_with_waiver"
    assert run.session["status"] == "running"
    assert "phase_handoff" not in run.session


def test_resume_continue_with_waiver_requires_feedback(tmp_path: Path) -> None:
    from pipeline.project.handoff import apply_phase_handoff_resume

    signal = _signal(SCOPE_EXPANSION_OUT_OF_PLAN_TRIGGER)
    payload = _payload_from_signal(signal)
    run_dir = tmp_path / "20260629_120200_scope"
    run_dir.mkdir()
    _seed_scope_decision(
        run_dir, handoff_id=signal.handoff_id, action="continue_with_waiver",
        feedback="   ",
    )
    state = _scope_resume_state(tmp_path)
    run = _scope_resume_run(run_dir, state, payload)

    with pytest.raises(RuntimeError, match="operator verdict"):
        apply_phase_handoff_resume(run, _scope_resume_profile(), None)


def test_decide_rejects_retry_feedback_for_scope_expansion(tmp_path: Path) -> None:
    # retry_feedback is omitted from the scope-expansion action set, so the SDK
    # decide gate refuses it before any decision artifact is written — the
    # operator cannot route a terminal scope-expansion handoff into a plan retry.
    signal = _signal(SCOPE_EXPANSION_OUT_OF_PLAN_TRIGGER)
    runs, run_id, _run_dir = _seed_paused_run(
        tmp_path, _payload_from_signal(signal),
    )
    with pytest.raises(InvalidPhaseHandoffState, match="available_actions"):
        phase_handoff_decide(
            run_id, signal.handoff_id, "retry_feedback",
            feedback="reduce the scope", runs_dir=runs, cwd=None,
        )


def test_resume_retry_feedback_rejected_defensively(tmp_path: Path) -> None:
    # Defense in depth: even a hand-edited decision artifact carrying
    # retry_feedback (bypassing the decide gate) must not mis-route into the
    # plan loop — the resume arm rejects it with a clear error.
    from pipeline.project.handoff import apply_phase_handoff_resume

    signal = _signal(SCOPE_EXPANSION_OUT_OF_PLAN_TRIGGER)
    payload = _payload_from_signal(signal)
    run_dir = tmp_path / "20260629_120300_scope"
    run_dir.mkdir()
    _seed_scope_decision(
        run_dir, handoff_id=signal.handoff_id, action="retry_feedback",
        feedback="reduce the scope",
    )
    state = _scope_resume_state(tmp_path)
    run = _scope_resume_run(run_dir, state, payload)

    with pytest.raises(RuntimeError, match="retry_feedback is not a supported"):
        apply_phase_handoff_resume(run, _scope_resume_profile(), None)
