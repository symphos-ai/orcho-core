"""Unit tests for :func:`sdk.run_control.snapshots.load_run_snapshot`.

Covers the five pending-operator-action forms plus the no-pending and
not-found paths, and asserts the snapshot is strictly read-only (durable
artifacts are byte-identical and no new files appear after a load).

Tests are filesystem-based but hermetic: each builds its own run tree
under a tmp ``runs_dir`` and passes ``runs_dir=`` / ``cwd=None`` so no
walk-up or ambient workspace leaks in.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdk.errors import NoWorkspace, RunNotFound
from sdk.run_control.snapshots import load_run_snapshot
from sdk.run_control.types import PendingOperatorAction, RunSnapshot

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_run(
    runs_dir: Path,
    run_id: str,
    meta: dict,
    *,
    checkpoint: dict | None = None,
    sub_runs: dict[str, dict] | None = None,
) -> Path:
    """Materialise a run directory with meta.json (+ optional checkpoint / sub-runs)."""
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if checkpoint is not None:
        (run_dir / "cross_checkpoint.json").write_text(
            json.dumps(checkpoint), encoding="utf-8",
        )
    for alias, sub_meta in (sub_runs or {}).items():
        sub_dir = run_dir / alias
        sub_dir.mkdir()
        (sub_dir / "meta.json").write_text(json.dumps(sub_meta), encoding="utf-8")
    return run_dir


def _load(runs_dir: Path, run_id: str) -> RunSnapshot:
    return load_run_snapshot(run_id, runs_dir=runs_dir, cwd=None)


# ── no pending ───────────────────────────────────────────────────────────────


class TestNoPending:
    def test_running_run_has_no_pending_action(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        meta = {
            "status": "running",
            "task": "do a thing",
            "project": "acme",
            "profile": "linear",
            "phases": {"plan": {}, "implement": {}},
            "worktree": {"path": "/wt/x"},
        }
        _make_run(runs, "run-1", meta)

        snap = _load(runs, "run-1")

        assert snap.pending_action is None
        assert snap.run_id == "run-1"
        assert snap.status == "running"
        assert snap.task == "do a thing"
        assert snap.project == "acme"
        assert snap.profile == "linear"
        assert snap.phases == ("plan", "implement")
        assert snap.worktree == {"path": "/wt/x"}
        # raw_meta is the full, unprojected meta dict.
        assert snap.raw_meta == meta

    def test_sub_runs_enumerated_with_status(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _make_run(
            runs,
            "cross-1",
            {"status": "running", "task": "t"},
            sub_runs={"alpha": {"status": "done"}, "beta": {"status": "running"}},
        )

        snap = _load(runs, "cross-1")

        rows = {r.name: r.status for r in snap.sub_runs}
        assert rows == {"alpha": "done", "beta": "running"}


# ── project handoff ──────────────────────────────────────────────────────────


class TestProjectHandoff:
    def test_project_handoff_action(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        # Real durable meta.phase_handoff shape: the id lives under "id".
        meta = {
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "validate_plan:plan_round:1",
                "phase": "validate_plan",
                "available_actions": ["continue", "retry_feedback", "halt"],
            },
        }
        _make_run(runs, "run-2", meta)

        action = _load(runs, "run-2").pending_action
        assert isinstance(action, PendingOperatorAction)
        assert action.kind == "phase_handoff"
        assert action.handoff_kind is None
        assert action.handoff_id == "validate_plan:plan_round:1"
        assert action.phase == "validate_plan"
        assert action.available_actions == ("continue", "retry_feedback", "halt")
        assert action.raw == meta["phase_handoff"]


class TestCurrentPendingHandoffId:
    """The snapshot surfaces the CURRENT active handoff id (MCP-facing).

    ``orcho_run_status`` / ``run_control`` MCP tools do not read meta
    themselves — they relay this snapshot. So the snapshot's
    ``pending_action.handoff_id`` must be the id of the round currently
    awaiting a decision, even when an older round was already decided.
    """

    def test_snapshot_pending_id_is_current_round_not_stale(
        self, tmp_path: Path,
    ) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        # The run already retried once (round:1 decided -> retry) and is now
        # paused awaiting the round:2 review decision.
        meta = {
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "review_changes:repair_round:2",
                "phase": "review_changes",
                "available_actions": ["continue", "retry_feedback", "halt"],
            },
        }
        run_dir = _make_run(runs, "r-progress", meta)
        # A stale decision artifact for the PRIOR round must not shadow the
        # current pending id.
        decisions = run_dir / "phase_handoff_decisions"
        decisions.mkdir()
        (decisions / "review_changes_repair_round_1_old.json").write_text(
            json.dumps(
                {
                    "run_id": "r-progress",
                    "handoff_id": "review_changes:repair_round:1",
                    "phase": "review_changes",
                    "action": "retry_feedback",
                    "feedback": "fix it",
                    "note": None,
                    "decided_at": "2026-06-12T00:00:00+00:00",
                },
            ),
            encoding="utf-8",
        )

        action = _load(runs, "r-progress").pending_action
        assert isinstance(action, PendingOperatorAction)
        assert action.handoff_id == "review_changes:repair_round:2"
        assert action.phase == "review_changes"


# ── cross handoff forms ──────────────────────────────────────────────────────


class TestCrossHandoff:
    """Cross pauses persist the full meta.phase_handoff payload (with id +
    available_actions) via save_cross_session, plus a checkpoint carrying
    the dispatch fields. available_actions must come verbatim from the
    active payload, not the checkpoint."""

    def test_cross_plan(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        meta = {
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "cross_plan:cross_plan_round:1",
                "phase": "cross_plan",
                "available_actions": ["continue", "retry_feedback", "halt"],
            },
        }
        checkpoint = {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "plan",
            "phase_handoff_id": "cross_plan:cross_plan_round:1",
        }
        _make_run(runs, "x-plan", meta, checkpoint=checkpoint)

        action = _load(runs, "x-plan").pending_action
        assert action.kind == "phase_handoff"
        assert action.handoff_kind == "plan"
        assert action.handoff_id == "cross_plan:cross_plan_round:1"
        # available_actions surfaced verbatim from the sanctioned payload.
        assert action.available_actions == ("continue", "retry_feedback", "halt")

    def test_cross_project(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        meta = {
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "project:svc:implement:r1",
                "phase": "implement",
                "available_actions": ["continue", "retry_feedback", "halt"],
            },
        }
        checkpoint = {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "project",
            "phase_handoff_id": "project:svc:implement:r1",
            "phase_handoff_project_alias": "svc",
            "phase_handoff_child_id": "child-run-9",
        }
        _make_run(runs, "x-proj", meta, checkpoint=checkpoint)

        action = _load(runs, "x-proj").pending_action
        assert action.kind == "phase_handoff"
        assert action.handoff_kind == "project"
        assert action.project_alias == "svc"
        assert action.available_actions == ("continue", "retry_feedback", "halt")
        # Child id is preserved in the raw escape hatch, not dropped.
        assert action.raw.get("phase_handoff_child_id") == "child-run-9"

    def test_cross_cfa(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        cfa_state = {"verdict": "REJECTED", "findings_count": 2, "summary": "nope"}
        meta = {
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "cfa:cross_final_acceptance:1",
                "phase": "cross_final_acceptance",
                "available_actions": ["continue", "halt"],
            },
        }
        checkpoint = {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "cfa",
            "phase_handoff_id": "cfa:cross_final_acceptance:1",
            "cfa_paused_state": cfa_state,
        }
        _make_run(runs, "x-cfa", meta, checkpoint=checkpoint)

        action = _load(runs, "x-cfa").pending_action
        assert action.kind == "phase_handoff"
        assert action.handoff_kind == "cfa"
        assert action.available_actions == ("continue", "halt")
        # cfa_paused_state is preserved verbatim in raw.
        assert action.raw.get("cfa_paused_state") == cfa_state

    def test_cross_pending_without_active_payload_yields_empty_actions(
        self, tmp_path: Path,
    ) -> None:
        # Defensive: a checkpoint marked pending but no meta.phase_handoff
        # (legacy / partial state) surfaces empty available_actions rather
        # than inventing verbs.
        runs = tmp_path / "runs"
        runs.mkdir()
        checkpoint = {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "plan",
            "phase_handoff_id": "cross_plan:cross_plan_round:1",
        }
        _make_run(runs, "x-bare", {"status": "running", "task": "t"}, checkpoint=checkpoint)

        action = _load(runs, "x-bare").pending_action
        assert action.kind == "phase_handoff"
        assert action.handoff_kind == "plan"
        # Falls back to the checkpoint id when no active payload is present.
        assert action.handoff_id == "cross_plan:cross_plan_round:1"
        assert action.available_actions == ()


# ── gate ─────────────────────────────────────────────────────────────────────


class TestGate:
    def test_pending_gate(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        gate = {
            "name": "review_gate",
            "run_policy": "blocking",
            "choices": ["run", "skip"],
            "on_skip": "halt",
        }
        _make_run(runs, "g-1", {"status": "running", "task": "t"}, checkpoint={"pending_gate": gate})

        action = _load(runs, "g-1").pending_action
        assert action.kind == "gate"
        assert action.handoff_kind is None
        # Gate choices are NOT reinterpreted as handoff verbs.
        assert action.available_actions == ()
        # choices / on_skip stay observable in raw.
        assert action.raw["choices"] == ["run", "skip"]
        assert action.raw["on_skip"] == "halt"


# ── precedence: cross checkpoint authoritative ───────────────────────────────


class TestPrecedence:
    def test_cross_checkpoint_wins_over_meta_handoff(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        meta = {
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {
                "id": "cfa:cross_final_acceptance:1",
                "phase": "cross_final_acceptance",
                "available_actions": ["continue", "halt"],
            },
        }
        checkpoint = {
            "phase_handoff_pending": True,
            "phase_handoff_kind": "cfa",
            "phase_handoff_id": "cfa:cross_final_acceptance:1",
        }
        _make_run(runs, "both", meta, checkpoint=checkpoint)

        action = _load(runs, "both").pending_action
        assert action.handoff_kind == "cfa"


# ── error + read-only invariants ─────────────────────────────────────────────


class TestErrorsAndReadOnly:
    def test_unknown_run_raises_run_not_found(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        with pytest.raises(RunNotFound):
            load_run_snapshot("missing", runs_dir=runs, cwd=None)

    def test_missing_workspace_raises_no_workspace(self, tmp_path: Path) -> None:
        with pytest.raises(NoWorkspace):
            load_run_snapshot("any", runs_dir=tmp_path / "nope", cwd=None)

    def test_load_does_not_mutate_artifacts(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        meta = {
            "status": "awaiting_phase_handoff",
            "task": "t",
            "phase_handoff": {"handoff_id": "h", "available_actions": ["continue"]},
        }
        checkpoint = {"pending_gate": {"name": "g", "choices": ["run", "skip"]}}
        run_dir = _make_run(runs, "ro", meta, checkpoint=checkpoint)

        before = {
            p.name: (p.read_bytes(), p.stat().st_mtime_ns)
            for p in run_dir.iterdir()
            if p.is_file()
        }
        files_before = sorted(p.name for p in run_dir.iterdir())

        load_run_snapshot("ro", runs_dir=runs, cwd=None)

        after = {
            p.name: (p.read_bytes(), p.stat().st_mtime_ns)
            for p in run_dir.iterdir()
            if p.is_file()
        }
        files_after = sorted(p.name for p in run_dir.iterdir())
        assert before == after
        assert files_before == files_after
