# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.protocols import SessionMode
from pipeline.plugins import PluginConfig
from pipeline.project import profile_dispatch, session_run
from pipeline.project.bootstrap import init_session_with_atexit
from pipeline.project.resume_control import (
    ResumeControlError,
    ResumeControlRefusal,
    ResumeRefusalProvenance,
    materialize_resume_control_refusal,
    prepare_unattended_handoff_rearm,
    read_resume_refusal_provenance,
    validate_unattended_handoff_payload,
)
from pipeline.project.types import ProjectRunRequest
from pipeline.project.verification_ledger_runtime import ResumeVerificationLedgerError


def _payload() -> dict[str, object]:
    return {
        "id": "handoff-1",
        "phase": "review_changes",
        "type": "human_feedback_on_reject",
        "trigger": "rejected",
        "verdict": "reject",
        "approved": False,
        "round_extras_key": "rounds",
        "round": 1,
        "loop_max_rounds": 2,
        "available_actions": ["continue", "halt"],
        "artifacts": {"review": "artifacts/review.md"},
        "last_output": "needs attention",
    }


def test_valid_unattended_halt_rearms_exact_canonical_payload() -> None:
    payload = _payload()
    rearm = prepare_unattended_handoff_rearm({
        "status": "halted",
        "halt_reason": "phase_handoff_unattended_halt",
        "phase_handoff_unattended": {"reason": "policy", "note": "n", "phase_handoff": payload},
    })

    assert rearm.handoff == payload
    assert rearm.handoff is not payload
    assert rearm.handoff["available_actions"] == ["continue", "halt"]


def test_legacy_compact_unattended_block_is_reason_preserving_refusal() -> None:
    with pytest.raises(ResumeControlRefusal) as raised:
        prepare_unattended_handoff_rearm({
            "status": "halted",
            "halt_reason": "phase_handoff_unattended_halt",
            "phase_handoff_unattended": {"handoff_id": "old", "phase": "implement"},
        })

    assert raised.value.halt_reason == "phase_handoff_unattended_halt"
    assert "legacy or incomplete" in str(raised.value)


def test_malformed_unattended_block_is_reason_preserving_refusal() -> None:
    payload = _payload()
    payload["available_actions"] = []

    with pytest.raises(ResumeControlRefusal) as raised:
        prepare_unattended_handoff_rearm({
            "status": "halted",
            "halt_reason": "phase_handoff_unattended_halt",
            "phase_handoff_unattended": {"reason": "policy", "note": "n", "phase_handoff": payload},
        })

    assert raised.value.halt_reason == "phase_handoff_unattended_halt"
    assert "invalid available_actions" in str(raised.value)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (None, "payload is missing"),
        ({}, "legacy or incomplete; missing"),
        ({**_payload(), "id": ""}, "has no id"),
        ({**_payload(), "phase": ""}, "has no phase"),
        ({**_payload(), "type": ""}, "has no type"),
        ({**_payload(), "type": "unknown"}, "has unknown type"),
        ({**_payload(), "trigger": None}, "has invalid trigger"),
        ({**_payload(), "verdict": None}, "has invalid verdict"),
        ({**_payload(), "approved": "false"}, "has invalid approved flag"),
        ({**_payload(), "round_extras_key": None}, "has invalid round_extras_key"),
        ({**_payload(), "round": 0}, "has invalid round"),
        ({**_payload(), "loop_max_rounds": 0}, "has invalid loop_max_rounds"),
        ({**_payload(), "available_actions": []}, "has invalid available_actions"),
        ({**_payload(), "artifacts": []}, "has invalid artifacts"),
        ({**_payload(), "last_output": None}, "has invalid last_output"),
    ],
)
def test_payload_validation_refusals_preserve_unattended_reason(payload, message) -> None:
    with pytest.raises(ResumeControlRefusal, match=message) as raised:
        validate_unattended_handoff_payload(payload)

    assert raised.value.halt_reason == "phase_handoff_unattended_halt"


@pytest.mark.parametrize(
    "meta",
    [
        {"status": "halted", "halt_reason": "phase_handoff_unattended_halt"},
        {
            "status": "halted",
            "halt_reason": "phase_handoff_unattended_halt",
            "phase_handoff_unattended": [],
        },
        {
            "status": "halted",
            "halt_reason": "phase_handoff_unattended_halt",
            "phase_handoff_unattended": {},
        },
    ],
)
def test_missing_or_malformed_unattended_block_refuses(meta) -> None:
    with pytest.raises(ResumeControlRefusal, match="legacy or incomplete") as raised:
        prepare_unattended_handoff_rearm(meta)

    assert raised.value.halt_reason == "phase_handoff_unattended_halt"


def test_non_unattended_halt_uses_persisted_reason_in_refusal() -> None:
    with pytest.raises(ResumeControlRefusal, match="not an unattended") as raised:
        prepare_unattended_handoff_rearm({
            "status": "halted", "halt_reason": "phase_handoff_halt",
        })

    assert raised.value.halt_reason == "phase_handoff_halt"


def test_ledger_error_is_catchable_resume_control_error() -> None:
    assert issubclass(ResumeVerificationLedgerError, ResumeControlError)


def test_bootstrap_carries_rearmed_payload_without_recomputation(tmp_path) -> None:
    payload = _payload()
    (tmp_path / "meta.json").write_text(
        json.dumps({
            "status": "halted",
            "halt_reason": "phase_handoff_unattended_halt",
            "phase_handoff_unattended": {
                "reason": "policy", "note": "n", "phase_handoff": payload,
            },
        }),
        encoding="utf-8",
    )

    session = init_session_with_atexit(
        task="t", project_path=tmp_path, plugin=PluginConfig(), model="m",
        profile_name="small_task", session_mode=SessionMode.AUTO,
        change_handoff="", output_dir=tmp_path,
    )
    session["status"] = "awaiting_phase_handoff"  # keep the test atexit-safe

    assert session["phase_handoff"] == payload
    assert session["_resume_unattended_handoff_rearm"] is True


def test_rearm_preserves_repairless_action_menu_without_retry_feedback() -> None:
    payload = _payload()
    payload["available_actions"] = ["continue", "halt", "continue_with_waiver"]

    rearm = prepare_unattended_handoff_rearm({
        "status": "halted",
        "halt_reason": "phase_handoff_unattended_halt",
        "phase_handoff_unattended": {
            "reason": "policy", "note": "n", "phase_handoff": payload,
        },
    })

    assert rearm.handoff["available_actions"] == payload["available_actions"]
    assert "retry_feedback" not in rearm.handoff["available_actions"]


@pytest.mark.parametrize(
    "contents",
    [None, "not json", "[]"],
)
def test_refusal_provenance_tolerates_missing_or_invalid_meta(tmp_path, contents) -> None:
    if contents is not None:
        (tmp_path / "meta.json").write_text(contents, encoding="utf-8")

    provenance = read_resume_refusal_provenance(tmp_path)

    assert provenance.status is None
    assert provenance.halt_reason is None
    assert provenance.meta == {}


def test_refusal_provenance_without_output_dir_is_empty() -> None:
    assert read_resume_refusal_provenance(None) == ResumeRefusalProvenance(None, None, {})


def test_refusal_provenance_keeps_only_valid_terminal_strings(tmp_path) -> None:
    (tmp_path / "meta.json").write_text(json.dumps({
        "status": "halted", "halt_reason": "phase_handoff_unattended_halt",
    }), encoding="utf-8")

    provenance = read_resume_refusal_provenance(tmp_path)

    assert provenance.status == "halted"
    assert provenance.halt_reason == "phase_handoff_unattended_halt"


def test_materialized_refusal_without_reason_or_durable_dependencies_uses_fallback() -> None:
    durable = materialize_resume_control_refusal(
        session=None,
        output_dir=None,
        checkpoint=None,
        state=None,
        provenance=ResumeRefusalProvenance(None, None, {}),
        error=ResumeControlError("control boundary failed"),
        task="t",
        project_dir="/project",
    )

    assert durable == {
        "task": "t",
        "project": "/project",
        "phases": {},
        "status": "halted",
        "halt_reason": "resume_control_refusal",
        "resume_refusal": {
            "error_type": "ResumeControlError",
            "message": "control boundary failed",
            "original_status": None,
            "halt_reason": "resume_control_refusal",
        },
    }


def _rearm_dispatch_run(payload):
    return SimpleNamespace(
        session={
            "phases": {},
            "phase_handoff": payload,
            "_resume_unattended_handoff_rearm": True,
        },
        state=SimpleNamespace(phase_handoff_request=None),
        max_rounds=None,
        registry=None,
        _session_adapters=None,
        _provider=None,
        _fsm_checkpoint=lambda *_: None,
        _fsm_metrics=lambda *_: None,
        _dispatch_active=False,
    )


def test_dispatch_rearms_unattended_handoff_through_pause_tail(monkeypatch) -> None:
    run = _rearm_dispatch_run(_payload())
    pauses: list[object] = []
    monkeypatch.setattr(profile_dispatch, "apply_runtime_max_rounds", lambda profile, **_: profile)
    monkeypatch.setattr(
        "pipeline.lifecycle.default_lifecycle_context", lambda **_: SimpleNamespace(),
    )
    monkeypatch.setattr("pipeline.project.handoff.apply_phase_handoff_pause", pauses.append)

    assert profile_dispatch.dispatch_via_v2_profile(run, object()) is run.session
    assert run.state.phase_handoff_request.handoff_id == "handoff-1"
    assert pauses == [run]
    assert run._dispatch_active is False
    assert "_resume_unattended_handoff_rearm" not in run.session


def test_dispatch_refuses_when_validated_payload_cannot_rehydrate(monkeypatch) -> None:
    run = _rearm_dispatch_run(_payload())
    monkeypatch.setattr(profile_dispatch, "apply_runtime_max_rounds", lambda profile, **_: profile)
    monkeypatch.setattr(
        "pipeline.lifecycle.default_lifecycle_context", lambda **_: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "pipeline.control.resume_preflight.build_signal_from_active_payload", lambda _: None,
    )

    with pytest.raises(ResumeControlRefusal, match="could not be rehydrated") as raised:
        profile_dispatch.dispatch_via_v2_profile(run, object())

    assert raised.value.halt_reason == "phase_handoff_unattended_halt"


def _resume_request(tmp_path):
    return ProjectRunRequest(
        task="t", project_dir=str(tmp_path), output_dir=tmp_path / "run",
        resume_from="run",
    )


def _patch_resume_coordinator(monkeypatch, ctx) -> None:
    monkeypatch.setattr(session_run, "_promote_plan_only_followup", lambda request: request)
    monkeypatch.setattr(session_run, "_resolve_profile_runtime", lambda request: ctx)


def test_typed_refusal_before_pipeline_run_persists_halted_meta(tmp_path, monkeypatch) -> None:
    request = _resume_request(tmp_path)
    request.output_dir.mkdir()
    request.output_dir.joinpath("meta.json").write_text(json.dumps({
        "status": "halted", "halt_reason": "pre_run_dirty_halt", "task": "t",
    }), encoding="utf-8")
    ctx = SimpleNamespace(session=None, ckpt=None, state=None, halted=False, session_ts="run")
    _patch_resume_coordinator(monkeypatch, ctx)
    monkeypatch.setattr(
        session_run, "_resolve_state",
        lambda *_: (_ for _ in ()).throw(ResumeVerificationLedgerError("no ledger")),
    )

    session, _, _ = session_run.run_project_pipeline_session(request)

    assert session["status"] == "halted"
    assert session["halt_reason"] == "pre_run_dirty_halt"
    assert session["resume_refusal"]["message"] == "no ledger"
    durable = json.loads(request.output_dir.joinpath("meta.json").read_text())
    assert durable["status"] == "halted"
    assert durable["halt_reason"] == "pre_run_dirty_halt"
    assert durable["resume_refusal"] == session["resume_refusal"]
    assert "interrupted_at" not in session
    assert "interrupted_at" not in durable


def test_typed_refusal_after_pipeline_run_syncs_checkpoint(tmp_path, monkeypatch) -> None:
    request = _resume_request(tmp_path)
    request.output_dir.mkdir()
    request.output_dir.joinpath("meta.json").write_text(json.dumps({
        "status": "interrupted", "halt_reason": "original_halt", "task": "t",
    }), encoding="utf-8")
    statuses: list[object] = []
    ctx = SimpleNamespace(
        session=None, ckpt=SimpleNamespace(set_status=statuses.append), state=None,
        halted=False, session_ts="run",
    )
    _patch_resume_coordinator(monkeypatch, ctx)

    def setup(_, context):
        context.session = {"status": "running", "phases": {}}
        context.state = SimpleNamespace(halt=False, halt_reason=None)

    monkeypatch.setattr(session_run, "_resolve_state", setup)
    monkeypatch.setattr(
        session_run, "_build_and_dispatch",
        lambda *_: (_ for _ in ()).throw(ResumeVerificationLedgerError("contract drift")),
    )

    session, _, _ = session_run.run_project_pipeline_session(request)

    assert session["halt_reason"] == "original_halt"
    assert session["resume_refusal"]["message"] == "contract drift"
    assert ctx.session["status"] == "halted"
    assert ctx.state.halt is True
    assert statuses and statuses[0].value == "halted"


def test_resume_control_refusal_preserves_unattended_reason_durably(tmp_path, monkeypatch) -> None:
    request = _resume_request(tmp_path)
    request.output_dir.mkdir()
    request.output_dir.joinpath("meta.json").write_text(json.dumps({
        "status": "halted", "halt_reason": "phase_handoff_unattended_halt", "task": "t",
    }), encoding="utf-8")
    ctx = SimpleNamespace(session=None, ckpt=None, state=None, halted=False, session_ts="run")
    _patch_resume_coordinator(monkeypatch, ctx)
    monkeypatch.setattr(
        session_run, "_resolve_state",
        lambda *_: (_ for _ in ()).throw(ResumeControlRefusal(
            "phase_handoff_unattended_halt", "unattended handoff payload is missing",
        )),
    )

    session, _, _ = session_run.run_project_pipeline_session(request)
    durable = json.loads(request.output_dir.joinpath("meta.json").read_text())

    assert session["status"] == durable["status"] == "halted"
    assert session["halt_reason"] == durable["halt_reason"] == "phase_handoff_unattended_halt"
    assert session["resume_refusal"] == durable["resume_refusal"] == {
        "error_type": "ResumeControlRefusal",
        "message": "unattended handoff payload is missing",
        "original_status": "halted",
        "halt_reason": "phase_handoff_unattended_halt",
    }
    assert "interrupted_at" not in session
    assert "interrupted_at" not in durable


def test_isolation_halt_returns_existing_session_without_dispatch(tmp_path, monkeypatch) -> None:
    request = _resume_request(tmp_path)
    session = {"status": "halted", "halt_reason": "pre_run_dirty_halt"}
    ctx = SimpleNamespace(session=session, ckpt=None, state=None, halted=True, session_ts="run")
    _patch_resume_coordinator(monkeypatch, ctx)
    monkeypatch.setattr(session_run, "_resolve_state", lambda *_: None)
    monkeypatch.setattr(
        session_run, "_build_and_dispatch",
        lambda *_: pytest.fail("halted context must not dispatch"),
    )

    assert session_run.run_project_pipeline_session(request) == (session, request.output_dir, "run")


def test_unknown_runtime_error_still_propagates(tmp_path, monkeypatch) -> None:
    request = _resume_request(tmp_path)
    ctx = SimpleNamespace(session=None, ckpt=None, state=None, halted=False, session_ts="run")
    _patch_resume_coordinator(monkeypatch, ctx)
    monkeypatch.setattr(
        session_run, "_resolve_state",
        lambda *_: (_ for _ in ()).throw(RuntimeError("provider exploded")),
    )

    with pytest.raises(RuntimeError, match="provider exploded"):
        session_run.run_project_pipeline_session(request)
