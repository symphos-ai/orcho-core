# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents.protocols import SessionMode
from pipeline.plugins import PluginConfig
from pipeline.project import session_run
from pipeline.project.bootstrap import init_session_with_atexit
from pipeline.project.resume_control import (
    ResumeControlError,
    ResumeControlRefusal,
    prepare_unattended_handoff_rearm,
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
    assert json.loads(request.output_dir.joinpath("meta.json").read_text())["status"] == "halted"


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
