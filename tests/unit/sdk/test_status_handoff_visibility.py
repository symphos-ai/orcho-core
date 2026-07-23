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
from core.io.ansi import set_color_enabled, strip_ansi
from sdk.errors import InvalidPhaseHandoffState
from sdk.phase_handoff import phase_handoff_decide, safe_handoff_id
from sdk.status import load_status
from sdk.types import GateStatus, PhaseStatus, RunMeta, RunRef, RunStatus

_PHASE = "review_changes"


def _seed_paused(
    runs_dir: Path,
    run_id: str,
    *,
    handoff_id: str,
    round_n: int,
    status: str = "awaiting_phase_handoff",
    available_actions: list[str] | None = None,
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
            "available_actions": available_actions or ["continue", "retry_feedback", "halt"],
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


def test_decide_rejects_retry_feedback_absent_from_repairless_handoff(
    tmp_path: Path,
) -> None:
    """Runtime-published membership is the SDK's sole action authority."""
    runs = tmp_path / "runs"
    _seed_paused(
        runs,
        "20260723_repairless",
        handoff_id="gate:pytest:1",
        round_n=1,
        available_actions=["continue", "halt", "continue_with_waiver"],
    )

    with pytest.raises(InvalidPhaseHandoffState, match="not in the active handoff"):
        phase_handoff_decide(
            run_id="20260723_repairless",
            handoff_id="gate:pytest:1",
            action="retry_feedback",
            feedback="repair it",
            runs_dir=runs,
        )


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
    (run_dir / "evidence.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "run_id": "20260612_done",
                "status": "done",
                "gates": [
                    {
                        "name": "lint",
                        "kind": "computational",
                        "outcome": "passed",
                        "duration_s": 1.25,
                    },
                    {
                        "name": "tests",
                        "kind": "computational",
                        "outcome": "skipped",
                        "duration_s": 0.0,
                    },
                ],
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
    assert [gate.name for gate in status.quality_gates] == ["lint", "tests"]
    assert "Gates:" in rendered
    assert "passed x1" in rendered
    assert "skipped x1" in rendered
    assert "tests                  skipped             0.00s computational" in rendered
    assert "lint                   passed" not in rendered
    assert "Delivery:" in rendered
    assert "Status: committed (approve)" in rendered
    assert "Verification missing: lint" in rendered
    assert "PR: https://example.test/pr/1" in rendered
    assert "Run dir:" in rendered


def test_status_uses_workspace_accounting_config_without_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infra import config

    monkeypatch.delenv("ORCHO_ACCOUNTING", raising=False)
    monkeypatch.delenv("ORCHO_WORKSPACE", raising=False)
    monkeypatch.delenv("ORCHO_RUNSPACE", raising=False)
    monkeypatch.delenv("ORCHO_DISABLE_LOCAL_CONFIG", raising=False)
    config._reset_config()

    workspace = tmp_path / "workspace-orchestrator"
    runs = workspace / "runspace" / "runs"
    run_dir = runs / "20260612_accounting"
    run_dir.mkdir(parents=True)
    (workspace / ".orcho").mkdir()
    (workspace / ".orcho" / "config.local.json").write_text(
        json.dumps({"accounting": {"enabled": True}}),
        encoding="utf-8",
    )
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "task": "workspace accounting status",
                "project": "/repo/orcho-core",
                "status": "done",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "metrics.json").write_text(
        json.dumps(
            {
                "total_tokens": 10,
                "total_duration_s": 1.0,
                "total_cost_usd_equivalent": 1.23,
                "phases": {
                    "plan": {
                        "model": "claude-opus-4-8",
                        "total_tokens": 10,
                        "cost_usd_equivalent": 1.23,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    try:
        status = load_status("20260612_accounting", workspace=workspace)
        rendered = format_status(status)
    finally:
        config._reset_config()

    assert "Cost ref: runtime-reported $1.23" in rendered
    assert "runtime-reported $1.23" in rendered


def test_cli_status_color_can_be_forced_without_changing_plain_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.infra import config

    monkeypatch.setenv("ORCHO_ACCOUNTING", "1")
    config._reset_config()
    runs = tmp_path / "runs"
    run_dir = runs / "20260612_color"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "task": "color status",
                "project": "/repo/orcho-core",
                "status": "done",
                "commit_delivery": {
                    "status": "committed",
                    "release_verdict": "APPROVED",
                    "verification_missing": ["lint"],
                },
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "metrics.json").write_text(
        json.dumps(
            {
                "total_tokens": 10,
                "total_duration_s": 1.0,
                "total_cost_usd_equivalent": 1.23,
                "cost_estimated": True,
                "phases": {
                    "plan": {
                        "model": "gpt-5.5",
                        "total_tokens": 10,
                        "duration_s": 1.0,
                        "cost_usd_equivalent": 1.23,
                        "cost_estimated": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    try:
        status = load_status("20260612_color", runs_dir=runs, cwd=None)
        set_color_enabled(True)
        rendered = format_status(status)
    finally:
        set_color_enabled(None)
        config._reset_config()

    assert "\x1b[" in rendered
    plain = strip_ansi(rendered)
    assert "Status:  done" in plain
    assert "Cost ref: estimated-api ~$1.23" in plain
    assert "Verification missing: lint" in plain


def test_cli_status_handles_invalid_metrics_empty_delivery_and_path_context(
    tmp_path: Path,
) -> None:
    runs = tmp_path / "runs"
    run_dir = runs / "20260612_running"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "task": "status edge cases",
                "project": "/repo/orcho-core",
                "profile": "feature",
                "timestamp": "2026-06-12T10:00:00",
                "status": "awaiting_phase_handoff",
                "phase_handoff": {"id": 42},
                "phases": {"plan": {}, "implement": {}},
                "commit_delivery": {},
                "worktree": {"path": "/tmp/worktree"},
                "parent_run_id": "20260611_parent",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "metrics.json").write_text(
        json.dumps(
            {
                "total_tokens": 10,
                "total_tokens_in": 7,
                "total_tokens_out": 3,
                "total_duration_s": 1.5,
                "total_retries": 2,
                "total_cost_usd_equivalent": "not-a-number",
                "cost_estimated": "unknown",
                "phases": [
                    "not a phase mapping",
                ],
            }
        ),
        encoding="utf-8",
    )

    status = load_status("20260612_running", runs_dir=runs, cwd=None)
    rendered = format_status(status)

    assert "Pending handoff" not in rendered
    assert "Cost ref:" not in rendered
    assert "Retries: 2" in rendered
    assert "Phases completed: plan, implement" in rendered
    assert "Delivery:" not in rendered
    assert "Worktree: /tmp/worktree" in rendered
    assert "Parent:   20260611_parent" in rendered


def test_cli_status_renders_cross_projects_subprojects_verbose_and_clips_text(
    tmp_path: Path,
) -> None:
    status = RunStatus(
        run_ref=RunRef(
            run_id="20260612_cross",
            run_dir=tmp_path / "runs" / "20260612_cross",
        ),
        meta=RunMeta(
            project=None,
            task="x" * 120,
            status="running",
            profile="cross",
            timestamp="2026-06-12T10:00:00",
            projects=("api", "web"),
        ),
        total_tokens=1_234,
        total_tokens_in=1_000,
        total_tokens_out=234,
        total_duration_s=3.5,
        total_rounds=1,
        raw_meta={"note": "verbose payload"},
        raw_metrics={
            "total_tokens": 1_234,
            "total_tokens_in": 1_000,
            "total_tokens_out": 234,
            "total_duration_s": 3.5,
            "total_rounds": 1,
            "total_cost_usd_equivalent": 1.5,
            "cost_estimated": "unknown",
            "phases": {
                "plan": {
                    "attempts": "bad",
                    "total_tokens": "bad",
                    "duration_s": "bad",
                    "model": "claude-model-name-that-is-long-enough-to-clip",
                    "cost_usd_equivalent": "bad",
                }
            },
        },
        sub_projects=(
            PhaseStatus(name="api", status="done"),
            PhaseStatus(name="web", status=None),
        ),
    )

    rendered = format_status(status, verbose=True)

    assert "Project: [cross] api, web" in rendered
    assert "Task:    " + ("x" * 80) in rendered
    assert "Projects:" in rendered
    assert "[api]  status=done" in rendered
    assert "[web]  status=?" in rendered
    assert "attempts=?" in rendered
    assert "claude-model-name-that-..." in rendered
    assert "Rounds:  1" in rendered
    assert "Detailed Meta:" in rendered
    assert '"note": "verbose payload"' in rendered

    no_phase_map = RunStatus(
        run_ref=RunRef(
            run_id="20260612_no_phase_map",
            run_dir=tmp_path / "runs" / "20260612_no_phase_map",
        ),
        meta=None,
        total_tokens=1,
        total_duration_s=1.0,
        raw_metrics={
            "total_cost_usd_equivalent": 2.0,
            "cost_estimated": "unknown",
            "phases": [],
        },
    )
    rendered_no_phase_map = format_status(no_phase_map)
    assert "Cost ref: runtime-reported $2.00" in rendered_no_phase_map


def test_cli_status_verbose_renders_all_quality_gates(tmp_path: Path) -> None:
    status = RunStatus(
        run_ref=RunRef(
            run_id="20260612_gates",
            run_dir=tmp_path / "runs" / "20260612_gates",
        ),
        meta=RunMeta(
            project="/repo/demo",
            task="gate visibility",
            status="done",
            profile="feature",
            timestamp="2026-06-12T10:00:00",
        ),
        quality_gates=(
            GateStatus(
                name="lint",
                kind="computational",
                outcome="passed",
                duration_s=1.25,
            ),
            GateStatus(
                name="tests",
                kind="computational",
                outcome="skipped",
                duration_s=0.0,
            ),
        ),
    )

    rendered = format_status(status, verbose=True)

    assert "Gates:" in rendered
    assert "passed x1" in rendered
    assert "skipped x1" in rendered
    assert "lint                   passed              1.25s computational" in rendered
    assert "tests                  skipped             0.00s computational" in rendered


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
