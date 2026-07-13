# SPDX-License-Identifier: Apache-2.0
"""SDK contract tests for ``request_handoff_advice`` (read-only handoff advisor).

Exercise the accessor against a synthetic paused run under the ``--mock`` provider
(the advisor's ``[handoff_advice]`` mock branch). Pin: an eligible paused
rejected handoff yields a typed recommendation with a non-empty provenance note
and exactly one durable write (the advice artifact, never a decision, never a
meta.status change); a missing run / mismatched id / ineligible handoff raise the
existing typed SDK errors; an unparseable advisor response is handled like the
existing dispatch / CI paths (no durable advice write, never auto-applied).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agents.runtimes._strategy import MockAgentProvider
from sdk import (
    HandoffAdviceResult,
    HandoffAdviceSafety,
    InvalidPhaseHandoffState,
    RunNotFound,
    request_handoff_advice,
)

_HANDOFF_ID = "review_changes:review:2"


def _payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "id": _HANDOFF_ID,
        "phase": "review_changes",
        "type": "human_feedback_on_reject",
        "trigger": "rejected",
        "verdict": "REJECTED",
        "approved": False,
        "round_extras_key": "review",
        "round": 2,
        "loop_max_rounds": 2,
        "available_actions": [
            "continue",
            "retry_feedback",
            "halt",
            "continue_with_waiver",
        ],
        "artifacts": {
            "findings": [
                {"id": "F1", "severity": "P1", "title": "bug", "body": "fix it"},
            ],
        },
        "last_output": "reviewer rejected the change",
    }
    payload.update(overrides)
    return payload


def _seed_paused_run(
    tmp_path: Path,
    run_id: str = "20260623_120000_aaaaaa",
    *,
    status: str = "awaiting_phase_handoff",
    phase_handoff: dict[str, Any] | None = "__default__",  # type: ignore[assignment]
) -> tuple[Path, str, Path]:
    """Create a runs dir + a paused run dir with meta.json; return (runs, id, dir)."""
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    run_dir = runs / run_id
    run_dir.mkdir()
    meta: dict[str, Any] = {
        "task": "Fix the bug",
        "project": str(project),
        "model": "claude-opus-4-8",
        "profile": "feature",
        "status": status,
        "phases": {},
    }
    if phase_handoff == "__default__":
        meta["phase_handoff"] = _payload()
    elif phase_handoff is not None:
        meta["phase_handoff"] = phase_handoff
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    return runs, run_id, run_dir


# ── eligible rejected handoff → typed advice + provenance (under --mock) ─────


def test_eligible_rejected_handoff_returns_typed_advice(tmp_path: Path) -> None:
    runs, run_id, run_dir = _seed_paused_run(tmp_path)

    result = request_handoff_advice(
        run_id,
        runs_dir=runs,
        cwd=None,
        provider=MockAgentProvider(),
    )

    assert isinstance(result, HandoffAdviceResult)
    assert result.run_id == run_id
    assert result.handoff_id == _HANDOFF_ID
    assert result.phase == "review_changes"
    # The mock advisor recommends a confident retry_feedback with concrete text.
    assert result.recommended_action == "retry_feedback"
    assert result.confidence == "high"
    assert result.rationale
    assert result.retry_feedback
    # Safety is the classifier's verdict verbatim.
    assert isinstance(result.safety, HandoffAdviceSafety)
    assert result.safety.auto_apply_ok is True
    assert result.safety.needs_confirmation is False
    # The advice artifact is the single durable write; provenance points at it.
    assert result.advice_artifact.startswith("phase_handoff_advice/")
    assert result.provenance_note
    assert f"advice_artifact={result.advice_artifact}" in result.provenance_note
    assert "feedback_source=agent_advice" in result.provenance_note
    # The durable artifact actually landed.
    assert (run_dir / result.advice_artifact).is_file()


def test_eligible_handoff_with_explicit_matching_id(tmp_path: Path) -> None:
    runs, run_id, _ = _seed_paused_run(tmp_path)
    result = request_handoff_advice(
        run_id,
        _HANDOFF_ID,
        runs_dir=runs,
        cwd=None,
        provider=MockAgentProvider(),
    )
    assert result.handoff_id == _HANDOFF_ID
    assert result.recommended_action == "retry_feedback"


def test_hygiene_gate_returns_waiver_without_a_decision_or_model(
    tmp_path: Path,
) -> None:
    payload = _payload(
        trigger="verification_gate_failed",
        available_actions=["continue_with_waiver", "halt"],
        artifacts={
            "findings": [
                {
                    "id": "verification_gate_env_failure",
                    "severity": "P3",
                    "failure_kind": "env_failure",
                    "body": "class=env_failure; exit_code=0",
                }
            ]
        },
        last_output="class=env_failure; exit_code=0",
    )
    runs, run_id, run_dir = _seed_paused_run(tmp_path, phase_handoff=payload)

    result = request_handoff_advice(run_id, runs_dir=runs, cwd=None)

    assert result.recommended_action == "continue_with_waiver"
    assert result.retry_feedback == ""
    assert result.advice_artifact.startswith("phase_handoff_advice/")
    assert not (run_dir / "phase_handoff_decisions").exists()


# ── single durable write: no decision, no meta.status change ─────────────────


def test_advice_writes_no_decision_and_does_not_change_status(
    tmp_path: Path,
) -> None:
    runs, run_id, run_dir = _seed_paused_run(tmp_path)

    request_handoff_advice(
        run_id,
        runs_dir=runs,
        cwd=None,
        provider=MockAgentProvider(),
    )

    # No decision artifact directory is created.
    assert not (run_dir / "phase_handoff_decisions").exists()
    # meta.status is untouched, the active handoff still present.
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "awaiting_phase_handoff"
    assert meta["phase_handoff"]["id"] == _HANDOFF_ID
    # The only durable write is the advice artifact.
    advice_files = list((run_dir / "phase_handoff_advice").iterdir())
    assert len(advice_files) == 1


# ── typed errors (no traceback) ──────────────────────────────────────────────


def test_nonexistent_run_raises_run_not_found(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    with pytest.raises(RunNotFound):
        request_handoff_advice(
            "20260623_999999_zzzzzz",
            runs_dir=runs,
            cwd=None,
            provider=MockAgentProvider(),
        )


def test_no_active_handoff_raises_invalid_state(tmp_path: Path) -> None:
    runs, run_id, _ = _seed_paused_run(
        tmp_path,
        status="done",
        phase_handoff=None,
    )
    with pytest.raises(InvalidPhaseHandoffState):
        request_handoff_advice(
            run_id,
            runs_dir=runs,
            cwd=None,
            provider=MockAgentProvider(),
        )


def test_mismatched_handoff_id_raises_invalid_state(tmp_path: Path) -> None:
    runs, run_id, _ = _seed_paused_run(tmp_path)
    with pytest.raises(InvalidPhaseHandoffState):
        request_handoff_advice(
            run_id,
            "review_changes:review:99",
            runs_dir=runs,
            cwd=None,
            provider=MockAgentProvider(),
        )


def test_not_paused_status_raises_invalid_state(tmp_path: Path) -> None:
    # Active payload present but the run is not on a decidable status.
    runs, run_id, _ = _seed_paused_run(tmp_path, status="running")
    with pytest.raises(InvalidPhaseHandoffState):
        request_handoff_advice(
            run_id,
            runs_dir=runs,
            cwd=None,
            provider=MockAgentProvider(),
        )


def test_ineligible_approved_verdict_raises_invalid_state(tmp_path: Path) -> None:
    # Trigger matches but the verdict is APPROVED → not an advisory-eligible pause.
    runs, run_id, _ = _seed_paused_run(
        tmp_path,
        phase_handoff=_payload(
            verdict="APPROVED",
            approved=True,
            trigger="approved",
            available_actions=["continue", "retry_feedback", "halt"],
        ),
    )
    with pytest.raises(InvalidPhaseHandoffState):
        request_handoff_advice(
            run_id,
            runs_dir=runs,
            cwd=None,
            provider=MockAgentProvider(),
        )


def test_ineligible_without_retry_feedback_raises_invalid_state(
    tmp_path: Path,
) -> None:
    # retry_feedback not offered → ineligible for an advisory pass.
    runs, run_id, _ = _seed_paused_run(
        tmp_path,
        phase_handoff=_payload(available_actions=["continue", "halt"]),
    )
    with pytest.raises(InvalidPhaseHandoffState):
        request_handoff_advice(
            run_id,
            runs_dir=runs,
            cwd=None,
            provider=MockAgentProvider(),
        )


# ── unparseable advisor response: handled like existing dispatch / CI paths ──


def test_unparseable_advice_no_durable_write_not_auto_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pipeline.project.handoff_advice as adv

    def _garbage_invoke(run: Any, ctx: Any, *, agent: Any = None) -> Any:
        return adv.AdvisorResult(
            advice=adv.parse_advice("not json at all, sorry"),
            raw="not json at all, sorry",
            usage={},
        )

    monkeypatch.setattr(adv, "invoke_advisor", _garbage_invoke)
    runs, run_id, run_dir = _seed_paused_run(tmp_path)

    result = request_handoff_advice(
        run_id,
        runs_dir=runs,
        cwd=None,
        provider=MockAgentProvider(),
    )

    # Parser normalises unparseable output to halt/low with the warning.
    assert result.recommended_action == "halt"
    assert result.confidence == "low"
    assert "advice_unparseable" in result.parse_warnings
    # Never auto-applied; no durable advice artifact and no provenance note.
    assert result.safety.auto_apply_ok is False
    assert result.advice_artifact == ""
    assert result.provenance_note == ""
    assert not (run_dir / "phase_handoff_advice").exists()
    assert not (run_dir / "phase_handoff_decisions").exists()
