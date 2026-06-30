"""Evidence-collector coverage for ADR 0073 implement-handoff delivery.

The collector must surface ``delivery_status`` (from ``meta.phases.implement``)
and the new ``decided_by`` (from ``meta.phase_handoff_waiver``) without breaking
the existing ``decided_at`` read, and keep ``continue`` vs
``continue_with_waiver`` distinguishable via ``action``.
"""
from __future__ import annotations

import json
from pathlib import Path

from sdk import get_errors_halt


def _seed_run(runs_dir: Path, run_id: str, *, meta: dict) -> Path:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    (run_dir / "metrics.json").write_text(
        json.dumps({
            "total_tokens": 0, "total_tokens_in": 0, "total_tokens_out": 0,
            "total_duration_s": 0.0, "total_rounds": 0,
        }) + "\n", encoding="utf-8",
    )
    return run_dir


def _errors(runs, run_id) -> list[dict]:
    info = get_errors_halt(run_id, runs_dir=runs, cwd=None)
    return list(info.errors)


def test_auto_waiver_surfaces_delivery_status_and_decided_by(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260604_100000_aaaaaa", meta={
        "task": "demo", "status": "done",
        "phases": {
            "implement": {
                "output": "build",
                "delivery_status": "waived",
                "delivery_waived": True,
                "waiver_id": "implement:implement_handoff:1",
                "action": "continue_with_waiver",
            },
        },
        "phase_handoff_waiver": {
            "handoff_id": "implement:implement_handoff:1",
            "phase": "implement",
            "waiver_text": "auto-waived: t2 incomplete accepted",
            "decided_at": "2026-06-04T10:00:00+00:00",
            "decided_by": "auto:on_exhausted",
        },
    })
    errors = _errors(runs, "20260604_100000_aaaaaa")

    waiver = next(e for e in errors if e.get("kind") == "phase_handoff_waiver")
    assert waiver["decided_by"] == "auto:on_exhausted"
    assert waiver["decided_at"] == "2026-06-04T10:00:00+00:00"  # not broken

    delivery = next(e for e in errors if e.get("kind") == "implement_delivery")
    assert delivery["delivery_status"] == "waived"
    assert delivery["delivery_waived"] is True
    assert delivery["waiver_id"] == "implement:implement_handoff:1"
    assert delivery["action"] == "continue_with_waiver"


def test_operator_continue_is_distinguishable_via_action(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260604_110000_bbbbbb", meta={
        "task": "demo", "status": "done",
        "phases": {
            "implement": {
                "output": "build",
                "delivery_status": "waived",
                "delivery_waived": True,
                "waiver_id": "implement:implement_handoff:1",
                "action": "continue",  # bare continue, not continue_with_waiver
            },
        },
        "phase_handoff_waiver": {
            "handoff_id": "implement:implement_handoff:1",
            "phase": "implement",
            "waiver_text": "Operator continued without explicit waiver feedback",
            "decided_at": "2026-06-04T11:00:00+00:00",
            "decided_by": "operator",
        },
    })
    errors = _errors(runs, "20260604_110000_bbbbbb")

    waiver = next(e for e in errors if e.get("kind") == "phase_handoff_waiver")
    assert waiver["decided_by"] == "operator"
    delivery = next(e for e in errors if e.get("kind") == "implement_delivery")
    assert delivery["action"] == "continue"  # distinguishes from waiver


def test_clean_delivery_emits_no_implement_delivery_breadcrumb(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260604_120000_cccccc", meta={
        "task": "demo", "status": "done",
        "phases": {"implement": {"output": "build", "delivery_status": "clean"}},
    })
    errors = _errors(runs, "20260604_120000_cccccc")
    assert not [e for e in errors if e.get("kind") == "implement_delivery"]


def test_implement_delivery_breadcrumb_surfaces_blocking_ids(tmp_path: Path) -> None:
    """P2 evidence: the implement_delivery breadcrumb names which subtasks
    blocked delivery — incomplete + missing-receipt ids — so a missing-receipt
    waiver is traceable in evidence (a missing receipt has no subtask.receipt
    event of its own)."""
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260604_130000_dddddd", meta={
        "task": "demo", "status": "done",
        "phases": {"implement": {
            "output": "build",
            "delivery_status": "waived",
            "delivery_waived": True,
            "waiver_id": "implement:implement_handoff:1",
            "action": "continue_with_waiver",
            "incomplete_subtasks": ["t2"],
            "missing_subtask_receipts": ["t3"],
            "attestation_incomplete": {"t2": "criteria not closed"},
        }},
        "phase_handoff_waiver": {
            "handoff_id": "implement:implement_handoff:1", "phase": "implement",
            "waiver_text": "auto-waived", "decided_at": "2026-06-04T13:00:00+00:00",
            "decided_by": "auto:on_exhausted",
        },
    })
    errors = _errors(runs, "20260604_130000_dddddd")
    delivery = next(e for e in errors if e.get("kind") == "implement_delivery")
    assert delivery["incomplete_subtasks"] == ["t2"]
    assert delivery["missing_subtask_receipts"] == ["t3"]
    assert delivery["attestation_incomplete"] == {"t2": "criteria not closed"}


def test_clean_delivery_breadcrumb_omitted_unaffected(tmp_path: Path) -> None:
    """A clean delivery still emits no implement_delivery breadcrumb (the new
    blocking-id fields don't change the clean-path shape)."""
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "20260604_131000_eeeeee", meta={
        "task": "demo", "status": "done",
        "phases": {"implement": {"output": "build", "delivery_status": "clean"}},
    })
    errors = _errors(runs, "20260604_131000_eeeeee")
    assert not [e for e in errors if e.get("kind") == "implement_delivery"]
