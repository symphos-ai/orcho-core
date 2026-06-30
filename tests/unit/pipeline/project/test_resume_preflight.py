"""Checkpoint-resume preflight detection + interactive resolution (T2).

Pins that resuming into an undecided active handoff is intercepted before
``run_pipeline`` trips ``load_handoff_decision_validated``:

* detection fires for ``awaiting_phase_handoff`` and the torn
  ``interrupted`` + active-payload shape, and stands down once a decision
  artifact exists or the run is terminal;
* the interactive resolver shows the same menu a fresh handoff uses and
  records the decision strictly through ``sdk.phase_handoff_decide``;
* an aborted prompt records nothing (no mutation).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from pipeline.control.resume_preflight import (
    build_signal_from_active_payload,
    detect_active_handoff_without_decision,
    render_noninteractive_hint,
    resolve_active_handoff_interactively,
)

_HANDOFF_ID = "validate_plan:plan_round:2"


def _payload(**overrides) -> dict:
    base = {
        "id": _HANDOFF_ID,
        "phase": "validate_plan",
        "type": "human_feedback_on_reject",
        "trigger": "rejected",
        "verdict": "REJECTED",
        "approved": False,
        "round_extras_key": "plan_round",
        "round": 2,
        "loop_max_rounds": 2,
        "available_actions": ["continue", "retry_feedback", "halt"],
        "artifacts": {},
        "last_output": "Mock critique: missing edge case.",
    }
    base.update(overrides)
    return base


def _make_run(
    tmp_path: Path,
    *,
    status: str = "interrupted",
    run_id: str = "20260101_000000",
    payload: dict | None = None,
) -> tuple[Path, Path, dict]:
    runs = tmp_path / "runs"
    run_dir = runs / run_id
    run_dir.mkdir(parents=True)
    meta = {
        "status": status,
        "phase_handoff": payload if payload is not None else _payload(),
        "task": "t",
        "project": "/p",
    }
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return runs, run_dir, meta


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def _scripted_stdin(*lines: str) -> _FakeTTY:
    return _FakeTTY("".join(line + "\n" for line in lines))


class TestDetection:
    def test_interrupted_active_no_decision_detected(self, tmp_path) -> None:
        _runs, run_dir, meta = _make_run(tmp_path, status="interrupted")
        pf = detect_active_handoff_without_decision(
            run_id=run_dir.name, run_dir=run_dir, meta=meta,
        )
        assert pf is not None
        assert pf.handoff_id == _HANDOFF_ID
        assert pf.status == "interrupted"
        assert pf.available_actions == ("continue", "retry_feedback", "halt")

    def test_awaiting_status_detected(self, tmp_path) -> None:
        _runs, run_dir, meta = _make_run(
            tmp_path, status="awaiting_phase_handoff",
        )
        pf = detect_active_handoff_without_decision(
            run_id=run_dir.name, run_dir=run_dir, meta=meta,
        )
        assert pf is not None

    def test_terminal_status_not_detected(self, tmp_path) -> None:
        _runs, run_dir, meta = _make_run(tmp_path, status="done")
        assert detect_active_handoff_without_decision(
            run_id=run_dir.name, run_dir=run_dir, meta=meta,
        ) is None

    def test_no_active_payload_not_detected(self, tmp_path) -> None:
        _runs, run_dir, _meta = _make_run(tmp_path, status="interrupted")
        assert detect_active_handoff_without_decision(
            run_id=run_dir.name, run_dir=run_dir,
            meta={"status": "interrupted"},
        ) is None

    def test_existing_decision_stands_down(self, tmp_path) -> None:
        from sdk.phase_handoff import phase_handoff_decide

        runs, run_dir, meta = _make_run(tmp_path, status="interrupted")
        phase_handoff_decide(
            run_dir.name, _HANDOFF_ID, "continue", runs_dir=runs, cwd=None,
        )
        assert detect_active_handoff_without_decision(
            run_id=run_dir.name, run_dir=run_dir, meta=meta,
        ) is None

    def test_prior_round_decision_does_not_block_current_round(
        self, tmp_path,
    ) -> None:
        """A recorded round:1 decision must not stand down preflight for the
        round:2 handoff now active — resume keys on the CURRENT id only."""
        from sdk.phase_handoff import safe_handoff_id

        id1 = "review_changes:repair_round:1"
        id2 = "review_changes:repair_round:2"
        # The run is now paused on round:2.
        _runs, run_dir, meta = _make_run(
            tmp_path,
            status="awaiting_phase_handoff",
            payload=_payload(
                id=id2, round=2, round_extras_key="repair_round",
                phase="review_changes",
            ),
        )
        # A decision for the PRIOR round (round:1) is already on disk.
        decisions = run_dir / "phase_handoff_decisions"
        decisions.mkdir()
        (decisions / f"{safe_handoff_id(id1)}.json").write_text(
            json.dumps({
                "run_id": run_dir.name,
                "handoff_id": id1,
                "phase": "review_changes",
                "action": "retry_feedback",
                "feedback": "round one fix",
                "note": None,
                "decided_at": "2026-06-12T00:00:00+00:00",
            }),
            encoding="utf-8",
        )

        pf = detect_active_handoff_without_decision(
            run_id=run_dir.name, run_dir=run_dir, meta=meta,
        )
        assert pf is not None
        assert pf.handoff_id == id2


class TestSignalBuilder:
    def test_builds_signal(self) -> None:
        signal = build_signal_from_active_payload(_payload())
        assert signal is not None
        assert signal.handoff_id == _HANDOFF_ID
        assert signal.round == 2
        assert signal.loop_max_rounds == 2

    def test_missing_field_returns_none(self) -> None:
        bad = _payload()
        del bad["id"]
        assert build_signal_from_active_payload(bad) is None

    def test_unknown_type_returns_none(self) -> None:
        assert build_signal_from_active_payload(
            _payload(type="garbage"),
        ) is None


class TestNonInteractiveHint:
    def test_hint_lists_fields_and_commands(self, tmp_path) -> None:
        _runs, run_dir, meta = _make_run(tmp_path)
        pf = detect_active_handoff_without_decision(
            run_id=run_dir.name, run_dir=run_dir, meta=meta,
        )
        hint = render_noninteractive_hint(pf)
        assert run_dir.name in hint
        assert _HANDOFF_ID in hint
        assert "interrupted" in hint
        assert "available_actions" in hint
        assert "phase_handoff_decide" in hint
        assert "--resume" in hint


class TestInteractiveResolution:
    def test_records_decision_and_shows_menu(self, tmp_path) -> None:
        runs, run_dir, meta = _make_run(tmp_path, status="interrupted")
        pf = detect_active_handoff_without_decision(
            run_id=run_dir.name, run_dir=run_dir, meta=meta,
        )
        out = _FakeTTY()
        recorded = resolve_active_handoff_interactively(
            pf, runs_dir=runs,
            stdin=_scripted_stdin("1", ""),  # continue, default note
            stdout=out,
        )
        assert recorded is True
        # Same menu surface a fresh handoff uses.
        body = out.getvalue()
        assert "Phase handoff" in body
        assert "Choose action" in body
        # Decision is now on disk → a re-detect stands down.
        assert detect_active_handoff_without_decision(
            run_id=run_dir.name, run_dir=run_dir, meta=meta,
        ) is None

    def test_abort_records_nothing(self, tmp_path) -> None:
        runs, run_dir, meta = _make_run(tmp_path, status="interrupted")
        pf = detect_active_handoff_without_decision(
            run_id=run_dir.name, run_dir=run_dir, meta=meta,
        )
        # Ctrl-D at the action prompt (empty stdin) → aborted sentinel.
        recorded = resolve_active_handoff_interactively(
            pf, runs_dir=runs, stdin=_FakeTTY(""), stdout=_FakeTTY(),
        )
        assert recorded is False
        assert not (run_dir / "phase_handoff_decisions").exists()

    def test_existing_same_action_decision_during_prompt_counts_as_recorded(
        self, tmp_path,
    ) -> None:
        from sdk.phase_handoff import phase_handoff_decide

        runs, run_dir, meta = _make_run(tmp_path, status="awaiting_phase_handoff")
        pf = detect_active_handoff_without_decision(
            run_id=run_dir.name, run_dir=run_dir, meta=meta,
        )
        assert pf is not None

        phase_handoff_decide(
            run_dir.name,
            _HANDOFF_ID,
            "retry_feedback",
            feedback="already recorded",
            runs_dir=runs,
            cwd=None,
        )

        recorded = resolve_active_handoff_interactively(
            pf,
            runs_dir=runs,
            stdin=_scripted_stdin(
                "2",
                "operator typed a later retry text",
                "",
                "",
            ),
            stdout=_FakeTTY(),
        )

        assert recorded is True
