"""Pending phase-handoff id visibility on the SDK/CLI status surfaces (T4).

This is the MCP-facing contract: ``orcho_run_status`` and the
``run_control`` MCP tools never read ``meta.json`` themselves — they relay
:func:`sdk.status.load_status` and :func:`sdk.run_control.load_run_snapshot`.
So the *current* pending handoff id (the round awaiting an operator
decision) must be deterministically extractable from ``load_status`` output,
exactly as :func:`sdk.phase_handoff.phase_handoff_decide` validates it
(``meta.phase_handoff['id']``). These tests pin:

* ``load_status`` exposes the current pending id via ``raw_meta`` and the
  ``RunMeta.extra`` projection (no orcho-mcp edits needed);
* the CLI ``orcho status`` printer renders that id only while awaiting;
* the handoff id progresses ``round:1 -> round:2`` across sequential
  rejects, and a recorded round:1 decision neither blocks nor shadows a
  ``decide(round:2)``;
* ``decide`` is exact-payload idempotent per id (repeat returns the same
  record; a divergent payload conflicts).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli._formatters import format_status
from sdk.errors import InvalidPhaseHandoffState
from sdk.phase_handoff import phase_handoff_decide, safe_handoff_id
from sdk.status import load_status

_PHASE = "review_changes"


def _seed_paused(
    runs_dir: Path,
    run_id: str,
    *,
    handoff_id: str,
    round_n: int,
    status: str = "awaiting_phase_handoff",
) -> Path:
    """Write a run paused on a ``review_changes`` repair-round handoff."""
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    meta: dict = {
        "task": "demo",
        "project": "/some/proj",
        "profile": "task",
        "timestamp": "2026-06-12T10:00:00",
        "status": status,
        "phases": {"plan": [{"approved": True}], "implement": [{}]},
    }
    if status == "awaiting_phase_handoff":
        meta["phase_handoff"] = {
            "id": handoff_id,
            "phase": _PHASE,
            "type": "human_feedback_on_reject",
            "trigger": "rejected",
            "verdict": "REJECTED",
            "approved": False,
            "round_extras_key": "repair_round",
            "round": round_n,
            "loop_max_rounds": 1,
            "available_actions": ["continue", "retry_feedback", "halt"],
            "artifacts": {},
            "last_output": "critique",
        }
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8",
    )
    return run_dir


# ── load_status exposes the current pending id (MCP-facing shape) ───────────


def test_load_status_exposes_current_pending_handoff_id(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _seed_paused(
        runs, "20260612_paused",
        handoff_id="review_changes:repair_round:2", round_n=2,
    )

    status = load_status("20260612_paused", runs_dir=runs, cwd=None)

    # The deterministic MCP-facing extraction path: raw_meta carries the
    # active payload verbatim, so its 'id' is the current pending id.
    assert status.raw_meta["phase_handoff"]["id"] == "review_changes:repair_round:2"
    # The typed RunMeta projection keeps the same payload under `extra`
    # (phase_handoff is not a promoted field), so embedders that read the
    # projection rather than raw_meta still see the current id.
    assert status.meta is not None
    assert status.meta.status == "awaiting_phase_handoff"
    assert status.meta.extra["phase_handoff"]["id"] == "review_changes:repair_round:2"


def test_load_status_no_pending_id_when_not_awaiting(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _seed_paused(
        runs, "20260612_done",
        handoff_id="review_changes:repair_round:1", round_n=1, status="done",
    )
    status = load_status("20260612_done", runs_dir=runs, cwd=None)
    assert "phase_handoff" not in status.raw_meta


# ── CLI status renders the current pending id ──────────────────────────────


def test_cli_status_renders_pending_handoff_id(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _seed_paused(
        runs, "20260612_paused",
        handoff_id="review_changes:repair_round:2", round_n=2,
    )
    status = load_status("20260612_paused", runs_dir=runs, cwd=None)

    rendered = format_status(status)
    assert "Pending handoff: review_changes:repair_round:2" in rendered


def test_cli_status_omits_pending_line_for_running_run(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _seed_paused(
        runs, "20260612_running",
        handoff_id="review_changes:repair_round:1", round_n=1, status="running",
    )
    status = load_status("20260612_running", runs_dir=runs, cwd=None)
    rendered = format_status(status)
    assert "Pending handoff" not in rendered


def test_cli_status_renders_phase_usage_delivery_and_ignores_artifact_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infra import config

    monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
    config._reset_config()
    runs = tmp_path / "runs"
    run_dir = runs / "20260612_done"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "task": "ship cost status",
                "project": "/repo/orcho-core",
                "profile": "feature",
                "timestamp": "2026-06-12T10:00:00",
                "status": "done",
                "phases": {"plan": {}, "implement": {}},
                "commit_delivery": {
                    "action": "approve",
                    "status": "committed",
                    "release_verdict": "APPROVED",
                    "release_summary": "Ready after verification receipt review.",
                    "verification_missing": ["lint"],
                    "pr_url": "https://example.test/pr/1",
                },
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "metrics.json").write_text(
        json.dumps(
            {
                "total_tokens": 300,
                "total_tokens_in": 250,
                "total_tokens_out": 50,
                "total_duration_s": 12.5,
                "total_rounds": 2,
                "total_cost_usd_equivalent": 12.34,
                "cost_estimated": True,
                "phases": {
                    "plan": {
                        "model": "claude-opus-4-8",
                        "attempts": 1,
                        "total_tokens": 100,
                        "duration_s": 3.0,
                        "cost_usd_equivalent": 1.0,
                    },
                    "review_changes": {
                        "model": "gpt-5.5",
                        "attempts": 2,
                        "total_tokens": 200,
                        "duration_s": 9.5,
                        "cost_usd_equivalent": 2.0,
                        "cost_estimated": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    for name in ("commit_decisions", "phase_handoff_advice", "phases"):
        (run_dir / name).mkdir()

    try:
        status = load_status("20260612_done", runs_dir=runs, cwd=None)
        rendered = format_status(status)
    finally:
        config._reset_config()

    assert "Projects:" not in rendered
    assert "Cost ref: estimated-api ~$12.34" in rendered
    assert "review_changes" in rendered
    assert "runtime-reported $1.00" in rendered
    assert "estimated-api ~$2.00" in rendered
    assert "Delivery:" in rendered
    assert "Status: committed (approve)" in rendered
    assert "Verification missing: lint" in rendered
    assert "PR: https://example.test/pr/1" in rendered
    assert "Run dir:" in rendered


# ── id progression + per-id idempotency ────────────────────────────────────


def test_handoff_id_progresses_and_old_round_does_not_block_decide(
    tmp_path: Path,
) -> None:
    """Two sequential rejects yield round:1 / round:2; round:1 never blocks."""
    runs = tmp_path / "runs"
    run_id = "20260612_progress"
    id1 = "review_changes:repair_round:1"
    id2 = "review_changes:repair_round:2"

    # Round 1: paused, operator retries.
    run_dir = _seed_paused(runs, run_id, handoff_id=id1, round_n=1)
    d1 = phase_handoff_decide(
        run_id, id1, "retry_feedback", feedback="round one fix",
        runs_dir=runs, cwd=None,
    )
    assert d1.handoff_id == id1

    # The retry produced a fresh rejection -> the run is now paused on round 2
    # with a NEW id. (Reseed meta to that state; the round:1 artifact stays.)
    _seed_paused(runs, run_id, handoff_id=id2, round_n=2)

    # The recorded round:1 decision must not block deciding round:2.
    d2 = phase_handoff_decide(
        run_id, id2, "retry_feedback", feedback="round two fix",
        runs_dir=runs, cwd=None,
    )
    assert d2.handoff_id == id2

    # Distinct ids -> distinct artifacts; both are on disk.
    assert id1 != id2
    decisions = run_dir / "phase_handoff_decisions"
    assert (decisions / f"{safe_handoff_id(id1)}.json").is_file()
    assert (decisions / f"{safe_handoff_id(id2)}.json").is_file()

    # The current pending id surfaced by status is the round:2 id.
    status = load_status(run_id, runs_dir=runs, cwd=None)
    assert status.raw_meta["phase_handoff"]["id"] == id2


def test_decide_is_exact_payload_idempotent_per_id(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    run_id = "20260612_idem"
    id2 = "review_changes:repair_round:2"
    _seed_paused(runs, run_id, handoff_id=id2, round_n=2)

    first = phase_handoff_decide(
        run_id, id2, "retry_feedback", feedback="same", runs_dir=runs, cwd=None,
    )
    # Repeat with the identical payload -> persisted record returned unchanged.
    again = phase_handoff_decide(
        run_id, id2, "retry_feedback", feedback="same", runs_dir=runs, cwd=None,
    )
    assert again.handoff_id == first.handoff_id
    assert again.action == first.action
    assert again.feedback == first.feedback
    assert again.decided_at == first.decided_at

    # A divergent payload for the SAME id is a conflict, not an overwrite.
    with pytest.raises(InvalidPhaseHandoffState, match="already decided"):
        phase_handoff_decide(
            run_id, id2, "retry_feedback", feedback="different",
            runs_dir=runs, cwd=None,
        )
