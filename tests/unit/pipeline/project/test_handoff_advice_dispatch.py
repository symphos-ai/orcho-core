"""Orchestrator dispatch of the advisory pseudo-actions (T3).

Covers ``pipeline.project.handoff_advice._handle_advice_request`` (the advisory
sub-flow the dispatch branch delegates to) and the wiring inside
``process_pending_phase_handoffs``:

* advice + back writes no decision artifact (the advisor never decides);
* retry_with_advice / apply produce an ordinary ``retry_feedback``
  ``HandoffDecisionInput`` whose ``note`` references the actually-written
  advice artifact (including the divergent-advice and operator-edit cases);
* non-retry recommendations and unconfirmed low-confidence return to the menu;
* the resume path is the SAME ``apply_phase_handoff_resume_with_banners`` the
  human ``retry_feedback`` decision uses (no parallel branch);
* advisor errors (exception / unparseable) never break the loop.

The advisor itself is monkeypatched (no real provider); stdin/stdout are
injected so the follow-up sub-menu is scriptable.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

from agents.entities import SubTask
from pipeline.control.handoff_prompt import AdviceActionRequest
from pipeline.plan_parser import ParsedPlan
from pipeline.project import handoff as handoff_mod, handoff_advice as adv
from pipeline.project.handoff_advice import AdvisorResult, HandoffAdvice
from pipeline.project.handoff_advice_dispatch import _handle_advice_request
from pipeline.project.handoff_advice_intent import parse_advice_intent

# ── fixtures / helpers ─────────────────────────────────────────────────────


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def _scripted(*lines: str) -> _FakeTTY:
    return _FakeTTY("".join(line + "\n" for line in lines))


def _advice(
    *,
    action: str = "retry_feedback",
    confidence: str = "high",
    retry_feedback: str = "Add a test for edge case A and re-run pytest.",
    parse_warnings: tuple[str, ...] = (),
) -> HandoffAdvice:
    return HandoffAdvice(
        recommended_action=action,  # type: ignore[arg-type]
        confidence=confidence,  # type: ignore[arg-type]
        rationale="The reviewer flagged a missing test.",
        retry_feedback=retry_feedback,
        risks=("scope creep",),
        expected_files=("a.py",),
        operator_note="",
        parse_warnings=parse_warnings,
        raw_output="{}",
        intent=parse_advice_intent(
            {
                "proposed_operations": [
                    {"kind": "repair", "target": "recorded_findings"},
                ],
                "contract_effects": [
                    {"invariant_id": "acceptance:1", "effect": "advance"},
                    {
                        "invariant_id": "task:repair-review:done:1",
                        "effect": "advance",
                    },
                ],
            },
        ),
    )


def _result(advice: HandoffAdvice) -> AdvisorResult:
    return AdvisorResult(advice=advice, raw="{}", usage={}, duration_s=0.0)


def _signal(
    *,
    handoff_id: str = "review_changes:review:2",
    available_actions: tuple[str, ...] = ("continue", "retry_feedback", "halt"),
) -> SimpleNamespace:
    return SimpleNamespace(
        handoff_id=handoff_id,
        phase="review_changes",
        type=SimpleNamespace(value="human_feedback_on_reject"),
        trigger="rejected",
        verdict="REJECTED",
        approved=False,
        round_extras_key="review",
        round=2,
        loop_max_rounds=2,
        available_actions=available_actions,
        artifacts={"findings": [{"id": "F1", "severity": "P2", "title": "gap"}]},
        last_output="reviewer rejected the change",
    )


def _hygiene_signal() -> SimpleNamespace:
    return SimpleNamespace(
        handoff_id="gate:test:1",
        phase="implement",
        type=SimpleNamespace(value="human_feedback_on_reject"),
        trigger="verification_gate_failed",
        verdict="REJECTED",
        approved=False,
        round_extras_key="repair_round",
        round=1,
        loop_max_rounds=2,
        available_actions=("continue_with_waiver", "halt"),
        artifacts={
            "findings": [
                {
                    "id": "verification_gate_provenance_failure",
                    "failure_kind": "provenance_failure",
                }
            ]
        },
        last_output="class=provenance_failure; exit_code=0",
    )


def _valid_parsed_plan() -> ParsedPlan:
    return ParsedPlan(
        subtasks=(
            SubTask(
                id="repair-review",
                goal="Repair the rejected review finding",
                done_criteria=("The targeted regression test passes.",),
                owned_files=("a.py",),
            ),
        ),
        source="json",
        short_summary="Repair the rejected handoff",
        planning_context="The review finding requires a focused retry.",
        goal="Return the rejected handoff to a verified state.",
        acceptance_criteria=("The handoff advice retry is contract-bound.",),
        owned_files=("a.py",),
    )


def _run(tmp_path) -> SimpleNamespace:
    run_dir = tmp_path / "20260613_010101_adv"
    run_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        output_dir=run_dir,
        git_cwd="",
        session_ts=run_dir.name,
        state=SimpleNamespace(
            task="resolve the rejected change",
            parsed_plan=_valid_parsed_plan(),
        ),
    )


def _note_relpath(note: str) -> str:
    assert note and note.startswith("feedback_source=agent_advice; advice_artifact=")
    return note.split("advice_artifact=", 1)[1]


# ── retry_with_advice (kind=6) ─────────────────────────────────────────────


def test_retry_with_advice_returns_retry_decision_with_provenance_note(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(adv, "invoke_advisor", lambda run, ctx, **k: _result(_advice()))
    run = _run(tmp_path)
    sig = _signal()
    di = _handle_advice_request(run, sig, AdviceActionRequest(kind="retry_with_advice"))
    assert di is not None
    assert di.action == "retry_feedback"
    assert di.feedback == "Add a test for edge case A and re-run pytest."
    rel = _note_relpath(di.note)
    assert rel == "phase_handoff_advice/" + _safe(sig.handoff_id) + ".json"
    assert (run.output_dir / rel).is_file()


def test_retry_with_advice_non_retry_recommendation_returns_to_menu(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        adv, "invoke_advisor",
        lambda run, ctx, **k: _result(_advice(action="continue", retry_feedback="")),
    )
    run = _run(tmp_path)
    di = _handle_advice_request(
        run, _signal(), AdviceActionRequest(kind="retry_with_advice"),
        stdout=_FakeTTY(),
    )
    assert di is None
    assert not (run.output_dir / "phase_handoff_decisions").exists()


def test_retry_with_advice_low_confidence_unconfirmed_returns_none(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        adv, "invoke_advisor",
        lambda run, ctx, **k: _result(_advice(confidence="low")),
    )
    di = _handle_advice_request(
        _run(tmp_path), _signal(), AdviceActionRequest(kind="retry_with_advice"),
        stdin=_scripted("n"), stdout=_FakeTTY(),
    )
    assert di is None


def test_retry_with_advice_low_confidence_confirmed_returns_menu(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        adv, "invoke_advisor",
        lambda run, ctx, **k: _result(_advice(confidence="low")),
    )
    di = _handle_advice_request(
        _run(tmp_path), _signal(), AdviceActionRequest(kind="retry_with_advice"),
        stdin=_scripted("y"), stdout=_FakeTTY(),
    )
    assert di is None


def test_retry_with_advice_unavailable_retry_returns_none(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(adv, "invoke_advisor", lambda run, ctx, **k: _result(_advice()))
    di = _handle_advice_request(
        _run(tmp_path),
        _signal(available_actions=("continue", "halt")),
        AdviceActionRequest(kind="retry_with_advice"),
        stdout=_FakeTTY(),
    )
    assert di is None


def test_hygiene_advice_is_deterministic_without_model_invocation(tmp_path, monkeypatch) -> None:
    def _unexpected_model_call(*args, **kwargs):
        raise AssertionError("hygiene advice must not invoke the model")

    monkeypatch.setattr(adv, "invoke_advisor", _unexpected_model_call)
    run = _run(tmp_path)

    di = _handle_advice_request(
        run,
        _hygiene_signal(),
        AdviceActionRequest(kind="retry_with_advice"),
        stdout=_FakeTTY(),
    )

    assert di is None
    artifacts = list((run.output_dir / "phase_handoff_advice").glob("*.json"))
    assert len(artifacts) == 1
    stored = adv.load_advice_artifact(artifacts[0])
    assert stored is not None
    assert stored["advice"]["recommended_action"] == "continue_with_waiver"


# ── advice (kind=5) follow-up sub-menu ─────────────────────────────────────


def test_advice_back_writes_no_decision(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(adv, "invoke_advisor", lambda run, ctx, **k: _result(_advice()))
    run = _run(tmp_path)
    di = _handle_advice_request(
        run, _signal(), AdviceActionRequest(kind="advice"),
        stdin=_scripted("3"), stdout=_FakeTTY(),  # 3 = back
    )
    assert di is None
    # The advisor wrote its advice artifact but NEVER a decision artifact.
    assert not (run.output_dir / "phase_handoff_decisions").exists()
    assert list((run.output_dir / "phase_handoff_advice").glob("*.json"))


def test_advice_halt_returns_halt_decision(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(adv, "invoke_advisor", lambda run, ctx, **k: _result(_advice()))
    run = _run(tmp_path)
    di = _handle_advice_request(
        run, _signal(), AdviceActionRequest(kind="advice"),
        stdin=_scripted("4"), stdout=_FakeTTY(),  # 4 = halt
    )
    assert di is not None
    assert di.action == "halt"
    assert (run.output_dir / _note_relpath(di.note)).is_file()


def test_advice_apply_returns_retry_decision(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(adv, "invoke_advisor", lambda run, ctx, **k: _result(_advice()))
    run = _run(tmp_path)
    di = _handle_advice_request(
        run, _signal(), AdviceActionRequest(kind="advice"),
        stdin=_scripted("1"), stdout=_FakeTTY(),  # 1 = apply
    )
    assert di is not None
    assert di.action == "retry_feedback"
    assert di.feedback == "Add a test for edge case A and re-run pytest."
    rel = _note_relpath(di.note)
    assert rel == "phase_handoff_advice/" + _safe("review_changes:review:2") + ".json"
    assert (run.output_dir / rel).is_file()


def test_advice_apply_low_confidence_non_retry_records_no_decision(
    tmp_path, monkeypatch,
) -> None:
    # F1 regression: a non-retry recommendation (here 'continue') with
    # confidence='low' must NEVER become a retry, even when the operator picks
    # "apply" and confirms the low-confidence prompt. The apply branch returns
    # to the menu with no decision; confirmation only gates a retry_feedback rec.
    monkeypatch.setattr(
        adv, "invoke_advisor",
        lambda run, ctx, **k: _result(
            _advice(action="continue", confidence="low", retry_feedback=""),
        ),
    )
    run = _run(tmp_path)
    di = _handle_advice_request(
        run, _signal(), AdviceActionRequest(kind="advice"),
        # "1" = apply, then "y" would confirm a low-confidence prompt — but the
        # non-retry guard returns before any confirmation is read.
        stdin=_scripted("1", "y"), stdout=_FakeTTY(),
    )
    assert di is None
    assert not (run.output_dir / "phase_handoff_decisions").exists()


def test_advice_apply_edit_non_retry_records_no_decision(
    tmp_path, monkeypatch,
) -> None:
    # Even an operator EDIT cannot upgrade a non-retry recommendation into a
    # retry from the advice path: the guard fires before the edit feedback is
    # considered.
    monkeypatch.setattr(
        adv, "invoke_advisor",
        lambda run, ctx, **k: _result(_advice(action="halt", retry_feedback="")),
    )
    run = _run(tmp_path)
    di = _handle_advice_request(
        run, _signal(), AdviceActionRequest(kind="advice"),
        stdin=_scripted("2", "operator wants to retry anyway", ""),  # 2 = edit
        stdout=_FakeTTY(),
    )
    assert di is None
    assert not (run.output_dir / "phase_handoff_decisions").exists()


def test_advice_edit_writes_divergent_artifact_and_note(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(adv, "invoke_advisor", lambda run, ctx, **k: _result(_advice()))
    run = _run(tmp_path)
    di = _handle_advice_request(
        run, _signal(), AdviceActionRequest(kind="advice"),
        stdin=_scripted("2", "Operator edited feedback", ""),  # 2 = edit
        stdout=_FakeTTY(),
    )
    assert di is not None
    assert di.action == "retry_feedback"
    assert di.feedback == "Operator edited feedback"
    rel = _note_relpath(di.note)
    # The note references a DIVERGENT (suffixed) artifact whose retry_feedback
    # is the operator-applied text, not the advisor's original.
    assert rel.endswith("_2.json")
    artifact = adv.load_advice_artifact(run.output_dir / rel)
    assert artifact is not None
    assert artifact["advice"]["retry_feedback"] == "Operator edited feedback"


# ── divergence + idempotency ───────────────────────────────────────────────


def test_divergent_advice_second_call_new_file_and_note(
    tmp_path, monkeypatch,
) -> None:
    calls = {"n": 0}

    def _vary(run, ctx, **k):
        calls["n"] += 1
        fb = "Fix A and re-run." if calls["n"] == 1 else "Different fix B."
        return _result(_advice(retry_feedback=fb))

    monkeypatch.setattr(adv, "invoke_advisor", _vary)
    run = _run(tmp_path)
    sig = _signal()
    di1 = _handle_advice_request(run, sig, AdviceActionRequest(kind="retry_with_advice"))
    di2 = _handle_advice_request(run, sig, AdviceActionRequest(kind="retry_with_advice"))
    rel1, rel2 = _note_relpath(di1.note), _note_relpath(di2.note)
    assert rel1.endswith(_safe(sig.handoff_id) + ".json")
    assert rel2.endswith("_2.json")
    assert rel1 != rel2
    assert (run.output_dir / rel1).is_file()
    assert (run.output_dir / rel2).is_file()
    # The applied decision references the version it actually generated from.
    assert adv.load_advice_artifact(run.output_dir / rel2)["advice"][
        "retry_feedback"
    ] == "Different fix B."


def test_repeat_apply_is_idempotent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(adv, "invoke_advisor", lambda run, ctx, **k: _result(_advice()))
    run = _run(tmp_path)
    sig = _signal()
    di1 = _handle_advice_request(run, sig, AdviceActionRequest(kind="retry_with_advice"))
    di2 = _handle_advice_request(run, sig, AdviceActionRequest(kind="retry_with_advice"))
    # Same generated payload → same artifact path → identical note (exact-payload
    # idempotency upheld); only one artifact file on disk.
    assert di1.note == di2.note
    assert len(list((run.output_dir / "phase_handoff_advice").glob("*.json"))) == 1


# ── advisor errors never break the loop ────────────────────────────────────


def test_advisor_exception_returns_none(tmp_path, monkeypatch) -> None:
    def _boom(run, ctx, **k):
        raise RuntimeError("provider blew up")

    monkeypatch.setattr(adv, "invoke_advisor", _boom)
    run = _run(tmp_path)
    di = _handle_advice_request(
        run, _signal(), AdviceActionRequest(kind="retry_with_advice"),
        stdout=_FakeTTY(),
    )
    assert di is None
    assert not (run.output_dir / "phase_handoff_advice").exists()


def test_unparseable_advice_returns_none(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        adv, "invoke_advisor",
        lambda run, ctx, **k: _result(
            _advice(action="halt", retry_feedback="",
                    parse_warnings=("advice_unparseable",)),
        ),
    )
    run = _run(tmp_path)
    di = _handle_advice_request(
        run, _signal(), AdviceActionRequest(kind="retry_with_advice"),
        stdout=_FakeTTY(),
    )
    assert di is None


def _safe(handoff_id: str) -> str:
    from sdk.phase_handoff import safe_handoff_id
    return safe_handoff_id(handoff_id)


# ── integration through process_pending_phase_handoffs ─────────────────────


def _real_signal(tmp_path):
    from pipeline.runtime.handoff import PhaseHandoffRequested
    from pipeline.runtime.roles import PhaseHandoffType

    return PhaseHandoffRequested(
        handoff_id="review_changes:review:2",
        phase="review_changes",
        type=PhaseHandoffType.HUMAN_FEEDBACK_ON_REJECT,
        trigger="rejected",
        verdict="REJECTED",
        approved=False,
        round_extras_key="review",
        round=2,
        loop_max_rounds=2,
        available_actions=("continue", "retry_feedback", "halt"),
        artifacts={"findings": [{"id": "F1", "severity": "P2"}]},
        last_output="reviewer rejected",
    )


def _pp_run(tmp_path, signal):
    from pipeline.plugins import PluginConfig
    from pipeline.runtime import PipelineState

    runs_root = tmp_path / "runs"
    run_dir = runs_root / "20260613_020202_pp"
    run_dir.mkdir(parents=True, exist_ok=True)
    state = PipelineState(
        task="t", project_dir=str(tmp_path), plugin=PluginConfig(),
    )
    state.parsed_plan = _valid_parsed_plan()
    state.phase_handoff_request = signal
    return SimpleNamespace(
        output_dir=run_dir,
        session={"phases": {}},
        session_ts=run_dir.name,
        state=state,
        no_interactive=False,
        _ckpt=None,
        _metrics=SimpleNamespace(save=lambda *a, **k: None),
        _dispatch_active=False,
        _presentation=handoff_mod.PresentationPolicy.SILENT,
    )


def _profile():
    from pipeline.runtime import PhaseStep, Profile

    return Profile(
        name="advanced", kind="advanced", description="x",
        steps=(PhaseStep(phase="review_changes"),),
    )


def test_process_pending_retry_with_advice_writes_decision_and_shares_resume(
    tmp_path, monkeypatch,
) -> None:
    import json

    from sdk.phase_handoff import safe_handoff_id

    signal = _real_signal(tmp_path)
    run = _pp_run(tmp_path, signal)

    monkeypatch.setattr(handoff_mod, "should_prompt_for_phase_handoff",
                        lambda **k: True)
    monkeypatch.setattr(
        handoff_mod, "prompt_phase_handoff_action",
        lambda sig, **k: AdviceActionRequest(kind="retry_with_advice"),
    )
    monkeypatch.setattr(adv, "invoke_advisor", lambda run, ctx, **k: _result(_advice()))

    spy = {"calls": 0}

    def _resume_spy(run, profile, ctx, *, on_round_end=None):
        spy["calls"] += 1
        return handoff_mod.PhaseHandoffResumeOutcome(
            profile=None, completed_phases=frozenset(), paused=False,
        )

    monkeypatch.setattr(
        handoff_mod, "apply_phase_handoff_resume_with_banners", _resume_spy,
    )

    result = handoff_mod.process_pending_phase_handoffs(run, _profile(), None)

    assert result.continue_dispatch is True
    # The retry flows through the SAME resume entrypoint the human
    # retry_feedback decision uses — no parallel branch.
    assert spy["calls"] == 1
    # A real decision artifact was written through the unchanged SDK path.
    dpath = (run.output_dir / "phase_handoff_decisions"
             / f"{safe_handoff_id(signal.handoff_id)}.json")
    assert dpath.is_file()
    decision = json.loads(dpath.read_text(encoding="utf-8"))
    assert decision["action"] == "retry_feedback"
    rel = _note_relpath(decision["note"])
    assert (run.output_dir / rel).is_file()
    assert adv.load_advice_artifact(run.output_dir / rel) is not None


def test_process_pending_advice_back_writes_no_decision(
    tmp_path, monkeypatch,
) -> None:
    from pipeline.control.handoff_prompt import (
        HANDOFF_PROMPT_ABORTED,
        AdviceFollowup,
    )

    signal = _real_signal(tmp_path)
    run = _pp_run(tmp_path, signal)

    calls = {"n": 0}

    def _prompt(sig, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return AdviceActionRequest(kind="advice")
        return HANDOFF_PROMPT_ABORTED  # second pass: abort → leave paused

    monkeypatch.setattr(handoff_mod, "should_prompt_for_phase_handoff",
                        lambda **k: True)
    monkeypatch.setattr(handoff_mod, "prompt_phase_handoff_action", _prompt)
    monkeypatch.setattr(adv, "invoke_advisor", lambda run, ctx, **k: _result(_advice()))
    monkeypatch.setattr(
        "pipeline.control.handoff_prompt.prompt_advice_followup",
        lambda **k: AdviceFollowup(action="back"),
    )

    result = handoff_mod.process_pending_phase_handoffs(run, _profile(), None)

    assert result.paused is True
    # advice + back wrote NO decision artifact; only the advice artifact exists.
    assert not (run.output_dir / "phase_handoff_decisions").exists()
    assert list((run.output_dir / "phase_handoff_advice").glob("*.json"))
